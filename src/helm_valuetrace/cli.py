from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import yaml

from . import __version__
from .analysis.secrets import DEFAULT_SENSITIVE_PATTERNS, redact_value
from .core import (
    ChartError,
    TraceResult,
    ValueTraceError,
    final_assignment,
    flatten_values,
    load_values_file,
    path_text,
    trace_values,
)
from .models import DeniedSource
from .output.safety import contributor_rows, escaped_path, terminal_safe
from .parsers.set_parser import parse_all_set_arguments


class ValueTraceArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueTraceError(
            message.split("invalid choice:")[0] + "invalid choice (value omitted)"
            if "invalid choice:" in message
            else message
        )


def build_parser() -> argparse.ArgumentParser:
    parser = ValueTraceArgumentParser(
        prog="helm valuetrace",
        usage="helm valuetrace CHART [options]",
        add_help=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent(
            """\
            Trace, validate, and compare the values supplied to a local Helm chart.

            ValueTrace shows the final value of every key, the file and line that
            supplied it, and how many times it was assigned. It can also detect
            unknown keys, reject forbidden override files, and compare an
            environment file with a reference structure.

            The command is read-only: it does not connect to Kubernetes, render
            templates, modify files, or install a Helm release.
            """
        ),
        epilog=textwrap.dedent(
            """\
            VALUE PRECEDENCE
              chart defaults -> -f files -> --set-json -> --set -> --set-string
              -> --set-file -> --set-literal
              User layers merge first; chart defaults then fill missing paths.

            EXAMPLES
              Explain one key (primary syntax):
                helm valuetrace ./chart --explain image.tag

              Trace one environment file:
                helm valuetrace ./chart -f ./values/staging.yaml

              Trace layered files and command-line values:
                helm valuetrace ./chart -f ./values/base.yaml -f ./values/prod.yaml \\
                  --set image.tag=v2.0.0 --only-overridden

              Detect misspelled or unexpected keys:
                helm valuetrace ./chart -f ./values/prod.yaml --strict-unknown

              Block debug values files from a deployment path:
                helm valuetrace ./chart -f ./values/prod.yaml \\
                  -f ./values/local-debug.yaml --deny-source '*local-debug*'

              Compare a values file with a reference structure:
                helm valuetrace ./chart --reference-values ./values/reference.yaml \\
                  -f ./values/staging.yaml --strict-reference

            EXIT CODES
              0  Success: analysis completed; no enabled validation or policy failed.
              1  Usage / input error: invalid argument, missing file, or invalid syntax.
              2  Validation / policy violation: structural issues, unknown keys, or denied source.
              3  Chart / dependency error: missing Chart.yaml or malformed archive.
              4  Internal error.

            Run 'helm valuetrace --version' to print the installed version.
            """
        ),
    )

    required = parser.add_argument_group("Required argument")
    required.add_argument(
        "chart",
        metavar="CHART",
        type=Path,
        help="Local unpacked chart directory containing Chart.yaml",
    )
    required.add_argument(
        "subcommand",
        nargs="?",
        default=None,
        help="Legacy syntax: CHART explain KEY; prefer CHART --explain KEY",
    )
    required.add_argument(
        "explain_key",
        nargs="?",
        default=None,
        help="Key to explain when using the 'explain' subcommand",
    )

    value_sources = parser.add_argument_group("Value sources")
    value_sources.add_argument(
        "-f",
        "--values",
        action="append",
        default=[],
        type=Path,
        metavar="FILE",
        help="Merge an override YAML file; repeat the option to add more files",
    )
    value_sources.add_argument(
        "--set",
        dest="set_values",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Apply a dotted key override after all files; repeatable",
    )
    value_sources.add_argument(
        "--set-string",
        dest="set_string_values",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Apply a string override (no type coercion); repeatable",
    )
    value_sources.add_argument(
        "--set-file",
        dest="set_file_values",
        action="append",
        default=[],
        metavar="KEY=PATH",
        help="Set key to the contents of a file; repeatable",
    )
    value_sources.add_argument(
        "--set-json",
        dest="set_json_values",
        action="append",
        default=[],
        metavar="KEY=JSON",
        help="Set key to a JSON-parsed value; repeatable",
    )
    value_sources.add_argument(
        "--set-literal",
        dest="set_literal_values",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Set key to a literal string (backslash/comma not special); repeatable",
    )
    value_sources.add_argument(
        "--reference-values",
        type=Path,
        metavar="FILE",
        help="Use FILE as the allowed-key structure without merging its values",
    )

    validation = parser.add_argument_group("Validation")
    validation.add_argument(
        "--strict-unknown",
        action="store_true",
        help="Exit with code 2 when an override or --set contains an unknown key",
    )
    validation.add_argument(
        "--strict-reference",
        action="store_true",
        help="Exit with code 2 when reference comparison finds extra or missing keys",
    )
    validation.add_argument(
        "--deny-source",
        action="append",
        default=[],
        metavar="PATTERN",
        help="Exit with code 2 when a -f file matches this name or glob; repeatable",
    )
    validation.add_argument(
        "--deny-cli-overrides",
        action="store_true",
        help="Exit with code 2 when any --set, --set-string, --set-file, --set-json, "
        "or --set-literal override is supplied",
    )
    validation.add_argument(
        "--strict-schema",
        action="store_true",
        help="Exit with code 2 when values.schema.json validation fails (default: warn only)",
    )

    output = parser.add_argument_group("Output")
    output.add_argument(
        "--explain",
        dest="explain_option",
        metavar="KEY",
        help="Explain one effective value and its history",
    )
    output.add_argument(
        "--redact-path",
        action="append",
        default=[],
        metavar="PATH",
        help="Redact this exact report path and its descendants; repeatable",
    )
    output.add_argument(
        "-o",
        "--output",
        choices=("table", "json", "yaml"),
        default="table",
        metavar="FORMAT",
        help="Report format: table, json, or yaml (default: table)",
    )
    output.add_argument(
        "--only-overridden",
        action="store_true",
        help="Display only keys that were assigned at least twice",
    )
    output.add_argument(
        "--no-redact",
        action="store_true",
        default=False,
        help="Disable automatic redaction of sensitive values (passwords, tokens, etc.)",
    )
    output.add_argument(
        "--redact-pattern",
        dest="redact_patterns",
        action="append",
        default=None,
        metavar="PATTERN",
        help="Additional glob pattern for sensitive key names; repeatable",
    )
    output.add_argument(
        "--template-analysis",
        action="store_true",
        default=False,
        help=(
            "Scan chart templates for .Values references and report unused values "
            "(informational only; never causes exit code 2)"
        ),
    )
    output.add_argument(
        "--experimental-subcharts",
        action="store_true",
        default=False,
        help=(
            "[EXPERIMENTAL] Process subchart dependencies in charts/. "
            "Does not run 'helm dependency update'. May not match Helm exactly."
        ),
    )

    general = parser.add_argument_group("General")
    general.add_argument(
        "-h",
        "--help",
        action="help",
        help="Show this help message and exit",
    )
    general.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"Helm ValueTrace {__version__}",
        help="Show the installed ValueTrace version and exit",
    )
    return parser


