"""Command-line interface for DataLens."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .analysis import analyze_table, json_bytes, profile_table
from .ingest import DataLensError, load_table
from .output import generate_charts, write_audit, write_findings, write_report


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("input", help="input CSV or XLSX file")
    parser.add_argument("--audit", default="datalens-audit.jsonl", help="audit trail output path")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="datalens", description="Deterministic CSV and XLSX profiling and heuristic analysis")
    commands = parser.add_subparsers(dest="command", required=True)

    profile = commands.add_parser("profile", help="profile columns and missing values")
    _common(profile)
    profile.add_argument("--format", choices=("table", "json"), default="table")
    profile.add_argument("--output", help="optional profile output file")

    analyze = commands.add_parser("analyze", help="analyze the file and emit evidence-backed findings JSON")
    _common(analyze)
    analyze.add_argument("--output", default="findings.json", help="findings JSON path")
    analyze.add_argument("--correlation-threshold", type=float, default=0.5)

    chart = commands.add_parser("chart", help="generate deterministic PNG charts")
    _common(chart)
    chart.add_argument("--output-dir", help="chart output directory")

    report = commands.add_parser("report", help="write a Markdown or self-contained HTML report")
    _common(report)
    report.add_argument("--format", choices=("md", "html"), default="md")
    report.add_argument("--output", help="report output file")
    report.add_argument("--correlation-threshold", type=float, default=0.5)

    trace = commands.add_parser("trace", help="resolve a finding ID to its rule and evidence cells")
    trace.add_argument("finding_id")
    trace.add_argument("--input", default="findings.json", help="findings JSON file")
    return parser


def _table_text(profile: dict[str, Any]) -> str:
    columns = profile["columns"]
    headers = ("COLUMN", "TYPE", "MISSING", "RATE", "CARDINALITY", "MIN", "MAX", "MEAN", "MEDIAN")
    rows = []
    for item in columns:
        numeric = item.get("numeric", {})
        rows.append((item["name"], item["inferred_type"], str(item["missing_count"]), f"{item['missing_rate']:.1%}", str(item["cardinality"]), str(numeric.get("min", "")), str(numeric.get("max", "")), str(numeric.get("mean", "")), str(numeric.get("median", ""))))
    widths = [max(len(headers[index]), *(len(row[index]) for row in rows)) if rows else len(headers[index]) for index in range(len(headers))]
    lines = ["  ".join(headers[index].ljust(widths[index]) for index in range(len(headers))), "  ".join("-" * width for width in widths)]
    lines.extend("  ".join(row[index].ljust(widths[index]) for index in range(len(headers))).rstrip() for row in rows)
    lines.append(f"Rows: {profile['row_count']}, columns: {profile['column_count']}, duplicate rows: {profile['duplicate_row_count']}")
    return "\n".join(lines)


def _profile_command(args: argparse.Namespace) -> int:
    table = load_table(args.input)
    profile = profile_table(table)
    result = {"profile": profile, "ingestion_decisions": table.decisions, "file_sha256": table.sha256}
    content = json_bytes(result) if args.format == "json" else (_table_text(profile) + "\n").encode("utf-8")
    if args.output:
        Path(args.output).write_bytes(content)
        print(f"Profile written to {args.output}")
    else:
        sys.stdout.write(content.decode("utf-8"))
    analysis = analyze_table(table)
    write_audit(analysis, args.audit, "profile")
    return 0


def _analyze_command(args: argparse.Namespace) -> int:
    table = load_table(args.input)
    if not 0 <= args.correlation_threshold <= 1:
        raise DataLensError("Correlation threshold must be between 0 and 1")
    result = analyze_table(table, args.correlation_threshold)
    write_findings(result, args.output)
    write_audit(result, args.audit, "analyze")
    print(f"Wrote {len(result['findings'])} findings to {args.output}")
    return 0


def _chart_command(args: argparse.Namespace) -> int:
    table = load_table(args.input)
    result = analyze_table(table)
    output_dir = Path(args.output_dir) if args.output_dir else Path(table.source_name).stem + "_charts"
    paths = generate_charts(table, result, output_dir)
    write_audit(result, args.audit, "chart")
    if paths:
        for path in paths:
            print(path)
    else:
        print("No charts could be generated for this input")
    return 0


def _report_command(args: argparse.Namespace) -> int:
    table = load_table(args.input)
    if not 0 <= args.correlation_threshold <= 1:
        raise DataLensError("Correlation threshold must be between 0 and 1")
    result = analyze_table(table, args.correlation_threshold)
    output = Path(args.output) if args.output else Path(f"report.{args.format}")
    chart_dir = output.parent / f"{output.stem}_charts"
    charts = generate_charts(table, result, chart_dir)
    audit = write_audit(result, args.audit, "report")
    write_report(result, charts, audit, output, args.format)
    print(f"Wrote {args.format.upper()} report to {output}")
    return 0


def _trace_command(args: argparse.Namespace) -> int:
    path = Path(args.input)
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DataLensError(f"Findings file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DataLensError(f"Findings file is not valid JSON: {path}") from exc
    finding = next((item for item in result.get("findings", []) if item.get("finding_id") == args.finding_id), None)
    if finding is None:
        raise DataLensError(f"Finding ID {args.finding_id} was not found in {path}")
    sys.stdout.write(json_bytes(finding).decode("utf-8"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    commands = {"profile": _profile_command, "analyze": _analyze_command, "chart": _chart_command, "report": _report_command, "trace": _trace_command}
    try:
        return commands[args.command](args)
    except (DataLensError, OSError, ValueError) as exc:
        print(f"datalens: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
