"""
Typed data models for Helm ValueTrace.

These are the central data structures that flow through the entire system —
from parsers through the coalescing engine, analysis layers, and output formatters.
All models use frozen dataclasses for immutability where appropriate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SourceType(str, Enum):
    """Identifies which mechanism introduced a value.

    Using str as base class guarantees that JSON serialization produces
    the plain string value (e.g., "set") rather than Python internals.
    """

    VALUES_FILE = "values-file"
    SET = "set"
    SET_STRING = "set-string"
    SET_FILE = "set-file"
    SET_JSON = "set-json"
    SET_LITERAL = "set-literal"
    DEFAULT = "default"
    DEPENDENCY = "dependency"
    GLOBAL = "global"


PathKey = tuple[str | int, ...]


def path_text(path: PathKey) -> str:
    """Convert an internal PathKey to a human-readable dotted string.

    List indexes are rendered with bracket notation to distinguish
    ``servers[0].port`` from ``servers.0.port``.

    Examples:
        ("image", "repository")    -> "image.repository"
        ("servers", 0, "port")     -> "servers[0].port"
        ("a.b",)                   -> "a.b"          (escaped dot key)
    """
    parts: list[str] = []
    for i, segment in enumerate(path):
        if isinstance(segment, int):
            parts.append(f"[{segment}]")
        else:
            if i > 0 and not isinstance(path[i - 1], int):  # noqa: SIM114
                parts.append(f".{segment}")
            elif i > 0:
                parts.append(f".{segment}")
            else:
                parts.append(segment)
    return "".join(parts)


def path_text_simple(path: PathKey) -> str:
    """Dot-only version of path_text; used for legacy compatibility."""
    return ".".join(str(s) for s in path)


@dataclass(frozen=True)
class Assignment:
    """A single recorded write to a value path.

    Preserved in insertion order so the full chain of assignments is
    available for provenance visualization.

    Attributes:
        source:       Human-readable source label (e.g. "values.yaml" or "--set[1]").
        source_type:  Machine-readable SourceType enum value.
        line:         1-indexed YAML line number; None for CLI flags.
        column:       1-indexed YAML column number; None when unavailable.
        value:        The value at the time of this assignment.
    """

    source: str
    source_type: SourceType
    line: int | None
    column: int | None
    value: Any

    @property
    def location(self) -> str:
        """``source:line`` if line is known, otherwise just ``source``."""
        if self.line is None:
            return self.source
        return f"{self.source}:{self.line}"

    def to_dict(self) -> dict[str, Any]:
        """Stable dict representation suitable for JSON/YAML serialization."""
        return {
            "source": self.source,
            "source_type": self.source_type.value,
            "line": self.line,
            "column": self.column,
            "value": self.value,
        }


@dataclass(frozen=True)
class UnknownValue:
    """A key supplied in overrides or --set that is not in the chart schema."""

    key: str
    source: str
    suggestion: str | None = None


@dataclass(frozen=True)
class MissingValue:
    """A key present in the reference structure but absent from the result."""

    key: str
    reference: str


@dataclass(frozen=True)
class SchemaError:
    """A JSON Schema validation failure.

    Distinct from UnknownValue — a schema error means the key exists in
    the schema but its value fails a type/enum/pattern/range constraint.
    """

    key: str
    message: str
    schema_path: str


@dataclass
class TraceResult:
    """The complete analysis output from trace_values().

    All public fields are collections that may be empty but are never None.
    """

    values: dict[str, Any]

    history: dict[PathKey, list[Assignment]]

    unknown: list[UnknownValue] = field(default_factory=list)

    missing: list[MissingValue] = field(default_factory=list)

    schema_errors: list[SchemaError] = field(default_factory=list)

    used_by_templates: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class DeniedSource:
    """A source rejected by a file glob or CLI override policy."""

    source: str
    pattern: str


@dataclass
class SubchartInfo:
    """Discovered subchart metadata and loaded values."""

    name: str
    alias: str
    path_prefix: PathKey
    chart_dir: Any
    values: dict[str, Any]
    lines: dict[PathKey, int]
    cols: dict[PathKey, int]
    source_label: str
    condition: str = ""
