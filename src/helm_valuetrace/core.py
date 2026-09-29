"""
Core Helm values coalescing and provenance engine.

This module implements Helm-accurate value resolution:
- Default values from chart/values.yaml
- User override files (-f)
- CLI overrides (--set and variants)
- Helm 3/4 null-handling differences

The public API is trace_values() which returns a TraceResult containing
the final merged values, the full assignment history, and analysis results.

This module has no I/O side effects and can be imported and tested without
spawning any external processes.
"""

from __future__ import annotations

import copy
import difflib
from collections.abc import Iterable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .parsers.set_parser import SetParseResult

from .exceptions import ChartError, ValueTraceError
from .models import (
    Assignment,
    MissingValue,
    PathKey,
    SchemaError,
    SourceType,
    SubchartInfo,
    TraceResult,
    UnknownValue,
    path_text,
)
from .parsers.yaml_loader import load_values_file

__all__ = [
    "Assignment",
    "ChartError",
    "MissingValue",
    "PathKey",
    "SchemaError",
    "SourceType",
    "SubchartInfo",
    "TraceResult",
    "UnknownValue",
    "ValueTraceError",
    "final_assignment",
    "flatten_values",
    "load_values_file",
    "parse_set_arguments",
    "path_text",
    "trace_values",
]

MISSING = object()
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


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


def _flexible_prefixes(default_values: dict[str, Any]) -> set[PathKey]:
    flexible: set[PathKey] = set()

    def walk(value: Any, prefix: PathKey = ()) -> None:
        if isinstance(value, dict):
            if not value and prefix:
                flexible.add(prefix)
                return
            for key, child in value.items():
                walk(child, prefix + (str(key),))
        elif isinstance(value, list) and prefix:
            flexible.add(prefix)

    walk(default_values)
    return flexible


def _is_known_path(path: PathKey, known: set[PathKey], flexible: set[PathKey]) -> bool:
    if path in known:
        return True
    return any(len(path) > len(prefix) and path[: len(prefix)] == prefix for prefix in flexible)


def _path_exists(value: Any, path: PathKey) -> bool:
    current = value
    for key in path:
        if isinstance(key, int):
            if not isinstance(current, list) or not (0 <= key < len(current)):
                return False
            current = current[key]
        else:
            if not isinstance(current, dict) or key not in current:
                return False
            current = current[key]
    return True


class ProvenanceHistory(dict[PathKey, list[Assignment]]):
    """Assignment history plus independent effective-source state."""

    def __init__(self) -> None:
        super().__init__()
        self.active: dict[PathKey, Assignment] = {}
        self.defaults: dict[PathKey, Assignment] = {}
        self.winners: dict[PathKey, Assignment | None] = {}
        self.origins: dict[PathKey, Assignment | None] = {}
        self.contributors: dict[PathKey, list[Assignment]] = {}
        self.generated: dict[PathKey, Assignment] = {}
        self.events: list[tuple[PathKey, Assignment]] = []
        self._descendants: dict[PathKey, set[PathKey]] = {}
        self._order: dict[int, int] = {}
        self._next_order = 0
        self._event_descendants: dict[PathKey, list[int]] = {}
        self._event_exact: dict[PathKey, list[int]] = {}

    def _index(self, path: PathKey, assignment: Assignment) -> None:
        self._order[id(assignment)] = self._next_order
        self._next_order += 1
        for length in range(1, len(path) + 1):
            self._descendants.setdefault(path[:length], set()).add(path)

    def replace(self, path: PathKey) -> None:
        for key in tuple(self._descendants.get(path, ())):
            self.active.pop(key, None)
            self.generated.pop(key, None)
            for length in range(1, len(key) + 1):
                prefix = key[:length]
                descendants = self._descendants[prefix]
                descendants.discard(key)
                if not descendants:
                    del self._descendants[prefix]

    def record(self, path: PathKey, assignment: Assignment) -> None:
        self.setdefault(path, []).append(assignment)
        self.active[path] = assignment
        position = len(self.events)
        self.events.append((path, assignment))
        self._event_exact.setdefault(path, []).append(position)
        for length in range(1, len(path) + 1):
            self._event_descendants.setdefault(path[:length], []).append(position)
        self._index(path, assignment)

    def related_events(self, path: PathKey) -> list[tuple[PathKey, Assignment]]:
        """Return explicit ancestor/descendant writes in original order."""
        positions = list(self._event_descendants.get(path, ()))
        for length in range(1, len(path)):
            positions.extend(self._event_exact.get(path[:length], ()))
        return [self.events[index] for index in sorted(positions)]

    def record_generated(self, path: PathKey, assignment: Assignment) -> None:
        self.generated[path] = assignment
        self._index(path, assignment)

    def begin_overrides(self) -> None:
        self.defaults = self.active.copy()
        self.active.clear()
        self._descendants.clear()


