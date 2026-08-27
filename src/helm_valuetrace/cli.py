from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import __version__
from .core import (
    TraceResult,
    ValueTraceError,
    final_assignment,
    flatten_values,
    load_values_file,
    path_text,
    trace_values,
)


class ValueTraceArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueTraceError(message)


@dataclass(frozen=True)
class DeniedSource:
    source: str
    pattern: str


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
              chart/values.yaml -> first -f file -> later -f files -> --set
              When the same key is assigned more than once, the last assignment wins.

            EXAMPLES
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
              0  Analysis completed; no enabled validation or source policy failed.
              1  Invalid input, missing file, invalid YAML, or another usage error.
              2  A validation check or source policy blocked the configuration.

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

    output = parser.add_argument_group("Output")
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

    general = parser.add_argument_group("General")
    general.add_argument(
        "-h",
        "--help",
        action="help",
        help="Show this help message and exit",
    )
    general.add_argument(
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


def _result_rows(result: TraceResult, only_overridden: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path, value in sorted(flatten_values(result.values).items(), key=lambda item: path_text(item[0])):
        assignments = result.history.get(path, [])
        if only_overridden and len(assignments) < 2:
            continue
        final = final_assignment(path, result.history)
        rows.append(
            {
                "key": path_text(path),
                "value": value,
                "source": final.location if final else "computed",
                "assignments": [
                    {"source": assignment.location, "value": assignment.value}
                    for assignment in assignments
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
    return [
        {"key": item.key, "reference": item.reference}
        for item in result.missing
    ]


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
    return [
        {"source": item.source, "pattern": item.pattern}
        for item in denied_sources
    ]


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
        return None

    if process.returncode != 0:
        return None
    match = re.search(r"v?(\d+)(?:\.|$)", process.stdout.strip())
    return int(match.group(1)) if match else None


def _print_warnings(result: TraceResult, denied_sources: list[DeniedSource]) -> None:
    if not (result.unknown or result.missing or denied_sources):
        return

    print("\nWARNINGS", file=sys.stderr)
    for item in result.unknown:
        message = f"- Unknown key '{item.key}' from {item.source}"
        if item.suggestion:
            message += f"; did you mean '{item.suggestion}'?"
        print(message, file=sys.stderr)
    for item in result.missing:
        print(
            f"- Missing key '{item.key}' compared with {item.reference}",
            file=sys.stderr,
        )
    for item in denied_sources:
        print(
            f"- Denied source '{item.source}' matched pattern '{item.pattern}'",
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
            row["key"],
            _compact_value(row["value"]),
            row["source"],
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
    document = {
        "values": rows,
        "unknown": _unknown_rows(result),
        "missing": _missing_rows(result),
        "denied_sources": _denied_source_rows(denied_sources),
    }
    if output == "json":
        print(json.dumps(document, indent=2, ensure_ascii=False, default=str))
    else:
        print(yaml.safe_dump(document, sort_keys=False, allow_unicode=True).rstrip())
    _print_warnings(result, denied_sources)


def run(args: argparse.Namespace) -> int:
    if args.strict_reference and args.reference_values is None:
        raise ValueTraceError("--strict-reference requires --reference-values FILE")

    chart = args.chart.resolve()
    if not chart.is_dir():
        raise ValueTraceError(f"Chart directory not found: {chart}")
    if not (chart / "Chart.yaml").is_file():
        raise ValueTraceError(f"Chart.yaml not found in: {chart}")

    default_path = chart / "values.yaml"
    displayed_default_path = args.chart / "values.yaml"
    if default_path.is_file():
        default_values, default_lines = load_values_file(default_path)
    else:
        default_values, default_lines = {}, {}

    overrides = []
    for value_path in args.values:
        resolved = value_path.resolve()
        values, lines = load_values_file(resolved)
        overrides.append((values, str(value_path), lines))
    denied_sources = _find_denied_sources(args.values, args.deny_source)

    reference_values = None
    reference_source = None
    if args.reference_values is not None:
        reference_path = args.reference_values.resolve()
        reference_values, _ = load_values_file(reference_path)
        reference_source = str(args.reference_values)

    result = trace_values(
        default_values=default_values,
        default_source=str(displayed_default_path),
        default_lines=default_lines,
        overrides=overrides,
        set_arguments=args.set_values,
        reference_values=reference_values,
        reference_source=reference_source,
        helm_major_version=_detect_helm_major_version(),
    )
    rows = _result_rows(result, args.only_overridden)

    if args.output == "table":
        _print_table(
            rows,
            result,
            denied_sources,
            source_policy_enabled=bool(args.deny_source),
        )
    else:
        _print_structured(args.output, rows, result, denied_sources)

    if denied_sources:
        return 2
    if args.strict_unknown and result.unknown:
        return 2
    if args.strict_reference and (result.unknown or result.missing):
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return run(args)
    except ValueTraceError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
