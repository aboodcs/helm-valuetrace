"""
Static template analysis — finds .Values.foo references in Helm templates.

IMPORTANT LIMITATIONS (documented honestly):
- Only detects static string references via regex matching .Values.<key>
- Many dynamic patterns are NOT detected (see LIMITATIONS list below)
- This analysis is purely informational — never causes exit code 2

LIMITATIONS:
  - Dynamic indexing (index .Values.foo .bar) is not detected
  - tpl calls with dynamic keys are not detected
  - with/range context changes may cause false negatives
  - include/template helper calls are not analyzed
  - Computed paths and variables are not detected
  - Whitespace control (- in {%- tags) does not affect detection
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT_VALUES_PATTERN = re.compile(r"\.Values(?![\w.])")
VALUES_REF_PATTERN = re.compile(r"\.Values\.([a-zA-Z0-9_.[\]]+)")

LIMITATIONS: list[str] = [
    "Dynamic indexing (index .Values.foo .bar) is not detected",
    "tpl calls and dynamic key generation are not detected",
    "with/range context switching may cause references to be missed",
    "Helper files are scanned, but helper execution and arguments are not resolved",
    "Computed paths, variables, and subchart context are not detected",
]


def find_value_references(chart_dir: Path) -> dict[str, list[str]]:
    """Return ``{key: [template_file, ...]}`` for each ``.Values.key`` reference found.

    Scans all ``*.yaml`` files inside ``chart_dir/templates/``.
    Returns an empty dict if the templates directory does not exist.
    """
    refs: dict[str, list[str]] = {}
    templates_dir = chart_dir / "templates"
    if not templates_dir.is_dir():
        return refs

    template_files: list[Path] = []
    for ext in ("*.yaml", "*.yml", "*.tpl", "*.txt"):
        template_files.extend(templates_dir.rglob(ext))

    for tmpl in sorted(set(template_files)):
        try:
            text = tmpl.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        relative = str(tmpl.relative_to(chart_dir))
        if ROOT_VALUES_PATTERN.search(text):
            refs.setdefault("", []).append(relative)
        for match in VALUES_REF_PATTERN.finditer(text):
            key = match.group(1)
            relative = str(tmpl.relative_to(chart_dir))
            refs.setdefault(key, [])
            if relative not in refs[key]:
                refs[key].append(relative)

    return refs