def _record(
    history: dict[PathKey, list[Assignment]], path: PathKey, assignment: Assignment
) -> None:
    if isinstance(history, ProvenanceHistory):
        history.record(path, assignment)
    else:
        history.setdefault(path, []).append(assignment)


def _lookup(value: Any, path: PathKey) -> Any:
    for segment in path:
        if not isinstance(value, list if isinstance(segment, int) else dict):
            return MISSING
        try:
            value = value[segment]
        except (KeyError, IndexError, TypeError):
            return MISSING
    return value


def _resolve_winners(
    history: ProvenanceHistory,
    merged: dict[str, Any],
    user_values: dict[str, Any],
    propagated_origins: dict[PathKey, Assignment] | None = None,
) -> None:
    history.winners.clear()
    history.origins.clear()
    history.contributors.clear()

    def order(assignment: Assignment) -> int:
        return history._order.get(id(assignment), -1)

    def source_at(active: dict[PathKey, Assignment], path: PathKey) -> Assignment | None:
        candidates = []
        for length in range(1, len(path) + 1):
            assignment = active.get(path[:length])
            if assignment is not None and _lookup(assignment.value, path[length:]) is not MISSING:
                candidates.append(assignment)
        return max(candidates, key=order, default=None)

    structural: dict[PathKey, Assignment] = {}
    for active in (history.defaults, history.active):
        for key, assignment in active.items():
            for length in range(1, len(key) + 1):
                prefix = key[:length]
                if prefix not in structural or order(assignment) > order(structural[prefix]):
                    structural[prefix] = assignment

    def walk(value: Any, path: PathKey = ()) -> list[Assignment]:
        contributors: dict[int, Assignment] = {}
        children = (
            value.items()
            if isinstance(value, dict)
            else enumerate(value)
            if isinstance(value, list)
            else ()
        )
        for key, child in children:
            for assignment in walk(child, path + (key,)):
                contributors[id(assignment)] = assignment
        if path:
            if not contributors:
                propagated = (propagated_origins or {}).get(path)
                is_user = _lookup(user_values, path) is not MISSING or path in history.active
                active = history.active if is_user else history.defaults
                winner = propagated or source_at(active, path)
                generated = history.generated.get(path)
                if (
                    propagated is None
                    and generated is not None
                    and (winner is None or order(generated) > order(winner))
                ):
                    winner = generated
                if propagated is None and isinstance(value, (dict, list)):
                    structure_source = structural.get(path)
                    if structure_source is not None and (
                        winner is None or order(structure_source) > order(winner)
                    ):
                        winner = structure_source
                if winner is not None:
                    contributors[id(winner)] = winner
            ordered = sorted(contributors.values(), key=order)
            winner = ordered[-1] if ordered else None
            history.contributors[path] = ordered
            history.origins[path] = winner
            history.winners[path] = replace(winner, value=copy.deepcopy(value)) if winner else None
        return list(contributors.values())

    walk(merged)


def _make_assignment(
    source: str,
    source_type: SourceType,
    lines: dict[PathKey, int],
    cols: dict[PathKey, int],
    path: PathKey,
    value: Any,
) -> Assignment:
    """Create an Assignment with location metadata where available."""
    return Assignment(
        source=source,
        source_type=source_type,
        line=lines.get(path),
        column=cols.get(path),
        value=value,
    )


