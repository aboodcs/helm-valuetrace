"""Bounded, offline JSON Schema validation using jsonschema's dialect validators."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from helm_valuetrace.exceptions import ValueTraceError
from helm_valuetrace.models import SchemaError

MAX_SCHEMA_BYTES = 8 * 1024 * 1024
MAX_VALIDATION_BYTES = 64 * 1024 * 1024
MAX_SCHEMA_DEPTH = 64
MAX_SCHEMA_NODES = 100_000
MAX_SCHEMA_ERRORS = 1000
SCHEMA_TIMEOUT = 10


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member")
        result[key] = value
    return result


def _invalid_number(_):
    raise ValueError("Non-finite JSON number")


def _finite_float(raw):
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("Non-finite JSON number")
    return value


def load_schema(chart_dir: Path) -> dict[str, Any] | None:
    """Load an optional UTF-8 JSON object; input diagnostics never echo its contents."""
    path = chart_dir / "values.schema.json"
    if not path.exists() and not path.is_symlink():
        return None
    try:
        if not path.is_file():
            raise ValueError("Schema must be a regular file")
        with path.open("rb") as stream:
            raw = stream.read(MAX_SCHEMA_BYTES + 1)
        if len(raw) > MAX_SCHEMA_BYTES:
            raise ValueError("Schema size limit exceeded")
        data = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_number,
            parse_float=_finite_float,
        )
        if not isinstance(data, dict):
            raise ValueError("Schema root must be an object")
        pending, count = [(data, 0)], 0
        while pending:
            item, depth = pending.pop()
            count += 1
            if depth > MAX_SCHEMA_DEPTH or count > MAX_SCHEMA_NODES:
                raise ValueError("Schema structure limit exceeded")
            if isinstance(item, dict):
                pending.extend((v, depth + 1) for v in item.values())
            elif isinstance(item, list):
                pending.extend((v, depth + 1) for v in item)
        return data
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise ValueTraceError(
            "Malformed values.schema.json: require a readable, bounded UTF-8 JSON object "
            "without duplicate members or non-finite numbers"
        ) from None


def _validate(values: dict[str, Any], schema: dict[str, Any]) -> list[SchemaError]:
    """Evaluate inside the isolated worker, including potentially costly regex/ref rules."""
    import jsonschema
    from referencing import Registry
    from referencing.exceptions import NoSuchResource

    def no_remote(uri):
        raise NoSuchResource(ref=uri)

    validator_class = (
        jsonschema.validators.validator_for(schema, default=None)
        if "$schema" in schema
        else jsonschema.Draft7Validator
    )
    if validator_class is None:
        raise ValueTraceError("Unsupported JSON Schema dialect")
    try:
        validator_class.check_schema(schema)
    except jsonschema.SchemaError:
        raise ValueTraceError("Invalid values.schema.json schema definition") from None
    validator = validator_class(schema, registry=Registry(retrieve=no_remote))
    errors = []
    try:
        for error in validator.iter_errors(values):
            if len(errors) >= MAX_SCHEMA_ERRORS:
                raise ValueTraceError("Schema validation error limit exceeded")
            errors.append(
                SchemaError(
                    key=".".join(str(p) for p in error.absolute_path) or "(root)",
                    message=f"Schema constraint failed: {error.validator}",
                    schema_path=".".join(str(p) for p in error.absolute_schema_path),
                )
            )
    except ValueTraceError:
        raise
    except Exception:
        raise ValueTraceError(
            "Schema evaluation failed; invalid or external references are unsupported"
        ) from None
    return errors


def _worker() -> None:
    """Private subprocess entrypoint. No chart code, shell, or remote retrieval is used."""
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        values, schema = json.load(sys.stdin)
        doc = {"errors": [asdict(e) for e in _validate(values, schema)]}
    except ValueTraceError as exc:
        doc = {"error": str(exc)}
    except ImportError:
        doc = {
            "error": "Schema validation requires jsonschema, referencing and POSIX resource limits"
        }
    except Exception:
        doc = {"error": "Schema evaluation failed or resource limit exceeded (details omitted)"}
    print(json.dumps(doc, ensure_ascii=True, allow_nan=False))


def validate_values(values: dict[str, Any], schema: dict[str, Any]) -> list[SchemaError]:
    """Validate offline with bounded CPU, memory, wall time, input and diagnostic count.

    The worker prevents schema regexes or reference recursion from hanging the CLI.
    Format annotations are not asserted. Unknown dialects fail explicitly.
    """
    try:
        payload = json.dumps([values, schema], ensure_ascii=True, allow_nan=False)
    except (ValueError, RecursionError, TypeError):
        raise ValueTraceError("Schema input must contain bounded JSON-compatible values") from None
    if len(payload) > MAX_VALIDATION_BYTES:
        raise ValueTraceError("Schema validation input size limit exceeded")
    paths = [str(Path(p).resolve()) for p in sys.path if p and Path(p).resolve() != Path.cwd()]
    package_root = str(Path(__file__).resolve().parents[2])
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([package_root, *paths]))
    bootstrap = (
        "import sys; sys.path.pop(0); "
        "from helm_valuetrace.analysis.schema import _worker; _worker()"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", bootstrap],
            input=payload,
            text=True,
            capture_output=True,
            env=env,
            timeout=SCHEMA_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ValueTraceError("Schema evaluation timed out: resource limit exceeded") from None
    except OSError:
        raise ValueTraceError("Cannot start isolated schema validation") from None
    if proc.returncode:
        raise ValueTraceError("Schema evaluation failed or resource limit exceeded")
    try:
        doc = json.loads(proc.stdout)
        if "error" in doc:
            raise ValueTraceError(doc["error"])
        return [SchemaError(**row) for row in doc["errors"]]
    except (ValueError, KeyError, TypeError):
        raise ValueTraceError("Invalid response from isolated schema validation") from None
