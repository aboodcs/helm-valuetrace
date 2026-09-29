"""
Helm-compatible --set-* argument parser.

This module implements the tested subset of Helm strvals parsing for:
  --set           key=value (with type coercion per Helm strvals rules)
  --set-string    key=value (always string, no type coercion)
  --set-file      key=path  (file contents as string)
  --set-json      key=json  (JSON-parsed value)
  --set-literal   key=value (literal string, alias for --set-string without escaping)

Helm key path parsing supports:
  - Simple dotted paths:   image.repository
  - Array indexing:        servers[0].port  →  path ("servers", 0, "port")
  - Escaped dots:          annotations.example\\.com/key  →  key "annotations.example.com/key"
  - Escaped brackets:      a\\[0\\]=val  →  key "a[0]"
  - Brace lists:           names={a,b,c}  →  ["a", "b", "c"]
  - Multiple assignments:  key1=val1,key2=val2

Reference: https://helm.sh/docs/intro/using_helm/#the-format-and-limitations-of---set

IMPORTANT: Behavior is verified against actual Helm in the differential test suite.
"""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any

from helm_valuetrace.exceptions import ValueTraceError
from helm_valuetrace.models import PathKey, SourceType

MAX_INPUT_BYTES = 8 * 1024 * 1024

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


class SetParseResult:
    """Represents one parsed --set-* assignment."""

    __slots__ = ("path", "source_label", "source_type", "value")

    def __init__(
        self,
        path: PathKey,
        value: Any,
        source_label: str,
        source_type: SourceType,
    ) -> None:
        self.path = path
        self.value = value
        self.source_label = source_label
        self.source_type = source_type

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"SetParseResult(path={self.path!r}, value={self.value!r}, "
            f"source_label={self.source_label!r}, source_type={self.source_type!r})"
        )


def parse_helm_scalar(raw_value: str) -> Any:
    """Coerce a raw --set value using Helm's strvals typing rules.

    Rules (matching Helm source code behavior):
    - "true"/"TRUE"/"True" → True
    - "false"/"FALSE"/"False" → False
    - "null"/"NULL"/"Null" → None
    - "0" → 0 (integer)
    - Integers that don't start with 0 and fit in int64 → int
    - Everything else → str (including "0123", "1.0", "yes")
    """
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
        if digits and len(digits.lstrip("0")) <= 19 and all("0" <= ch <= "9" for ch in digits):
            parsed = int(
                ("-" if raw_value.startswith("-") else "") + (digits.lstrip("0") or "0"), 10
            )
            if INT64_MIN <= parsed <= INT64_MAX:
                return parsed

    return raw_value


def _parse_key_path(raw_key: str, *, literal: bool = False) -> PathKey:
    """Parse a Helm --set key into a typed path tuple.

    Handles:
      image.repository   → ("image", "repository")
      servers[0].port    → ("servers", 0, "port")
      a\\.b              → ("a.b",)           # escaped dot
      a[0][1]            → ("a", 0, 1)
      annotations.example\\.com/key → ("annotations.example.com/key",)

    The parsing follows Helm's strvals.parseKey:
    1. Read characters, building the current segment
    2. On '\\' consume the next character literally (escaped)
    3. On '[' start an index segment; read digits until ']'
    4. On '.' start a new segment
    """
    if not raw_key:
        raise ValueTraceError("Empty --set key")

    segments: list[str | int] = []
    buffer: list[str] = []
    i = 0
    length = len(raw_key)

    while i < length:
        ch = raw_key[i]

        if ch == "\\" and not literal:
            if i + 1 < length:
                buffer.append(raw_key[i + 1])
                i += 2
            else:
                buffer.append("\\")
                i += 1
            continue

        if ch == "[":
            segment_str = "".join(buffer)
            buffer = []
            if segment_str:
                segments.append(segment_str)

            i += 1
            closing = raw_key.find("]", i)
            if closing == -1:
                raise ValueTraceError("Unclosed array index in --set key")
            digits = raw_key[i:closing]
            unsigned = digits[1:] if digits.startswith(("+", "-")) else digits
            if not unsigned or not all("0" <= ch <= "9" for ch in unsigned):
                raise ValueTraceError("Invalid array index in --set key")
            significant = unsigned.lstrip("0") or "0"
            if len(significant) > 5:
                raise ValueTraceError("Array index exceeds Helm limit 65536")
            index = int(significant) * (-1 if digits.startswith("-") else 1)
            if not 0 <= index <= 65536:
                raise ValueTraceError("Array index must be between 0 and 65536")
            i = closing
            segments.append(index)
            i += 1

            if i < length and raw_key[i] == ".":
                i += 1
                if i == length or raw_key[i] == ".":
                    raise ValueTraceError("Empty path segments are unsupported")
            elif i < length and raw_key[i] != "[":
                raise ValueTraceError("Expected dot or index after closing bracket")
            continue

        if ch == ".":
            segment_str = "".join(buffer)
            buffer = []
            if not segment_str or i == length - 1:
                raise ValueTraceError("Empty path segments are unsupported")
            segments.append(segment_str)
            i += 1
            continue

        buffer.append(ch)
        i += 1

    segment_str = "".join(buffer)
    if segment_str:
        segments.append(segment_str)

    if not segments:
        raise ValueTraceError(f"Invalid --set key (no segments): '{raw_key}'")

    if len(segments) > 31 or isinstance(segments[0], int):
        raise ValueTraceError("Unsupported or excessively nested --set path")
    return tuple(segments)


