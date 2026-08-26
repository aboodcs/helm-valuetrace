from __future__ import annotations

import argparse
import json
import sys
import textwrap
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="helm valuetrace",
        usage="helm valuetrace CHART [options]",
        add_help=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent(
            """\
            Trace, validate, and compare the values supplied to a local Helm chart.

            ValueTrace shows the final value of every key, the file and line that
            supplied it, and how many times it was assigned. It can also detect
            unknown keys and compare an environment file with a reference structure.

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

              Compare a values file with a reference structure:
                helm valuetrace ./chart --reference-values ./values/reference.yaml \\
                  -f ./values/staging.yaml --strict-reference

            EXIT CODES
              0  Analysis completed; no enabled strict check failed.
              1  Invalid input, missing file, invalid YAML, or another usage error.
              2  A strict validation check found unknown or missing keys.

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


def _print_table(rows: list[dict[str, Any]], result: TraceResult) -> None:
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

    def clipped(cell: str, width: int) -> str:
        if len(cell) <= width:
            return cell
        return cell[: max(0, width - 1)] + "…"

    print("HELM VALUETRACE")
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rendered:
        print("  ".join(clipped(cell, widths[index]).ljust(widths[index]) for index, cell in enumerate(row)))

    overridden = sum(1 for row in rows if len(row["assignments"]) > 1)
    print(
        f"\n{len(rows)} final values, {overridden} overridden values, "
        f"{len(result.unknown)} unknown keys, {len(result.missing)} missing reference keys"
    )

    if result.unknown or result.missing:
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


def _print_structured(output: str, rows: list[dict[str, Any]], result: TraceResult) -> None:
    document = {
        "values": rows,
        "unknown": _unknown_rows(result),
        "missing": _missing_rows(result),
    }
    if output == "json":
        print(json.dumps(document, indent=2, ensure_ascii=False, default=str))
    else:
        print(yaml.safe_dump(document, sort_keys=False, allow_unicode=True).rstrip())


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
    )
    rows = _result_rows(result, args.only_overridden)

    if args.output == "table":
        _print_table(rows, result)
    else:
        _print_structured(args.output, rows, result)

    if args.strict_unknown and result.unknown:
        return 2
    if args.strict_reference and (result.unknown or result.missing):
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except ValueTraceError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