def _copy_value_tree(value: Any) -> Any:
    """Copy loaded values without retaining shared YAML alias identities.

    Helm's YAML-to-JSON conversion expands aliases. A memoized deepcopy would
    preserve shared list elements and let an indexed override mutate siblings.
    The loader enforces depth and expanded-node limits before this operation.
    """
    if isinstance(value, dict):
        return {key: _copy_value_tree(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_copy_value_tree(child) for child in value]
    return copy.deepcopy(value)


def _apply_mapping(
    destination: dict[str, Any],
    incoming: dict[str, Any],
    source: str,
    source_type: SourceType,
    lines: dict[PathKey, int],
    cols: dict[PathKey, int],
    history: dict[PathKey, list[Assignment]],
    prefix: PathKey = (),
) -> None:
    for raw_key, incoming_value in incoming.items():
        key = str(raw_key)
        current_path = prefix + (key,)

        if isinstance(history, ProvenanceHistory) and not (
            isinstance(incoming_value, dict) and isinstance(destination.get(key), dict)
        ):
            history.replace(current_path)

        if isinstance(incoming_value, dict):
            current_value = destination.get(key, MISSING)
            if not isinstance(current_value, dict):
                destination[key] = {}
                if not incoming_value:
                    _record(
                        history,
                        current_path,
                        _make_assignment(source, source_type, lines, cols, current_path, {}),
                    )
                    continue

            if not incoming_value:
                if current_value is MISSING:
                    _record(
                        history,
                        current_path,
                        _make_assignment(source, source_type, lines, cols, current_path, {}),
                    )
                continue

            _apply_mapping(
                destination[key],
                incoming_value,
                source,
                source_type,
                lines,
                cols,
                history,
                current_path,
            )
            continue

        destination[key] = _copy_value_tree(incoming_value)
        _record(
            history,
            current_path,
            _make_assignment(
                source,
                source_type,
                lines,
                cols,
                current_path,
                copy.deepcopy(incoming_value),
            ),
        )


def _set_nested(
    target: dict[str, Any],
    path: PathKey,
    value: Any,
    history: ProvenanceHistory | None = None,
    assignment: Assignment | None = None,
) -> None:
    """Set a nested value in *target* following *path*.

    Supports mixed str/int path segments — int segments index into lists,
    growing them with None placeholders when necessary.
    """
    current: Any = target
    for i, key in enumerate(path):
        expected = list if isinstance(key, int) else dict
        if not isinstance(current, expected):
            raise ValueTraceError("Incompatible container type in --set path")
        if isinstance(key, int):
            if key < 0 or key > 65536:
                raise ValueTraceError("Array index must be between 0 and 65536")
            while len(current) <= key:
                if len(current) < key and history is not None and assignment is not None:
                    history.record_generated(
                        path[:i] + (len(current),), replace(assignment, value=None)
                    )
                current.append(None)
        if i == len(path) - 1:
            current[key] = value
            return
        next_type = list if isinstance(path[i + 1], int) else dict
        existing = current[key] if isinstance(current, list) else current.get(key, MISSING)
        if existing is MISSING or (existing is None and isinstance(current, list)):
            current[key] = next_type()
            if history is not None:
                history.replace(path[: i + 1])
        elif not isinstance(existing, next_type):
            if isinstance(current, list) and next_type is dict:
                current[key] = {}
                if history is not None:
                    history.replace(path[: i + 1])
            else:
                raise ValueTraceError("Incompatible container type in --set path")
        current = current[key]


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

    if raw_value and raw_value[0] != "0":
        digits = raw_value[1:] if raw_value[0] in {"+", "-"} else raw_value
        if digits and all("0" <= character <= "9" for character in digits):
            parsed = int(raw_value, 10)
            if INT64_MIN <= parsed <= INT64_MAX:
                return parsed

    return raw_value


def _split_set_argument(raw: str) -> list[str]:
    """Split a raw --set argument on commas respecting brackets and escapes."""
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
    """Parse a sequence of raw --set strings into (path, value, source_label) tuples.

    Delegates to parsers.set_parser.parse_set() for unified Helm strvals parsing.
    """
    from .parsers.set_parser import parse_set

    parsed: list[tuple[PathKey, Any, str]] = []
    for entry in parse_set(list(raw_arguments)):
        parsed.append((entry.path, entry.value, entry.source_label))
    return parsed


def _coalesce_values(
    default_values: dict[str, Any],
    user_values: dict[str, Any],
    remove_default_nulls: bool,
    nested: bool = False,
) -> dict[str, Any]:
    """Coalesce chart defaults into higher-precedence user values like Helm."""
    result = copy.deepcopy(user_values)

    for key, default_value in default_values.items():
        if key in result:
            user_value = result[key]
            if user_value is None and (not nested or default_value is not None):
                del result[key]
            elif isinstance(user_value, dict) and isinstance(default_value, dict):
                result[key] = _coalesce_values(
                    default_value,
                    user_value,
                    remove_default_nulls,
                    nested=True,
                )
            continue

        if default_value is None and remove_default_nulls:
            continue
        if isinstance(default_value, dict):
            result[key] = _coalesce_values(
                default_value,
                {},
                remove_default_nulls,
                nested=True,
            )
        else:
            result[key] = copy.deepcopy(default_value)

    return result


def trace_values(
    default_values: dict[str, Any],
    default_source: str,
    default_lines: dict[PathKey, int],
    overrides: Iterable[tuple[dict[str, Any], str, dict[PathKey, int]]],
    set_arguments: Iterable[str] = (),
    reference_values: dict[str, Any] | None = None,
    reference_source: str | None = None,
    helm_major_version: int | None = None,
    default_cols: dict[PathKey, int] | None = None,
    extra_set_entries: list[SetParseResult] | None = None,
    subcharts: list[SubchartInfo] | None = None,
) -> TraceResult:
    """Resolve and trace Helm values following Helm's coalescing semantics.

    Args:
        default_values:     Values from the chart's values.yaml.
        default_source:     Human-readable label for the defaults file.
        default_lines:      Line-number map for the defaults file.
        overrides:          Sequence of (values_dict, source_label, lines_dict) tuples.
        set_arguments:      Raw --set strings (comma-separated key=value pairs).
        reference_values:   Optional structure used for missing-key detection.
        reference_source:   Human-readable label for the reference file.
        helm_major_version: 3 or 4 to control null-default behavior.
        default_cols:       Optional column-number map for the defaults file.
        extra_set_entries:  Pre-parsed SetParseResult objects from --set-string,
                            --set-file, --set-json, --set-literal.  Applied after
                            the regular --set entries, in the order supplied.
        subcharts:          Optional list of SubchartInfo objects discovered in charts/.

    Returns:
        TraceResult with the final merged values and provenance history.
    """
    normalized_defaults: dict[str, Any] = {}
    user_values: dict[str, Any] = {}
    history = ProvenanceHistory()
    unknown_entries: list[tuple[PathKey, str]] = []

    _default_cols: dict[PathKey, int] = default_cols or {}

    known = _known_paths(default_values)
    flexible = _flexible_prefixes(default_values)
    if reference_values is not None:
        known.update(_known_paths(reference_values))
        flexible.update(_flexible_prefixes(reference_values))

    if subcharts:
        for sc in subcharts:
            known.add(sc.path_prefix)
            for p in _known_paths(sc.values):
                known.add(sc.path_prefix + p)
            for p in _flexible_prefixes(sc.values):
                flexible.add(sc.path_prefix + p)

        all_global_subpaths = {p[1:] for p in known if p and p[0] == "global"}
        for sc in subcharts:
            for p in _known_paths(sc.values):
                if p and p[0] == "global":
                    all_global_subpaths.add(p[1:])
        for gp in all_global_subpaths:
            known.add(("global",) + gp)
            for sc in subcharts:
                known.add(sc.path_prefix + ("global",) + gp)

    if subcharts:
        for sc in subcharts:
            target = normalized_defaults
            for seg in sc.path_prefix:
                if seg not in target or not isinstance(target[seg], dict):
                    target[seg] = {}
                target = target[seg]
            prefixed_lines = {sc.path_prefix + p: line for p, line in sc.lines.items()}
            prefixed_cols = {sc.path_prefix + p: col for p, col in sc.cols.items()}
            _apply_mapping(
                target,
                sc.values,
                sc.source_label,
                SourceType.DEFAULT,
                prefixed_lines,
                prefixed_cols,
                history,
                prefix=sc.path_prefix,
            )

    _apply_mapping(
        normalized_defaults,
        default_values,
        default_source,
        SourceType.DEFAULT,
        default_lines,
        _default_cols,
        history,
    )

    history.begin_overrides()

    for override_values, source, line_numbers in overrides:
        for override_path in flatten_values(override_values):
            if not _is_known_path(override_path, known, flexible):
                unknown_entries.append((override_path, source))
        _apply_mapping(
            user_values,
            override_values,
            source,
            SourceType.VALUES_FILE,
            line_numbers,
            {},
            history,
        )

    for set_path, set_value, source in parse_set_arguments(set_arguments):
        history.replace(set_path)
        _set_nested(
            user_values,
            set_path,
            copy.deepcopy(set_value),
            history,
            Assignment(source, SourceType.SET, None, None, set_value),
        )
        flat = flatten_values(set_value, prefix=set_path)
        if not flat:
            flat = {set_path: set_value}
        for p, v in flat.items():
            if not _is_known_path(p, known, flexible):
                unknown_entries.append((p, source))
            _record(
                history,
                p,
                Assignment(
                    source=source,
                    source_type=SourceType.SET,
                    line=None,
                    column=None,
                    value=copy.deepcopy(v),
                ),
            )
        if set_path not in flat:
            if not _is_known_path(set_path, known, flexible):
                unknown_entries.append((set_path, source))
            _record(
                history,
                set_path,
                Assignment(
                    source=source,
                    source_type=SourceType.SET,
                    line=None,
                    column=None,
                    value=copy.deepcopy(set_value),
                ),
            )

    for entry in extra_set_entries or []:
        history.replace(entry.path)
        _set_nested(
            user_values,
            entry.path,
            copy.deepcopy(entry.value),
            history,
            Assignment(entry.source_label, entry.source_type, None, None, entry.value),
        )
        flat = flatten_values(entry.value, prefix=entry.path)
        if not flat:
            flat = {entry.path: entry.value}
        for p, v in flat.items():
            if not _is_known_path(p, known, flexible):
                unknown_entries.append((p, entry.source_label))
            _record(
                history,
                p,
                Assignment(
                    source=entry.source_label,
                    source_type=entry.source_type,
                    line=None,
                    column=None,
                    value=copy.deepcopy(v),
                ),
            )
        if entry.path not in flat:
            if not _is_known_path(entry.path, known, flexible):
                unknown_entries.append((entry.path, entry.source_label))
            _record(
                history,
                entry.path,
                Assignment(
                    source=entry.source_label,
                    source_type=entry.source_type,
                    line=None,
                    column=None,
                    value=copy.deepcopy(entry.value),
                ),
            )

    merged = _coalesce_values(
        normalized_defaults,
        user_values,
        remove_default_nulls=(helm_major_version or 0) >= 4,
    )

    propagated_origins: dict[PathKey, Assignment] = {}
    if subcharts:
        _resolve_winners(history, merged, user_values)
        root_origins = {
            path: origin
            for path, origin in history.origins.items()
            if path[0] == "global" and origin is not None
        }
        root_events = [(path, a) for path, a in history.events if path[0] == "global"]
        for sc in subcharts:
            target = merged
            for seg in sc.path_prefix:
                if seg not in target or not isinstance(target[seg], dict):
                    target[seg] = {}
                target = target[seg]
            target.setdefault("global", {})

        root_global = merged.get("global")
        if isinstance(root_global, dict):
            for sc in subcharts:
                copies: dict[int, Assignment] = {}
                for root_path, assignment in root_events:
                    propagated = copy.deepcopy(assignment)
                    copies[id(assignment)] = propagated
                    history.record(sc.path_prefix + root_path, propagated)
                for root_path, origin in root_origins.items():
                    copied = copies.get(id(origin))
                    if copied is None:
                        copied = copy.deepcopy(origin)
                        copies[id(origin)] = copied
                        history.record_generated(sc.path_prefix + root_path, copied)
                    propagated_origins[sc.path_prefix + root_path] = copied
                for g_path, g_val in flatten_values(root_global).items():
                    full_sc_path = sc.path_prefix + ("global",) + g_path
                    _set_nested(merged, full_sc_path, copy.deepcopy(g_val))

    _resolve_winners(history, merged, user_values, propagated_origins)

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


_SOURCE_TYPE_PRECEDENCE = {
    SourceType.DEFAULT: 1,
    SourceType.DEPENDENCY: 2,
    SourceType.GLOBAL: 3,
    SourceType.VALUES_FILE: 4,
    SourceType.SET: 5,
    SourceType.SET_STRING: 5,
    SourceType.SET_FILE: 5,
    SourceType.SET_JSON: 5,
    SourceType.SET_LITERAL: 5,
}


def final_assignment(path: PathKey, history: dict[PathKey, list[Assignment]]) -> Assignment | None:
    """Return the last (winning) assignment for a path, or a parent's last assignment."""
    if isinstance(history, ProvenanceHistory):
        return history.winners.get(path)
    direct = history.get(path, [])
    last_direct = direct[-1] if direct else None

    child_assignments = [
        assignments[-1]
        for key, assignments in history.items()
        if len(key) > len(path) and key[: len(path)] == path and assignments
    ]
    best_child = None
    if child_assignments:
        best_child = max(
            reversed(child_assignments),
            key=lambda a: _SOURCE_TYPE_PRECEDENCE.get(a.source_type, 0),
        )

    if last_direct and best_child:
        if _SOURCE_TYPE_PRECEDENCE.get(best_child.source_type, 0) >= _SOURCE_TYPE_PRECEDENCE.get(
            last_direct.source_type, 0
        ):
            return best_child
        return last_direct
    if last_direct:
        return last_direct
    if best_child:
        return best_child

    for length in range(len(path) - 1, 0, -1):
        parent = path[:length]
        if history.get(parent):
            return history[parent][-1]
    return None