def _unescape_value(raw: str) -> str:
    """Unescape backslash escapes in a value: \\X -> X."""
    res: list[str] = []
    escaped = False
    for ch in raw:
        if escaped:
            res.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        else:
            res.append(ch)
    return "".join(res)


def _parse_brace_list(raw_value: str) -> list[str]:
    """Parse a brace-list value like {a,b,c} into ["a", "b", "c"].

    Helm's strvals brace-list parsing supports escaped commas: {a,b\\,c} -> ["a", "b,c"].
    """
    inner = raw_value[1:-1]
    if not inner:
        return [""]
    items: list[str] = []
    buf: list[str] = []
    escaped = False
    for ch in inner:
        if escaped:
            buf.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ",":
            items.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if escaped:
        buf.append("\\")
    items.append("".join(buf))
    return items


def _split_assignments(raw: str) -> list[str]:
    """Split strvals at unescaped commas, except an initial brace list."""
    _check_input_size(raw)
    parts = []
    start = 0
    escaped = False
    in_value = False
    brace = False
    first_value = False
    for i, ch in enumerate(raw):
        if escaped:
            escaped = False
            first_value = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == "=" and not in_value:
            in_value = True
            first_value = True
            continue
        if first_value:
            brace = ch == "{"
            first_value = False
        elif brace and ch == "}":
            brace = False
            if i + 1 < len(raw) and raw[i + 1] != ",":
                raise ValueTraceError("Unexpected text after brace list")
        if ch == "," and not brace:
            if i == start:
                raise ValueTraceError("Empty --set assignment")
            parts.append(raw[start:i])
            start = i + 1
            in_value = False
    if brace:
        raise ValueTraceError("Unclosed brace list")
    if start < len(raw):
        parts.append(raw[start:])
    return parts


def _parse_set_value(raw_value: str, coerce: bool = True) -> Any:
    """Parse the value side of a --set assignment.

    If raw_value starts with '{' and ends with '}', it is a brace list.
    Otherwise it is a scalar subject to Helm type coercion (if coerce=True).
    """
    if raw_value.startswith("{") and raw_value.endswith("}"):
        items = _parse_brace_list(raw_value)
        return [parse_helm_scalar(v) for v in items] if coerce else items
    unescaped = _unescape_value(raw_value)
    if coerce:
        return parse_helm_scalar(unescaped)
    return unescaped


def _split_key_value(raw: str) -> tuple[str, str]:
    escaped = False
    for i, ch in enumerate(raw):
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == "=":
            return raw[:i], raw[i + 1 :]
    raise ValueTraceError("Expected an unescaped equals sign in assignment")


