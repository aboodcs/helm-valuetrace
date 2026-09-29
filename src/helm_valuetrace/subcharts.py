"""
Subchart and dependency support for Helm ValueTrace.

Supports:
- Local subcharts in charts/ directory (pre-fetched dependencies)
- Dependency metadata and alias mapping from Chart.yaml
- Hierarchical value coalescing (child defaults -> parent overrides -> user overrides -> CLI)
- Global values (.global) propagation to all subcharts
- Recursive nested subcharts (parent -> child -> grandchild)
- Strict error checking with ChartError (missing deps, duplicate aliases, malformed YAML)
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from .core import ChartError, PathKey, ValueTraceError, load_values_file
from .models import SubchartInfo

__all__ = [
    "SubchartInfo",
    "discover_subcharts",
    "load_subchart_values",
]


def _load_yaml_file(path: Path) -> dict[str, Any]:
    """Load bounded YAML, retaining the legacy empty result for a missing file."""
    if not path.exists():
        return {}
    return load_values_file(path)[0]


def _get_dependencies(chart_dir: Path) -> list[dict[str, Any]]:
    """Return the list of dependencies from Chart.yaml."""
    chart_yaml = chart_dir / "Chart.yaml"
    if not chart_yaml.is_file():
        return []
    data = _load_yaml_file(chart_yaml)
    deps = data.get("dependencies", [])
    return deps if isinstance(deps, list) else []


def _validate_dependencies_metadata(chart_dir: Path) -> list[dict[str, Any]]:
    """Load and validate dependencies from Chart.yaml.

    Raises ChartError if Chart.yaml is malformed or has duplicate aliases.
    """
    chart_yaml = chart_dir / "Chart.yaml"
    if not chart_yaml.is_file():
        return []

    try:
        data = load_values_file(chart_yaml)[0]
    except ValueTraceError as exc:
        raise ChartError(f"Malformed Chart.yaml in {chart_dir}") from exc

    deps = data.get("dependencies")
    if deps is None:
        return []
    if not isinstance(deps, list):
        raise ChartError(f"Malformed Chart.yaml in {chart_dir}: 'dependencies' must be a list")

    seen_aliases: set[str] = set()
    seen_names: set[str] = set()
    for dep in deps:
        if not isinstance(dep, dict):
            raise ChartError(
                f"Malformed dependency entry in {chart_dir}/Chart.yaml: expected mapping"
            )
        name = dep.get("name")
        if not name or not isinstance(name, str):
            raise ChartError(f"Malformed dependency in {chart_dir}/Chart.yaml: missing 'name'")
        alias = dep.get("alias", name)
        if not isinstance(alias, str):
            raise ChartError(
                f"Malformed dependency alias in {chart_dir}/Chart.yaml: must be string"
            )
        if alias in seen_aliases:
            raise ChartError(f"More than one dependency with name or alias {alias!r}")
        seen_aliases.add(alias)
        if name in seen_names:
            raise ChartError("Multiple aliases for one dependency are unsupported")
        seen_names.add(name)
        if any(dep.get(field) for field in ("tags", "import-values")):
            raise ChartError("Dependency tags and import-values are unsupported")

        condition = dep.get("condition", "")
        if not isinstance(condition, str) or (
            condition and not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", condition)
        ):
            raise ChartError("Unsupported dependency condition: use one simple dotted path")

    return deps


def select_enabled_subcharts(
    subcharts: list[SubchartInfo], values: dict[str, Any]
) -> list[SubchartInfo]:
    """Evaluate top-level conditions on the fully coalesced preliminary values.

    Missing paths leave dependencies enabled, as in Helm. Non-boolean values
    are rejected rather than guessing. Disabled defaults must be removed by
    tracing again, retaining any parent/user values at the dependency path.
    """
    disabled: list[PathKey] = []
    for sc in subcharts:
        if not sc.condition:
            continue
        value: Any = values
        for segment in sc.condition.split("."):
            if not isinstance(value, dict) or segment not in value:
                break
            value = value[segment]
        else:
            if not isinstance(value, bool):
                raise ChartError(
                    f"Unsupported non-boolean dependency condition at {sc.condition}; "
                    "use a boolean value"
                )
            if not value:
                disabled.append(sc.path_prefix)
    return [
        sc
        for sc in subcharts
        if not any(sc.path_prefix[: len(prefix)] == prefix for prefix in disabled)
    ]


def discover_subcharts(
    chart_dir: Path,
    base_prefix: PathKey = (),
    displayed_chart: Path | None = None,
    seen_dirs: set[Path] | None = None,
) -> list[SubchartInfo]:
    """Discover subcharts in chart_dir/charts recursively.

    Returns a list of SubchartInfo objects ordered from deepest subchart
    to top-level subcharts (post-order traversal) so that deeper defaults
    can be applied first.
    """
    if seen_dirs is None:
        seen_dirs = set()

    real_chart_dir = chart_dir.resolve()
    if real_chart_dir in seen_dirs:
        raise ChartError("Recursive or repeated subchart directory is unsupported")
    if len(seen_dirs) >= 128 or len(base_prefix) >= 32:
        raise ChartError("Subchart depth or count limit exceeded")
    seen_dirs.add(real_chart_dir)

    deps = _validate_dependencies_metadata(chart_dir)
    if base_prefix and any(dep.get("condition") for dep in deps):
        raise ChartError("Conditions on nested dependencies are unsupported")
    conditions = {dep["name"]: dep.get("condition", "") for dep in deps}
    alias_map: dict[str, str] = {}
    declared_names: set[str] = set()
    for dep in deps:
        name = dep["name"]
        alias = dep.get("alias", name)
        alias_map[name] = alias
        declared_names.add(name)

    charts_dir = chart_dir / "charts"
    if not charts_dir.is_dir():
        if declared_names:
            first_missing = sorted(declared_names)[0]
            raise ChartError(
                "Chart dependency declared in Chart.yaml, "
                f"but missing in charts/ directory: {first_missing}"
            )
        return []

    found_subchart_names: set[str] = set()
    for item in charts_dir.iterdir():
        if item.is_dir():
            found_subchart_names.add(item.name)
        elif item.is_file() and (item.name.endswith(".tgz") or item.name.endswith(".tar.gz")):
            raise ChartError("Packaged dependencies in charts/ are unsupported; unpack them first")

    for dep_name in declared_names:
        if dep_name not in found_subchart_names:
            raise ChartError(
                "Chart dependency declared in Chart.yaml, "
                f"but missing in charts/ directory: {dep_name}"
            )

    subcharts: list[SubchartInfo] = []

    for subchart_dir in sorted(charts_dir.iterdir()):
        if not subchart_dir.is_dir():
            continue

        chart_yaml = subchart_dir / "Chart.yaml"
        if not chart_yaml.is_file():
            if subchart_dir.name in declared_names:
                raise ChartError(
                    "Chart dependency declared in Chart.yaml, "
                    f"but missing Chart.yaml: {subchart_dir.name}"
                )
            continue

        try:
            meta = load_values_file(chart_yaml)[0]
        except ValueTraceError as exc:
            raise ChartError(f"Malformed Chart.yaml in subchart {subchart_dir.name}") from exc
        if not all(isinstance(meta.get(k), str) and meta[k] for k in ("name", "apiVersion")):
            raise ChartError(
                f"Malformed Chart.yaml in subchart {subchart_dir.name}: missing required field"
            )
        if meta["name"] != subchart_dir.name:
            raise ChartError("Subchart directory names differing from chart names are unsupported")

        for dep in deps:
            if (
                dep["name"] == subchart_dir.name
                and dep.get("condition")
                and dep.get("alias", dep["name"]) != dep["name"]
                and str(dep.get("version", "")) != str(meta.get("version", ""))
            ):
                raise ChartError(
                    "Aliased dependency conditions require an exact matching version; "
                    "missing versions and version ranges are unsupported"
                )

        subchart_name = subchart_dir.name
        effective_name = alias_map.get(subchart_name, subchart_name)
        path_prefix = base_prefix + (effective_name,)

        values_yaml = subchart_dir / "values.yaml"
        if values_yaml.is_file():
            try:
                values, lines, cols = load_values_file(values_yaml)
            except ValueTraceError as exc:
                raise ChartError(
                    f"Malformed values.yaml in subchart {subchart_name}: {exc}"
                ) from exc
        else:
            values, lines, cols = {}, {}, {}

        if displayed_chart is not None:
            if displayed_chart.is_file():
                source_label = f"charts/{subchart_name}/values.yaml"
            else:
                source_label = str(displayed_chart / "charts" / subchart_name / "values.yaml")
        else:
            source_label = f"charts/{subchart_name}/values.yaml"

        nested_displayed = (
            displayed_chart / "charts" / subchart_name
            if displayed_chart and not displayed_chart.is_file()
            else None
        )
        nested = discover_subcharts(
            subchart_dir,
            base_prefix=path_prefix,
            displayed_chart=nested_displayed,
            seen_dirs=seen_dirs,
        )

        subcharts.extend(nested)
        subcharts.append(
            SubchartInfo(
                name=subchart_name,
                alias=effective_name,
                path_prefix=path_prefix,
                chart_dir=subchart_dir,
                values=values,
                lines=lines,
                cols=cols,
                source_label=source_label,
                condition=conditions.get(subchart_name, ""),
            )
        )

    return subcharts


def load_subchart_values(
    parent_chart_dir: Path,
    parent_values: dict[str, Any],
) -> dict[str, tuple[dict[str, Any], str]]:
    """Load and merge subchart values following Helm's coalescing semantics.

    Preserved for backward compatibility with existing tests and CLI.
    """
    charts_dir = parent_chart_dir / "charts"
    if not charts_dir.is_dir():
        return {}

    dependencies = _get_dependencies(parent_chart_dir)
    alias_map: dict[str, str] = {}
    for dep in dependencies:
        if isinstance(dep, dict):
            name = dep.get("name", "")
            alias = dep.get("alias", name)
            alias_map[name] = alias

    global_values = parent_values.get("global", {}) or {}
    result: dict[str, tuple[dict[str, Any], str]] = {}

    for subchart_dir in sorted(charts_dir.iterdir()):
        if not subchart_dir.is_dir():
            continue
        if not (subchart_dir / "Chart.yaml").is_file():
            continue

        subchart_name = subchart_dir.name
        effective_name = alias_map.get(subchart_name, subchart_name)

        subchart_values = _load_yaml_file(subchart_dir / "values.yaml")
        parent_override = parent_values.get(effective_name, {}) or {}

        merged = _merge_subchart_values(subchart_values, parent_override, global_values)
        source_label = f"charts/{subchart_name}/values.yaml"
        result[effective_name] = (merged, source_label)

    return result


def _merge_subchart_values(
    subchart_defaults: dict[str, Any],
    parent_override: dict[str, Any],
    global_values: dict[str, Any],
) -> dict[str, Any]:
    """Merge subchart values following Helm coalescing order."""
    merged = copy.deepcopy(subchart_defaults)
    _deep_merge(merged, parent_override)
    if global_values:
        merged.setdefault("global", {})
        _deep_merge(merged["global"], global_values)
    return merged


def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> None:
    """In-place deep merge: source wins over target for scalar/list values."""
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)
