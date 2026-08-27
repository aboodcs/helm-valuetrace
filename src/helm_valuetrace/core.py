from __future__ import annotations

import copy
import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode


PathKey = tuple[str, ...]
MISSING = object()
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


class ValueTraceError(Exception):
    """Raised for input errors that should be shown without a traceback."""


@dataclass(frozen=True)
class Assignment:
    source: str
    line: int | None
    value: Any

    @property
    def location(self) -> str:
        if self.line is None:
            return self.source
        return f"{self.source}:{self.line}"


@dataclass(frozen=True)
class UnknownValue:
    key: str
    source: str
    suggestion: str | None = None


@dataclass(frozen=True)
class MissingValue:
    key: str
    reference: str


@dataclass
class TraceResult:
    values: dict[str, Any]
    history: dict[PathKey, list[Assignment]]
    unknown: list[UnknownValue]
    missing: list[MissingValue]


def path_text(path: PathKey) -> str:
    return ".".join(path)


def flatten_values(value: Any, prefix: PathKey = ()) -> dict[PathKey, Any]:
    """Flatten mappings while keeping lists and empty mappings as leaf values."""
    if isinstance(value, dict) and value:
        flattened: dict[PathKey, Any] = {}
        for key, child in value.items():
            flattened.update(flatten_values(child, prefix + (str(key),)))
        return flattened
    if not prefix:
        return {}
    return {prefix: value}


def _known_paths(value: Any, prefix: PathKey = ()) -> set[PathKey]:
    """Collect both mapping parents and leaves as structurally known paths."""
    paths = {prefix} if prefix else set()
    if isinstance(value, dict):
        for key, child in value.items():
            paths.update(_known_paths(child, prefix + (str(key),)))
    return paths


def _collect_line_numbers(node: Node | None, prefix: PathKey = ()) -> dict[PathKey, int]:
    if node is None:
        return {}

    if isinstance(node, MappingNode):
        if not node.value and prefix:
            return {prefix: node.start_mark.line + 1}
        lines: dict[PathKey, int] = {}
        for key_node, value_node in node.value:
            if not isinstance(key_node, ScalarNode):
                continue
            child_path = prefix + (str(key_node.value),)
            lines.update(_collect_line_numbers(value_node, child_path))
        return lines

    if isinstance(node, SequenceNode):
        return {prefix: node.start_mark.line + 1} if prefix else {}

    return {prefix: node.start_mark.line + 1} if prefix else {}


def load_values_file(path: Path) -> tuple[dict[str, Any], dict[PathKey, int]]:
    if not path.is_file():
        raise ValueTraceError(f"Values file not found: {path}")

    try:
        text = path.read_text(encoding="utf-8")
        loaded = yaml.safe_load(text)
        node = yaml.compose(text)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueTraceError(f"Cannot read YAML from {path}: {exc}") from exc

    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ValueTraceError(f"Top-level YAML value must be a mapping: {path}")

    return loaded, _collect_line_numbers(node)


def _apply_mapping(
    destination: dict[str, Any],
    incoming: dict[str, Any],
    source: str,
    lines: dict[PathKey, int],
    history: dict[PathKey, list[Assignment]],
    prefix: PathKey = (),
) -> None:
    for raw_key, incoming_value in incoming.items():
        key = str(raw_key)
        current_path = prefix + (key,)

        if isinstance(incoming_value, dict):
            current_value = destination.get(key, MISSING)
            if not isinstance(current_value, dict):
                destination[key] = {}
                if not incoming_value:
                    history.setdefault(current_path, []).append(
                        Assignment(
                            source=source,
                            line=lines.get(current_path),
                            value={},
                        )
                    )
                    continue

            if not incoming_value:
                if current_value is MISSING:
                    history.setdefault(current_path, []).append(
                        Assignment(
                            source=source,
                            line=lines.get(current_path),
                            value={},
                        )
                    )
                continue

            _apply_mapping(
                destination[key],
                incoming_value,
                source,
                lines,
                history,
                current_path,
            )
            continue

        destination[key] = copy.deepcopy(incoming_value)
        history.setdefault(current_path, []).append(
            Assignment(source=source, line=lines.get(current_path), value=copy.deepcopy(incoming_value))
        )


