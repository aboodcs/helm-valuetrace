"""
YAML loader with line and column tracking for Helm values files.

Preserves structural location metadata (1-indexed line and column numbers)
for leaf keys and mappings so that provenance reporting can pinpoint exact
source locations.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from helm_valuetrace.exceptions import ValueTraceError
from helm_valuetrace.models import PathKey


def collect_location_info(
    node: Node | None,
    prefix: PathKey = (),
) -> tuple[dict[PathKey, int], dict[PathKey, int]]:
    """Recursively collect line and column numbers from a YAML node tree.

    Returns two dicts: lines (1-indexed) and columns (1-indexed).
    Both use the same PathKey. Values are collected at the deepest leaf
    that has location information.
    """
    if node is None:
        return {}, {}

    if isinstance(node, MappingNode):
        if not node.value and prefix:
            line = node.start_mark.line + 1
            col = node.start_mark.column + 1
            return {prefix: line}, {prefix: col}
        lines: dict[PathKey, int] = {}
        cols: dict[PathKey, int] = {}
        for key_node, value_node in node.value:
            if not isinstance(key_node, ScalarNode):
                continue
            child_path = prefix + (str(key_node.value),)
            child_lines, child_cols = collect_location_info(value_node, child_path)
            lines.update(child_lines)
            cols.update(child_cols)
        return lines, cols

    if isinstance(node, SequenceNode):
        lines, cols = {}, {}
        if prefix:
            lines[prefix] = node.start_mark.line + 1
            cols[prefix] = node.start_mark.column + 1
        for index, child in enumerate(node.value):
            child_lines, child_cols = collect_location_info(child, (*prefix, index))
            lines.update(child_lines)
            cols.update(child_cols)
        return lines, cols

    if prefix:
        line = node.start_mark.line + 1
        col = node.start_mark.column + 1
        return {prefix: line}, {prefix: col}
    return {}, {}


_collect_location_info = collect_location_info


def collect_line_numbers(node: Node | None, prefix: PathKey = ()) -> dict[PathKey, int]:
    """Collect line numbers only (backward-compatible helper)."""
    lines, _ = collect_location_info(node, prefix)
    return lines


_collect_line_numbers = collect_line_numbers


MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_DEPTH = 64
MAX_NODES = 100_000


class _ValuesLoader(yaml.SafeLoader):
    """Safe YAML construction with bounded composition and Helm scalar rules."""

    def __init__(self, stream: str):
        super().__init__(stream)
        self._depth = 0
        self._nodes = 0

    def resolve(self, kind, value, implicit):
        tag = super().resolve(kind, value, implicit)
        if tag == "tag:yaml.org,2002:float" and implicit[0]:
            clean = value.replace("_", "")
            if "e" in clean.lower() and not math.isfinite(float(clean)):
                return "tag:yaml.org,2002:str"
        return tag

    def compose_node(self, parent, index):
        self._depth += 1
        self._nodes += 1
        try:
            if self._depth > MAX_DEPTH or self._nodes > MAX_NODES:
                raise ValueTraceError("YAML exceeds depth or node limit")
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1


def _construct_number(loader: _ValuesLoader, node: ScalarNode):
    raw = loader.construct_scalar(node)
    clean = raw.replace("_", "")
    try:
        if node.tag.endswith(":int"):
            signless = clean.lstrip("+-")
            base = 0 if signless.lower().startswith(("0x", "0b", "0o")) else 10
            if len(signless) > 1 and signless.startswith("0") and base == 10:
                base = 8
            value = int(clean, base)
            if abs(value) > 2**53:
                raise ValueTraceError(
                    "YAML integers outside the exact float64 range are unsupported"
                )
            return value
        value = float(clean)
        if not math.isfinite(value):
            raise ValueTraceError("Non-finite YAML numbers are unsupported")
        return value
    except (ValueError, OverflowError):
        raise ValueTraceError("Invalid or unsupported YAML numeric value") from None


def _construct_binary(loader: _ValuesLoader, node: ScalarNode):
    return loader.construct_yaml_binary(node).decode("utf-8", errors="replace")


_ValuesLoader.yaml_implicit_resolvers = {
    key: [
        (tag, regex)
        for tag, regex in entries
        if tag
        not in {"tag:yaml.org,2002:timestamp", "tag:yaml.org,2002:int", "tag:yaml.org,2002:float"}
    ]
    for key, entries in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_ValuesLoader.add_implicit_resolver(
    "tag:yaml.org,2002:int",
    re.compile(r"^[+-]?(?:0[bB][01_]+|0[xX][0-9a-fA-F_]+|0[oO][0-7_]+|0[0-7_]+|[1-9][0-9_]*|0)$"),
    list("-+0123456789"),
)
_ValuesLoader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(
        r"^[+-]?(?:[0-9][0-9_]*(?:\.[0-9_]*)?(?:[eE][+-]?[0-9][0-9_]*)?|\.[0-9_]+(?:[eE][+-]?[0-9][0-9_]*)?|\.(?:inf|Inf|INF|nan|NaN|NAN))$"
    ),
    list("-+0123456789."),
)
_ValuesLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:y|Y|n|N)$"), list("yYnN")
)
_ValuesLoader.bool_values = {**yaml.SafeLoader.bool_values, "y": True, "n": False}
_ValuesLoader.add_constructor("tag:yaml.org,2002:int", _construct_number)
_ValuesLoader.add_constructor("tag:yaml.org,2002:float", _construct_number)
_ValuesLoader.add_constructor("tag:yaml.org,2002:timestamp", yaml.SafeLoader.construct_scalar)
_ValuesLoader.add_constructor("tag:yaml.org,2002:binary", _construct_binary)


def _validate_node_graph(node: Node | None) -> None:
    """Bound expanded aliases before construction, and reject non-JSON keys."""
    if node is None:
        return
    active: set[int] = set()
    stack = [(node, 0, False)]
    count = 0
    while stack:
        current, depth, exiting = stack.pop()
        if exiting:
            active.remove(id(current))
            continue
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise ValueTraceError("YAML exceeds expanded node or depth limit")
        if id(current) in active:
            raise ValueTraceError("Recursive YAML aliases are unsupported")
        if current.tag not in {
            "tag:yaml.org,2002:map",
            "tag:yaml.org,2002:seq",
            "tag:yaml.org,2002:str",
            "tag:yaml.org,2002:null",
            "tag:yaml.org,2002:bool",
            "tag:yaml.org,2002:int",
            "tag:yaml.org,2002:float",
            "tag:yaml.org,2002:timestamp",
            "tag:yaml.org,2002:binary",
        }:
            raise ValueTraceError("Unsupported YAML tag")
        active.add(id(current))
        stack.append((current, depth, True))
        if isinstance(current, MappingNode):
            for key, value in reversed(current.value):
                if not isinstance(key, ScalarNode) or key.tag not in {
                    "tag:yaml.org,2002:str",
                    "tag:yaml.org,2002:merge",
                }:
                    raise ValueTraceError("YAML mapping keys must be strings")
                stack.append((value, depth + 1, False))
        elif isinstance(current, SequenceNode):
            stack.extend((child, depth + 1, False) for child in reversed(current.value))


def load_values_file(
    path: Path,
) -> tuple[dict[str, Any], dict[PathKey, int], dict[PathKey, int]]:
    """Load a bounded, JSON-compatible YAML mapping with reliable locations.

    Multi-document YAML files (multiple ``---`` separators) are supported.
    All non-empty mapping documents are merged in order; later documents
    override earlier ones for duplicate keys, matching Helm's behaviour.
    """
    if not path.is_file():
        raise ValueTraceError(f"Values file not found: {path}")

    loader = None
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueTraceError("YAML file exceeds 8 MiB size limit")
        text = raw.decode("utf-8")
        loader = _ValuesLoader(text)

        merged: dict[str, Any] = {}
        merged_lines: dict[PathKey, int] = {}
        merged_cols: dict[PathKey, int] = {}

        while loader.check_node():
            node = loader.get_node()
            if node is None:
                continue
            _validate_node_graph(node)
            doc = loader.construct_document(node)
            if doc is None:
                continue
            if not isinstance(doc, dict):
                raise ValueTraceError(f"Top-level YAML value must be a mapping: {path}")
            doc_lines, doc_cols = collect_location_info(node)
            merged.update(doc)
            merged_lines.update(doc_lines)
            merged_cols.update(doc_cols)

        lines, cols = merged_lines, merged_cols
        loaded = merged if merged else None

    except UnicodeError:
        raise ValueTraceError(f"Values file must be UTF-8: {path}") from None
    except OSError as exc:
        raise ValueTraceError(f"Cannot read file {path}: {exc.strerror}") from None
    except (yaml.YAMLError, RecursionError) as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise ValueTraceError(f"Cannot parse YAML from {path}{location}") from None
    finally:
        if loader is not None:
            loader.dispose()

    if loaded is None:
        loaded = {}
    return loaded, lines, cols