def parse_set(
    raw_arguments: Iterable[str],
    source_prefix: str = "--set",
) -> list[SetParseResult]:
    """Parse ``--set key=value[,key=value...]`` arguments.

    Type coercion follows Helm's strvals rules.
    Supports dotted paths, array indexes, escaped dots, brace lists.
    """
    results: list[SetParseResult] = []
    for arg_index, raw in enumerate(raw_arguments, start=1):
        for assignment in _split_assignments(raw):
            if "=" not in assignment:
                raise ValueTraceError(f"Invalid {source_prefix} value (expected key=value)")
            raw_key, raw_value = _split_key_value(assignment)
            path = _parse_key_path(raw_key)
            value = _parse_set_value(raw_value, coerce=True)
            results.append(
                SetParseResult(
                    path=path,
                    value=copy.deepcopy(value),
                    source_label=f"{source_prefix}[{arg_index}]",
                    source_type=SourceType.SET,
                )
            )
    return results


def parse_set_string(raw_arguments: Iterable[str]) -> list[SetParseResult]:
    """Parse ``--set-string key=value`` arguments.

    Values are always preserved as strings — no type coercion.
    Escaped characters (such as \\,) are unescaped to match Helm strvals behavior.
    """
    results: list[SetParseResult] = []
    for arg_index, raw in enumerate(raw_arguments, start=1):
        for assignment in _split_assignments(raw):
            if "=" not in assignment:
                raise ValueTraceError("Invalid --set-string value (expected key=value)")
            raw_key, raw_value = _split_key_value(assignment)
            path = _parse_key_path(raw_key)
            results.append(
                SetParseResult(
                    path=path,
                    value=_parse_set_value(raw_value, coerce=False),
                    source_label=f"--set-string[{arg_index}]",
                    source_type=SourceType.SET_STRING,
                )
            )
    return results


def parse_set_file(raw_arguments: list[str]) -> list[SetParseResult]:
    """Parse ``--set-file key=path`` arguments.

    Reads the file at ``path`` and uses its contents as a string value.
    The file is read at parse time (matching Helm's behavior).

    Raises ValueTraceError if the file cannot be read.
    File contents are never exposed in error messages.
    """
    results: list[SetParseResult] = []
    for arg_index, raw in enumerate(raw_arguments, start=1):
        for assignment in _split_assignments(raw):
            if "=" not in assignment:
                raise ValueTraceError("Invalid --set-file value (expected key=path)")
            raw_key, file_path_str = _split_key_value(assignment)
            path = _parse_key_path(raw_key)
            file_path = Path(_unescape_value(file_path_str))

            try:
                if not file_path.is_file():
                    raise FileNotFoundError
                with file_path.open("rb") as stream:
                    raw_contents = stream.read(MAX_INPUT_BYTES + 1)
                if len(raw_contents) > MAX_INPUT_BYTES:
                    raise ValueTraceError("--set-file input exceeds 8 MiB size limit")
                file_contents = raw_contents.decode("utf-8", errors="replace")
            except FileNotFoundError:
                raise ValueTraceError(
                    f"--set-file: file not found for key '{raw_key}': {file_path_str}"
                ) from None
            except OSError as exc:
                raise ValueTraceError(
                    f"--set-file: cannot read file for key '{raw_key}': {exc.strerror}"
                ) from None

            results.append(
                SetParseResult(
                    path=path,
                    value=file_contents,
                    source_label=f"--set-file[{arg_index}]",
                    source_type=SourceType.SET_FILE,
                )
            )
    return results


def _check_input_size(raw: str) -> None:
    if len(raw) > MAX_INPUT_BYTES or len(raw.encode("utf-8", errors="replace")) > MAX_INPUT_BYTES:
        raise ValueTraceError("CLI input exceeds 8 MiB size limit")


def _json_number(raw: str) -> float | int:
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("Non-finite JSON number")
    if value.is_integer() and abs(value) < 1e21:
        return int(Decimal(repr(value)))
    return value