def _compact_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if isinstance(value, bool):
        return str(value).lower()
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _result_rows(
    result: TraceResult,
    only_overridden: bool,
    redact_patterns: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Build output rows from TraceResult.

    If *redact_patterns* is not None (i.e. redaction is enabled), sensitive
    values are replaced with the REDACTED sentinel before inclusion in any row.
    """
    patterns = redact_patterns if redact_patterns is not None else DEFAULT_SENSITIVE_PATTERNS

    def _maybe_redact(value: Any, key: str) -> Any:
        if redact_patterns is None:
            return value
        return redact_value(value, key, patterns)

    active_ids = {
        id(a) for sources in getattr(result.history, "contributors", {}).values() for a in sources
    }
    rows: list[dict[str, Any]] = []
    for path, value in sorted(
        flatten_values(result.values).items(), key=lambda item: path_text(item[0])
    ):
        events = getattr(result.history, "events", [])
        related = (
            result.history.related_events(path)
            if hasattr(result.history, "related_events")
            else [(p, a) for p, a in events if p[: len(path)] == path or path[: len(p)] == p]
        )
        if not events:
            related = [(path, a) for a in result.history.get(path, [])]
        assignments = [a for _, a in related]
        if only_overridden and len(assignments) < 2:
            continue
        final = final_assignment(path, result.history)
        key_str = path_text(path)
        contributors = contributor_rows(path, result.history)
        mixed = len({c["file"] for c in contributors}) > 1
        rows.append(
            {
                "key": key_str,
                "path": list(path),
                "escaped_key": escaped_path(path),
                "value": _maybe_redact(value, path),
                "source": "multiple sources" if mixed else final.location if final else "computed",
                "source_type": None if mixed else final.source_type.value if final else None,
                "contributors": contributors,
                "assignments": [
                    {
                        "source": assignment.location,
                        "source_type": assignment.source_type.value,
                        "line": assignment.line,
                        "column": assignment.column,
                        "key": path_text(assignment_path),
                        "status": "contributing" if id(assignment) in active_ids else "overridden",
                        "path": list(assignment_path),
                        "value": _maybe_redact(
                            _maybe_redact(assignment.value, assignment_path), path
                        ),
                    }
                    for assignment_path, assignment in related
                ],
            }
        )
    return rows


def _unknown_rows(result: TraceResult) -> list[dict[str, str | None]]:
    return [
        {"key": item.key, "source": item.source, "suggestion": item.suggestion}
        for item in result.unknown
    ]


def _missing_rows(result: TraceResult) -> list[dict[str, str]]:
    return [{"key": item.key, "reference": item.reference} for item in result.missing]


def _find_denied_sources(
    value_paths: list[Path],
    patterns: list[str],
) -> list[DeniedSource]:
    for pattern in patterns:
        if not pattern:
            raise ValueTraceError("--deny-source PATTERN must not be empty")

    denied: list[DeniedSource] = []
    for value_path in value_paths:
        resolved = value_path.resolve()
        candidates = {
            str(value_path),
            value_path.as_posix(),
            value_path.name,
            str(resolved),
            resolved.as_posix(),
        }
        for pattern in patterns:
            if any(fnmatch.fnmatchcase(candidate, pattern) for candidate in candidates):
                denied.append(DeniedSource(source=str(value_path), pattern=pattern))
                break
    return denied


def _denied_source_rows(denied_sources: list[DeniedSource]) -> list[dict[str, str]]:
    return [{"source": item.source, "pattern": item.pattern} for item in denied_sources]


def _detect_helm_major_version() -> int | None:
    """Read the invoking Helm major version without contacting a cluster."""
    helm_bin = os.environ.get("HELM_BIN")
    if not helm_bin:
        return None

    try:
        process = subprocess.run(
            [helm_bin, "version", "--template", "{{.Version}}"],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValueTraceError("Cannot determine configured Helm version") from None

    if process.returncode != 0:
        raise ValueTraceError("Cannot determine configured Helm version")
    match = re.search(r"v?(\d+)(?:\.|$)", process.stdout.strip())
    if not match or int(match.group(1)) not in (3, 4):
        raise ValueTraceError("Configured Helm version must be 3 or 4")
    return int(match.group(1))


def _print_warnings(result: TraceResult, denied_sources: list[DeniedSource]) -> None:
    if not (result.unknown or result.missing or denied_sources):
        return

    print("\nWARNINGS", file=sys.stderr)
    for item in result.unknown:
        message = f"- Unknown key '{item.key}' from {item.source}"
        if item.suggestion:
            message += f"; did you mean '{item.suggestion}'?"
        print(terminal_safe(message), file=sys.stderr)
    for item in result.missing:
        print(
            terminal_safe(f"- Missing key '{item.key}' compared with {item.reference}"),
            file=sys.stderr,
        )
    for item in denied_sources:
        print(
            terminal_safe(f"- Denied source '{item.source}' matched pattern '{item.pattern}'"),
            file=sys.stderr,
        )


def _print_table(
    rows: list[dict[str, Any]],
    result: TraceResult,
    denied_sources: list[DeniedSource],
    source_policy_enabled: bool,
) -> None:
    headers = ("KEY", "FINAL VALUE", "SOURCE", "ASSIGNMENTS")
    rendered = [
        (
            terminal_safe(row["key"]),
            terminal_safe(_compact_value(row["value"])),
            terminal_safe(row["source"]),
            str(len(row["assignments"])),
        )
        for row in rows
    ]
    widths = [len(header) for header in headers]
    for row in rendered:
        for index, cell in enumerate(row):
            widths[index] = min(max(widths[index], len(cell)), (46, 36, 44, 11)[index])

    def clipped(cell: str, width: int, keep_end: bool = False) -> str:
        if len(cell) <= width:
            return cell
        if keep_end:
            return "…" + cell[-max(0, width - 1) :]
        return cell[: max(0, width - 1)] + "…"

    print("HELM VALUETRACE")
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rendered:
        print(
            "  ".join(
                clipped(cell, widths[index], keep_end=index == 2).ljust(widths[index])
                for index, cell in enumerate(row)
            )
        )

    overridden = sum(1 for row in rows if len(row["assignments"]) > 1)
    total = len(flatten_values(result.values))
    displayed = (
        f"{len(rows)} displayed of {total} final values"
        if len(rows) != total
        else f"{total} final values"
    )
    summary = (
        f"\n{displayed}, {overridden} overridden values, "
        f"{len(result.unknown)} unknown keys, {len(result.missing)} missing reference keys"
    )
    if source_policy_enabled:
        noun = "source" if len(denied_sources) == 1 else "sources"
        summary += f", {len(denied_sources)} denied {noun}"
    print(summary)
    _print_warnings(result, denied_sources)


def _print_structured(
    output: str,
    rows: list[dict[str, Any]],
    result: TraceResult,
    denied_sources: list[DeniedSource],
) -> None:
    schema_error_rows = [
        {"key": e.key, "message": e.message, "schema_path": e.schema_path}
        for e in result.schema_errors
    ]
    document = {
        "values": rows,
        "unknown": _unknown_rows(result),
        "missing": _missing_rows(result),
        "denied_sources": _denied_source_rows(denied_sources),
        "schema_errors": schema_error_rows,
    }
    if output == "json":
        print(json.dumps(document, indent=2, ensure_ascii=True, allow_nan=False))
    else:
        print(yaml.safe_dump(document, sort_keys=False, allow_unicode=False).rstrip())
    _print_warnings(result, denied_sources)


def _run_with_chart_dir(args: argparse.Namespace, chart: Path) -> int:
    """Execute the trace analysis against a resolved chart directory.

    Separated from run() so that TgzChartLoader can supply the temporary
    chart directory as a context-managed Path before calling this function.
    """
    default_path = chart / "values.yaml"
    displayed_default_path = args.chart / "values.yaml"
    if default_path.is_file():
        default_values, default_lines, default_cols = load_values_file(default_path)
    else:
        default_values, default_lines, default_cols = {}, {}, {}

    overrides = []
    for value_path in args.values:
        resolved = value_path.resolve()
        values, lines, _cols = load_values_file(resolved)
        overrides.append((values, str(value_path), lines))
    denied_sources = _find_denied_sources(args.values, args.deny_source)

    reference_values = None
    reference_source = None
    if args.reference_values is not None:
        reference_path = args.reference_values.resolve()
        reference_values, _, _ref_cols = load_values_file(reference_path)
        reference_source = str(args.reference_values)

    all_set_entries = parse_all_set_arguments(
        set_args=args.set_values or [],
        set_string_args=args.set_string_values or [],
        set_file_args=args.set_file_values or [],
        set_json_args=args.set_json_values or [],
        set_literal_args=args.set_literal_values or [],
    )

    if args.deny_cli_overrides:
        for attribute, flag in (
            ("set_values", "--set"),
            ("set_string_values", "--set-string"),
            ("set_file_values", "--set-file"),
            ("set_json_values", "--set-json"),
            ("set_literal_values", "--set-literal"),
        ):
            for index, _ in enumerate(getattr(args, attribute) or [], 1):
                denied_sources.append(DeniedSource(f"{flag}[{index}]", "--deny-cli-overrides"))

    from .subcharts import discover_subcharts, select_enabled_subcharts

    subcharts = discover_subcharts(chart, displayed_chart=args.chart)

    trace_options = {
        "default_values": default_values,
        "default_source": str(displayed_default_path),
        "default_lines": default_lines,
        "overrides": overrides,
        "set_arguments": (),
        "reference_values": reference_values,
        "reference_source": reference_source,
        "helm_major_version": _detect_helm_major_version(),
        "default_cols": default_cols,
        "extra_set_entries": all_set_entries if all_set_entries else None,
        "subcharts": subcharts,
    }

    result = trace_values(**trace_options)
    enabled = select_enabled_subcharts(subcharts, result.values)
    if len(enabled) != len(subcharts):
        subcharts = enabled
        trace_options["subcharts"] = subcharts
        result = trace_values(**trace_options)

    from .analysis.schema import load_schema
    from .analysis.schema import validate_values as validate_schema_values

    chart_schema = load_schema(chart)
    schema_errors = []
    if args.strict_schema and chart_schema is None:
        raise ValueTraceError("--strict-schema requires values.schema.json")
    if chart_schema is not None:
        schema_errors = validate_schema_values(result.values, chart_schema)
        result.schema_errors = schema_errors
        if result.unknown and args.reference_values is None:
            result.unknown = [
                u for u in result.unknown if not _schema_permits_key(chart_schema, u.key)
            ]

    from dataclasses import replace

    from .core import MISSING, _lookup

    for subchart in subcharts:
        child_schema = load_schema(subchart.chart_dir)
        child_values = _lookup(result.values, subchart.path_prefix)
        if child_schema is not None and child_values is not MISSING:
            for error in validate_schema_values(child_values, child_schema):
                prefix = path_text(subchart.path_prefix)
                schema_errors.append(
                    replace(
                        error, key=prefix if error.key == "(root)" else prefix + "." + error.key
                    )
                )
    result.schema_errors = schema_errors

    if args.no_redact:
        redact_patterns = None
    else:
        redact_patterns = list(DEFAULT_SENSITIVE_PATTERNS)
        if args.redact_patterns:
            redact_patterns.extend(args.redact_patterns)
        redact_patterns.extend("path:" + p for p in args.redact_path)

    rows = (
        []
        if args.subcommand == "explain"
        else _result_rows(result, args.only_overridden, redact_patterns=redact_patterns)
    )

    if args.subcommand == "explain":
        _print_explanation(args, result, redact_patterns)
        _print_warnings(result, denied_sources)
    elif args.output == "table":
        _print_table(
            rows,
            result,
            denied_sources,
            source_policy_enabled=bool(args.deny_source or args.deny_cli_overrides),
        )
    else:
        _print_structured(args.output, rows, result, denied_sources)

    if schema_errors:
        print("\nSCHEMA WARNINGS", file=sys.stderr)
        for err in schema_errors:
            print(terminal_safe(f"- [{err.key}] {err.message}"), file=sys.stderr)

    if args.template_analysis:
        from .analysis.templates import LIMITATIONS, find_value_references

        template_refs = find_value_references(chart)
        result.used_by_templates = template_refs

        all_value_keys = {path_text(p) for p in flatten_values(result.values)}
        used_keys = set(template_refs.keys())
        unused_keys = {
            key
            for key in all_value_keys
            if not any(
                ref == "" or key == ref or key.startswith((ref + ".", ref + "["))
                for ref in used_keys
            )
        }

        print("\nTEMPLATE ANALYSIS", file=sys.stderr)
        print("Limitations:", file=sys.stderr)
        for lim in LIMITATIONS:
            print(f"  - {lim}", file=sys.stderr)
        if "" in template_refs:
            print(
                "Root .Values is consumed wholesale; child usage cannot be determined "
                "statically. All values are potentially referenced.",
                file=sys.stderr,
            )
        if unused_keys:
            print(f"\nValues not referenced in templates ({len(unused_keys)}):", file=sys.stderr)
            for key in sorted(unused_keys):
                print(terminal_safe(f"  WARNING: {key}"), file=sys.stderr)

    if args.experimental_subcharts:
        print(
            "\nWARNING: --experimental-subcharts reports the local dependency subset only.",
            file=sys.stderr,
        )
        if subcharts:
            print("Subcharts found:", file=sys.stderr)
            for subchart in subcharts:
                print(
                    terminal_safe(f"  {subchart.alias}: {subchart.source_label}"), file=sys.stderr
                )
        else:
            print("No pre-fetched subcharts found in charts/ directory.", file=sys.stderr)

    if denied_sources:
        return 2
    if args.strict_unknown and result.unknown:
        return 2
    if args.strict_reference and (result.unknown or result.missing):
        return 2
    if args.strict_schema and schema_errors:
        return 2
    return 0


def _print_explanation(
    args: argparse.Namespace, result: TraceResult, patterns: list[str] | None
) -> None:
    from .output.explain import format_explain
    from .parsers.set_parser import _parse_key_path

    key = args.explain_key
    if not key:
        raise ValueTraceError("'explain' requires a KEY argument")
    path = _parse_key_path(key)
    available = set(result.history) | set(getattr(result.history, "winners", {}))
    if path not in available:
        matches = [p for p in available if path_text(p) == key]
        if len(matches) == 1:
            path = matches[0]
        elif len(matches) > 1:
            raise ValueTraceError("Ambiguous key; escape literal dots and brackets")
    if path not in available and not any(p[: len(path)] == path for p in available):
        raise ValueTraceError(f"Key not found in chart values: {key!r}")

    def redactor(value, name):
        return value if patterns is None else redact_value(value, name, patterns)

    print(format_explain(key, path, result, redactor, args.output))


def _is_archive_path(chart_path: Path) -> bool:
    """Return True if chart_path is an archive (.tgz, .tar.gz, or tarball)."""
    name = chart_path.name.lower()
    if name.endswith((".tgz", ".tar.gz")):
        return True
    try:
        import tarfile

        if chart_path.is_file() and tarfile.is_tarfile(chart_path):
            return True
    except OSError:
        pass
    return False


def _schema_permits_key(schema: dict[str, Any], dotted_key: str) -> bool:
    """Return True if the JSON Schema explicitly permits *dotted_key*.

    Walks the schema tree along each segment of the dotted key.  At each
    level, a segment is considered permitted when:

    * it matches a ``properties`` entry in the current schema node, OR
    * it matches any ``patternProperties`` regex pattern in the node, OR
    * ``additionalProperties`` is ``True`` or absent (JSON Schema default).

    If ``additionalProperties`` is ``False`` or a sub-schema dict **and** the
    key segment does not match ``properties`` or ``patternProperties``, the
    key is NOT permitted and the heuristic unknown report is kept.

    Array-index segments (integers) are always considered permitted.

    This is used by the MEGA-F-001 v2 fix to filter ``result.unknown``
    individually per key rather than suppressing all unknowns based solely on
    the root-level ``additionalProperties`` value.
    """
    import re as _re

    segments = dotted_key.replace("\\.", "\x00").split(".")
    segments = [s.replace("\x00", ".") for s in segments]

    node: dict[str, Any] | None = schema

    for seg in segments:
        if node is None:
            return True

        if not isinstance(node, dict):
            return True

        node = _resolve_schema_refs(node)

        props = node.get("properties", {})
        pattern_props = node.get("patternProperties", {})
        ap = node.get("additionalProperties", None)

        if seg.lstrip("-").isdigit():
            node = node.get("items") or node.get("prefixItems")
            continue

        if seg in props:
            node = props[seg] if isinstance(props[seg], dict) else None
            continue

        pattern_match = False
        for pattern, pschema in pattern_props.items():
            try:
                if _re.search(pattern, seg):
                    pattern_match = True
                    node = pschema if isinstance(pschema, dict) else None
                    break
            except _re.error:
                pass

        if pattern_match:
            continue

        return ap is True or ap is None

    return True


def _resolve_schema_refs(node: dict[str, Any]) -> dict[str, Any]:
    """Shallow best-effort $ref/allOf resolver for structural inspection.

    We only need to detect additionalProperties/properties/patternProperties.
    Full $ref resolution happens inside jsonschema during validation.
    This function handles the common pattern of wrapping via allOf.
    """
    if not isinstance(node, dict):
        return node

    if "allOf" in node:
        merged: dict[str, Any] = {}
        for sub in node.get("allOf", []):
            if isinstance(sub, dict) and "$ref" not in sub:
                merged.update(sub)
        merged.update({k: v for k, v in node.items() if k != "allOf"})
        return merged

    return node


def run(args: argparse.Namespace) -> int:
    if args.explain_option is not None:
        if args.subcommand is not None:
            raise ValueTraceError("Use either --explain KEY or explain KEY")
        args.subcommand, args.explain_key = "explain", args.explain_option
    if args.strict_reference and args.reference_values is None:
        raise ValueTraceError("--strict-reference requires --reference-values FILE")

    from .parsers.set_parser import _parse_key_path

    for redact_path in args.redact_path:
        _parse_key_path(redact_path)
    chart_path = args.chart
    is_archive = _is_archive_path(chart_path)

    if is_archive:
        from .parsers.chart_loader import TgzChartLoader

        with TgzChartLoader(chart_path.resolve()) as chart_dir:
            if args.subcommand == "explain":
                return _run_with_chart_dir(args, chart_dir)
            if args.subcommand is not None:
                raise ValueTraceError(
                    f"Unknown subcommand: {args.subcommand!r}. Did you mean 'explain'?"
                )
            return _run_with_chart_dir(args, chart_dir)

    chart = chart_path.resolve()
    if not chart.is_dir():
        raise ValueTraceError(f"Chart directory not found: {chart}")
    chart_yaml = chart / "Chart.yaml"
    if not chart_yaml.is_file():
        raise ChartError(f"Chart.yaml not found in: {chart}")
    try:
        metadata, _, _ = load_values_file(chart_yaml)
        if not all(
            isinstance(metadata.get(k), str) and metadata[k] for k in ("name", "apiVersion")
        ):
            raise ValueTraceError("missing required name or apiVersion")
    except ValueTraceError as exc:
        raise ChartError(f"Malformed Chart.yaml in {chart}") from exc

    if args.subcommand == "explain":
        return _run_with_chart_dir(args, chart)
    if args.subcommand is not None:
        raise ValueTraceError(f"Unknown subcommand: {args.subcommand!r}. Did you mean 'explain'?")
    return _run_with_chart_dir(args, chart)


def parse_cli_args(
    parser: argparse.ArgumentParser, argv: list[str] | None = None
) -> argparse.Namespace:
    """Parse CLI arguments allowing flags to appear before or after subcommands."""
    args, unknown = parser.parse_known_intermixed_args(argv)
    if unknown:
        parser.error("unrecognized arguments (values omitted)")
    return args


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parse_cli_args(parser, argv)
        return run(args)
    except ChartError as exc:
        print(terminal_safe(f"Error: {exc}"), file=sys.stderr)
        return 3
    except ValueTraceError as exc:
        print(terminal_safe(f"Error: {exc}"), file=sys.stderr)
        return 1
    except Exception:
        print(
            "Internal error: analysis failed (details suppressed to protect values)",
            file=sys.stderr,
        )
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