def _set_nested(target: dict[str, Any], path: PathKey, value: Any) -> None:
    current = target
    for key in path[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[path[-1]] = value


def _parse_helm_set_scalar(raw_value: str) -> Any:
    """Parse a supported --set scalar with Helm's strvals typing rules."""
    normalized = raw_value.lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    if normalized == "null":
        return None
    if raw_value == "0":
        return 0

    # Helm parses base-10 int64 values only when the raw value does not start
    # with zero. Values such as 0123, 1.0, and yes therefore remain strings.
    if raw_value and raw_value[0] != "0":
        digits = raw_value[1:] if raw_value[0] in {"+", "-"} else raw_value
        if digits and all("0" <= character <= "9" for character in digits):
            parsed = int(raw_value, 10)
            if INT64_MIN <= parsed <= INT64_MAX:
                return parsed

    return raw_value


def _split_set_argument(raw: str) -> list[str]:
    parts: list[str] = []
    buffer: list[str] = []
    depth = 0
    escaped = False
    quote: str | None = None

    for character in raw:
        if escaped:
            buffer.append(character)
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if quote:
            buffer.append(character)
            if character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
            buffer.append(character)
            continue
        if character in "[{(":
            depth += 1
        elif character in "]})" and depth:
            depth -= 1
        if character == "," and depth == 0:
            parts.append("".join(buffer).strip())
            buffer = []
        else:
            buffer.append(character)

    if escaped:
        buffer.append("\\")
    if buffer:
        parts.append("".join(buffer).strip())
    return [part for part in parts if part]


def parse_set_arguments(raw_arguments: Iterable[str]) -> list[tuple[PathKey, Any, str]]:
    parsed: list[tuple[PathKey, Any, str]] = []
    for argument_number, raw in enumerate(raw_arguments, start=1):
        for assignment in _split_set_argument(raw):
            if "=" not in assignment:
                raise ValueTraceError(f"Invalid --set value (expected key=value): {assignment}")
            raw_key, raw_value = assignment.split("=", 1)
            path = tuple(part.strip() for part in raw_key.split(".") if part.strip())
            if not path:
                raise ValueTraceError(f"Invalid --set key: {raw_key}")
            value = _parse_helm_set_scalar(raw_value)
            parsed.append((path, value, f"--set[{argument_number}]"))
    return parsed


def _coalesce_values(
    default_values: dict[str, Any],
    user_values: dict[str, Any],
    remove_default_nulls: bool,
) -> dict[str, Any]:
    """Coalesce chart defaults into higher-precedence user values like Helm."""
    result = copy.deepcopy(user_values)

    for key, default_value in default_values.items():
        if key in result:
            user_value = result[key]
            if user_value is None:
                # A user null removes a key that exists in chart defaults.
                del result[key]
            elif isinstance(user_value, dict) and isinstance(default_value, dict):
                result[key] = _coalesce_values(
                    default_value,
                    user_value,
                    remove_default_nulls,
                )
            continue

        if default_value is None and remove_default_nulls:
            continue
        if isinstance(default_value, dict):
            result[key] = _coalesce_values(
                default_value,
                {},
                remove_default_nulls,
            )
        else:
            result[key] = copy.deepcopy(default_value)

    return result


def _flexible_prefixes(default_values: dict[str, Any]) -> set[PathKey]:
    flexible: set[PathKey] = set()

    def walk(value: Any, prefix: PathKey = ()) -> None:
        if isinstance(value, dict):
            if not value and prefix:
                flexible.add(prefix)
                return
            for key, child in value.items():
                walk(child, prefix + (str(key),))

    walk(default_values)
    return flexible


def _is_known_path(path: PathKey, known: set[PathKey], flexible: set[PathKey]) -> bool:
    if path in known:
        return True
    return any(len(path) > len(prefix) and path[: len(prefix)] == prefix for prefix in flexible)


def _path_exists(value: Any, path: PathKey) -> bool:
    current = value
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return False
        current = current[key]
    return True


def trace_values(
    default_values: dict[str, Any],
    default_source: str,
    default_lines: dict[PathKey, int],
    overrides: Iterable[tuple[dict[str, Any], str, dict[PathKey, int]]],
    set_arguments: Iterable[str] = (),
    reference_values: dict[str, Any] | None = None,
    reference_source: str | None = None,
    helm_major_version: int | None = None,
) -> TraceResult:
    normalized_defaults: dict[str, Any] = {}
    user_values: dict[str, Any] = {}
    history: dict[PathKey, list[Assignment]] = {}
    unknown_entries: list[tuple[PathKey, str]] = []

    known = _known_paths(default_values)
    flexible = _flexible_prefixes(default_values)
    if reference_values is not None:
        known.update(_known_paths(reference_values))
        flexible.update(_flexible_prefixes(reference_values))

    # Helm first merges all user-supplied files and --set values, then
    # coalesces chart defaults into that result. Keep history in source order,
    # but do not merge defaults into user values prematurely.
    _apply_mapping(
        normalized_defaults,
        default_values,
        default_source,
        default_lines,
        history,
    )

    for override_values, source, line_numbers in overrides:
        for override_path in flatten_values(override_values):
            if not _is_known_path(override_path, known, flexible):
                unknown_entries.append((override_path, source))
        _apply_mapping(user_values, override_values, source, line_numbers, history)

    for set_path, set_value, source in parse_set_arguments(set_arguments):
        if not _is_known_path(set_path, known, flexible):
            unknown_entries.append((set_path, source))
        _set_nested(user_values, set_path, copy.deepcopy(set_value))
        history.setdefault(set_path, []).append(Assignment(source=source, line=None, value=copy.deepcopy(set_value)))

    merged = _coalesce_values(
        normalized_defaults,
        user_values,
        remove_default_nulls=(helm_major_version or 0) >= 4,
    )

    known_text = sorted(path_text(path) for path in known)
    unknown: list[UnknownValue] = []
    seen: set[tuple[str, str]] = set()
    for unknown_path, source in unknown_entries:
        key = path_text(unknown_path)
        identity = (key, source)
        if identity in seen:
            continue
        seen.add(identity)
        match = difflib.get_close_matches(key, known_text, n=1, cutoff=0.62)
        unknown.append(UnknownValue(key=key, source=source, suggestion=match[0] if match else None))

    missing: list[MissingValue] = []
    if reference_values is not None:
        displayed_reference = reference_source or "reference values"
        for reference_path in sorted(flatten_values(reference_values), key=path_text):
            if not _path_exists(merged, reference_path):
                missing.append(
                    MissingValue(
                        key=path_text(reference_path),
                        reference=displayed_reference,
                    )
                )

    return TraceResult(values=merged, history=history, unknown=unknown, missing=missing)


def final_assignment(path: PathKey, history: dict[PathKey, list[Assignment]]) -> Assignment | None:
    if path in history and history[path]:
        return history[path][-1]
    for length in range(len(path) - 1, 0, -1):
        parent = path[:length]
        if parent in history and history[parent]:
            return history[parent][-1]
    return None