def _normalize_json(value: Any) -> Any:
    """Match Go's replacement of isolated UTF-16 surrogates and bound reports."""
    count = 0

    def normalize(item, depth):
        nonlocal count
        count += 1
        if depth > 64 or count > 100_000:
            raise ValueTraceError("JSON exceeds depth or node limit")
        if isinstance(item, str):
            return "".join("\ufffd" if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in item)
        if isinstance(item, list):
            return [normalize(child, depth + 1) for child in item]
        if isinstance(item, dict):
            return {
                normalize(key, depth + 1): normalize(child, depth + 1)
                for key, child in item.items()
            }
        return item

    return normalize(value, 0)


def parse_set_json(raw_arguments: list[str]) -> list[SetParseResult]:
    """Parse ``--set-json key=json`` arguments.

    The value is parsed as JSON. Supports objects, arrays, strings, numbers,
    booleans, and null.

    Raises ValueTraceError on invalid JSON.
    """
    results: list[SetParseResult] = []

    def reject_constant(value):
        raise ValueError("Non-finite JSON number")

    decoder = json.JSONDecoder(
        parse_constant=reject_constant, parse_int=_json_number, parse_float=_json_number
    )
    for arg_index, raw in enumerate(raw_arguments, start=1):
        _check_input_size(raw)
        if raw.lstrip().startswith("{"):
            raise ValueTraceError("--set-json object-only syntax is unsupported; use KEY=JSON")
        remaining = raw
        while remaining:
            escaped = False
            split = None
            for i, ch in enumerate(remaining):
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == "=":
                    split = i
                    break
            if split is None:
                raise ValueTraceError("Invalid --set-json expression; expected KEY=JSON")
            path = _parse_key_path(remaining[:split])
            tail = remaining[split + 1 :].lstrip()
            try:
                if not tail or tail.startswith(","):
                    value, end = None, 0
                else:
                    value, end = decoder.raw_decode(tail)
                    value = _normalize_json(value)
            except (ValueError, RecursionError):
                raise ValueTraceError("--set-json: invalid JSON value for key") from None
            rest = tail[end:].lstrip()
            if rest and not rest.startswith(","):
                raise ValueTraceError("--set-json: expected comma after JSON value")
            results.append(
                SetParseResult(path, value, f"--set-json[{arg_index}]", SourceType.SET_JSON)
            )
            remaining = rest[1:] if rest else ""
    return results


def parse_set_literal(raw_arguments: list[str]) -> list[SetParseResult]:
    """Parse ``--set-literal key=value`` arguments.

    The value is used literally as a string.  Unlike ``--set-string``, the
    value is not processed for escape sequences at all — backslashes and
    commas have no special meaning.

    For supported key paths, this matches Helm's --set-literal semantics: the entire remainder after
    the first '=' is the literal value.
    """
    results: list[SetParseResult] = []
    for arg_index, raw in enumerate(raw_arguments, start=1):
        if "=" not in raw:
            raise ValueTraceError("Invalid --set-literal value (expected key=value)")
        _check_input_size(raw)
        raw_key, raw_value = raw.split("=", 1)
        path = _parse_key_path(raw_key, literal=True)
        results.append(
            SetParseResult(
                path=path,
                value=raw_value,
                source_label=f"--set-literal[{arg_index}]",
                source_type=SourceType.SET_LITERAL,
            )
        )
    return results


def parse_all_set_arguments(
    set_args: list[str] | None = None,
    set_string_args: list[str] | None = None,
    set_file_args: list[str] | None = None,
    set_json_args: list[str] | None = None,
    set_literal_args: list[str] | None = None,
) -> list[SetParseResult]:
    """Parse all --set-* arguments in Helm's application order.

    Helm applies overrides in this order (left to right, last wins):
    --set-json, --set, --set-string, --set-file, --set-literal

    When multiple --set flags appear on the command line, they are applied
    in the order they appear. This function preserves that order by
    interleaving results from each parser in their respective argument order.

    In practice, all five lists are independent and applied sequentially;
    this function concatenates them in Helm's application order.
    """
    results: list[SetParseResult] = []

    if set_json_args:
        results.extend(parse_set_json(set_json_args))
    if set_args:
        results.extend(parse_set(set_args))
    if set_string_args:
        results.extend(parse_set_string(set_string_args))
    if set_file_args:
        results.extend(parse_set_file(set_file_args))
    if set_literal_args:
        results.extend(parse_set_literal(set_literal_args))

    return results
