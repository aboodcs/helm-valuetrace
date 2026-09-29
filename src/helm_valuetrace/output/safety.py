"""Escape terminal controls while retaining original data in structured reports."""

import unicodedata

from helm_valuetrace.models import PathKey


def terminal_safe(value: str) -> str:
    return "".join(
        char.encode("unicode_escape").decode("ascii")
        if unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
        else char
        for char in value
    )


def escaped_path(path: PathKey) -> str:
    parts = []
    for segment in path:
        if isinstance(segment, int):
            parts.append(f"[{segment}]")
        else:
            escaped = "".join("\\" + ch if ch in "\\.[]=," else ch for ch in segment)
            parts.append(("." if parts else "") + escaped)
    return "".join(parts)


def contributor_rows(path, history):
    """One entry per actual source location; no synthesized composite value."""
    found = {}
    for a in getattr(history, "contributors", {}).get(path, []):
        key = (a.source, a.source_type, a.line, a.column)
        found.setdefault(
            key,
            {
                "source": a.location,
                "source_type": a.source_type.value,
                "file": a.source,
                "line": a.line,
                "column": a.column,
            },
        )
    return list(found.values())
