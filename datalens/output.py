"""Stable audit serialization, CPU chart rendering, and report generation."""

from __future__ import annotations

import base64
import html
import json
import math
from pathlib import Path
from typing import Any

from .analysis import json_bytes, is_missing, parse_date, parse_number
from .ingest import Table


def audit_events(result: dict[str, Any], command: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = [{"event": "analysis_started", "command": command, "source": result["source"], "file_sha256": result["file_sha256"]}]
    for decision in result["ingestion_decisions"]:
        events.append({"event": "ingestion_decision", **decision})
    for finding in result["findings"]:
        events.append({"event": "rule_fired", "finding_id": finding["finding_id"], "rule_id": finding["rule_id"], "threshold": finding["threshold"], "evidence_count": len(finding["evidence"])})
        events.append({"event": "finding_emitted", "finding_id": finding["finding_id"], "rule_id": finding["rule_id"], "title": finding["title"]})
    events.append({"event": "analysis_completed", "finding_count": len(result["findings"]), "row_count": result["profile"]["row_count"], "column_count": result["profile"]["column_count"]})
    return events


def audit_bytes(events: list[dict[str, Any]]) -> bytes:
    return b"".join(json_bytes(event, pretty=False) for event in events)


def write_audit(result: dict[str, Any], path: str | Path, command: str) -> list[dict[str, Any]]:
    events = audit_events(result, command)
    Path(path).write_bytes(audit_bytes(events))
    return events


def write_findings(result: dict[str, Any], path: str | Path) -> None:
    Path(path).write_bytes(json_bytes(result))


def _numeric_columns(table: Table, result: dict[str, Any]) -> list[int]:
    return [index for index, column in enumerate(result["profile"]["columns"]) if column["inferred_type"].startswith("numeric")]


def _chart_paths(table: Table, result: dict[str, Any], output_dir: Path) -> list[Path]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "font.size": 9,
        "axes.titlesize": 12,
        "axes.labelsize": 9,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "savefig.facecolor": "white",
        "axes.spines.top": False,
        "axes.spines.right": False,
    })
    output_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    columns = result["profile"]["columns"]
    if table.rows and table.headers:
        missing = np.array([[1.0 if is_missing(row[index] if index < len(row) else None) else 0.0 for index in range(len(table.headers))] for row in table.rows], dtype=float)
        fig, axis = plt.subplots(figsize=(max(6.0, min(16.0, len(table.headers) * 1.1)), max(2.8, min(10.0, len(table.rows) * 0.23))))
        axis.imshow(missing, aspect="auto", interpolation="nearest", cmap="OrRd", vmin=0, vmax=1)
        axis.set_title("Missing value map")
        axis.set_xlabel("Column")
        axis.set_ylabel("Source row index")
        axis.set_xticks(range(len(table.headers)), table.headers, rotation=45, ha="right")
        fig.tight_layout()
        path = output_dir / "missingness.png"
        fig.savefig(path, dpi=110, metadata={"Software": "DataLens"})
        plt.close(fig)
        created.append(path)

    numeric_indices = _numeric_columns(table, result)
    if numeric_indices and table.rows:
        top = max(numeric_indices, key=lambda index: (columns[index].get("numeric", {}).get("parsed_count", 0), columns[index]["cardinality"], -index))
        observed = [(table.row_numbers[row_index], parse_number(row[top] if top < len(row) else None)) for row_index, row in enumerate(table.rows)]
        observed = [(row_number, value) for row_number, value in observed if value is not None]
        outlier_rows = set()
        for finding in result["findings"]:
            if finding["rule_id"] in {"NUMERIC_ZSCORE_3", "NUMERIC_IQR_1_5"}:
                outlier_rows.update(pointer["row"] for pointer in finding["evidence"] if pointer["column"] == table.headers[top])
        fig, axis = plt.subplots(figsize=(8, 4.2))
        axis.scatter([row for row, _ in observed], [value for _, value in observed], s=24, color="#2463a6", label="Observed")
        flagged = [(row, value) for row, value in observed if row in outlier_rows]
        if flagged:
            axis.scatter([row for row, _ in flagged], [value for _, value in flagged], s=44, color="#c13b35", label="Heuristic outlier")
            axis.legend(frameon=False)
        axis.set_title(f"Numeric observations: {table.headers[top]}")
        axis.set_xlabel("Source row number")
        axis.set_ylabel(table.headers[top])
        fig.tight_layout()
        path = output_dir / "numeric_outliers.png"
        fig.savefig(path, dpi=110, metadata={"Software": "DataLens"})
        plt.close(fig)
        created.append(path)

    date_indices = [index for index, column in enumerate(columns) if column["inferred_type"].startswith("date")]
    for date_index in date_indices:
        for numeric_index in numeric_indices:
            buckets: dict[Any, list[float]] = {}
            for row in table.rows:
                day = parse_date(row[date_index] if date_index < len(row) else None)
                number = parse_number(row[numeric_index] if numeric_index < len(row) else None)
                if day is not None and number is not None:
                    buckets.setdefault(day, []).append(number)
            if len(buckets) < 2:
                continue
            ordered = sorted((day, sum(values) / len(values)) for day, values in buckets.items())
            fig, axis = plt.subplots(figsize=(8, 4.2))
            axis.plot([item[0] for item in ordered], [item[1] for item in ordered], color="#2463a6", marker="o", markersize=3, linewidth=1.5)
            axis.set_title(f"Observed values by date: {table.headers[numeric_index]}")
            axis.set_xlabel(table.headers[date_index])
            axis.set_ylabel(table.headers[numeric_index])
            fig.autofmt_xdate()
            fig.tight_layout()
            slug = f"timeseries_{date_index + 1}_{numeric_index + 1}.png"
            path = output_dir / slug
            fig.savefig(path, dpi=110, metadata={"Software": "DataLens"})
            plt.close(fig)
            created.append(path)

    if len(numeric_indices) >= 2:
        matrix = []
        for left_index in numeric_indices:
            row_values = []
            for right_index in numeric_indices:
                pairs = [(parse_number(row[left_index] if left_index < len(row) else None), parse_number(row[right_index] if right_index < len(row) else None)) for row in table.rows]
                pairs = [(left, right) for left, right in pairs if left is not None and right is not None]
                if len(pairs) < 3:
                    row_values.append(float("nan"))
                    continue
                left_mean = sum(left for left, _ in pairs) / len(pairs)
                right_mean = sum(right for _, right in pairs) / len(pairs)
                numerator = sum((left - left_mean) * (right - right_mean) for left, right in pairs)
                left_sum = sum((left - left_mean) ** 2 for left, _ in pairs)
                right_sum = sum((right - right_mean) ** 2 for _, right in pairs)
                row_values.append(numerator / math.sqrt(left_sum * right_sum) if left_sum and right_sum else float("nan"))
            matrix.append(row_values)
        fig, axis = plt.subplots(figsize=(max(5.5, len(numeric_indices) * 1.0), max(4.5, len(numeric_indices) * 0.8)))
        image = axis.imshow(np.array(matrix), cmap="coolwarm", vmin=-1, vmax=1, interpolation="nearest")
        axis.set_title("Pairwise Pearson correlation")
        names = [table.headers[index] for index in numeric_indices]
        axis.set_xticks(range(len(names)), names, rotation=45, ha="right")
        axis.set_yticks(range(len(names)), names)
        fig.colorbar(image, ax=axis, label="Pearson r")
        fig.tight_layout()
        path = output_dir / "correlations.png"
        fig.savefig(path, dpi=110, metadata={"Software": "DataLens"})
        plt.close(fig)
        created.append(path)
    return created


def generate_charts(table: Table, result: dict[str, Any], output_dir: str | Path) -> list[Path]:
    return _chart_paths(table, result, Path(output_dir))


def _value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _profile_markdown(profile: dict[str, Any]) -> list[str]:
    lines = [f"Rows: {profile['row_count']}, columns: {profile['column_count']}, duplicate rows: {profile['duplicate_row_count']}", "", "| Column | Type | Missing | Rate | Cardinality | Numeric range |", "| --- | --- | ---: | ---: | ---: | --- |"]
    for column in profile["columns"]:
        numeric = column.get("numeric", {})
        limits = f"{numeric.get('min')} to {numeric.get('max')}" if numeric else ""
        lines.append(f"| {column['name']} | {column['inferred_type']} | {column['missing_count']} | {column['missing_rate']} | {column['cardinality']} | {limits} |")
    return lines


def render_markdown(result: dict[str, Any], chart_paths: list[Path], audit: list[dict[str, Any]], report_dir: Path) -> str:
    lines = ["# DataLens analysis report", "", "> Heuristic and statistical observations, not verified business facts. Trend estimates are not forecasts.", "", f"Source: `{result['source']}`", f"SHA-256: `{result['file_sha256']}`", "", "## Profile", ""]
    lines.extend(_profile_markdown(result["profile"]))
    lines.extend(["", "## Findings", ""])
    if not result["findings"]:
        lines.append("No findings crossed the configured heuristic or statistical thresholds.")
    for finding in result["findings"]:
        lines.extend([f"### {finding['finding_id']}: {finding['title']}", "", f"Type: {finding['observation_type']}. Rule: `{finding['rule_id']}`.", "", finding["explanation"], "", f"Threshold: `{_value(finding['threshold'])}`", "", "Evidence pointers:"])
        for pointer in finding["evidence"]:
            lines.append(f"- `{pointer['column']}`, row {pointer['row']}: `{_value(pointer['value'])}`")
        if finding["details"]:
            lines.extend(["", f"Details: `{_value(finding['details'])}`"])
        lines.append("")
    lines.extend(["## Charts", ""])
    if chart_paths:
        for path in chart_paths:
            relative = Path(path).resolve().relative_to(report_dir.resolve()) if path.resolve().is_relative_to(report_dir.resolve()) else path
            lines.extend([f"### {path.stem.replace('_', ' ').title()}", "", f"![{path.stem}]({relative.as_posix()})", ""])
    else:
        lines.append("No charts were available for this input.")
    lines.extend(["## Interpretation notes", "", "Correlation is not causation. A flagged threshold is a review prompt, not a verified business fact. Trend estimates summarize observed values and are not forecasts.", "", "## Audit trail", "", "```jsonl"])
    lines.extend(json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for event in audit)
    lines.extend(["```", ""])
    return "\n".join(lines)


def render_html(result: dict[str, Any], chart_paths: list[Path], audit: list[dict[str, Any]]) -> str:
    profile_rows = []
    for column in result["profile"]["columns"]:
        numeric = column.get("numeric", {})
        limits = f"{numeric.get('min')} to {numeric.get('max')}" if numeric else ""
        profile_rows.append("<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in (column["name"], column["inferred_type"], column["missing_count"], column["missing_rate"], column["cardinality"], limits)) + "</tr>")
    finding_html = []
    for finding in result["findings"]:
        evidence = "".join(f"<li><code>{html.escape(pointer['column'])}</code>, row {pointer['row']}: <code>{html.escape(_value(pointer['value']))}</code></li>" for pointer in finding["evidence"])
        details = f"<pre>{html.escape(_value(finding['details']))}</pre>" if finding["details"] else ""
        finding_html.append(f"<article><h3>{html.escape(finding['finding_id'])}: {html.escape(finding['title'])}</h3><p><strong>{html.escape(finding['observation_type'])}</strong>, rule <code>{html.escape(finding['rule_id'])}</code>.</p><p>{html.escape(finding['explanation'])}</p><p>Threshold: <code>{html.escape(_value(finding['threshold']))}</code></p><details><summary>Evidence pointers ({len(finding['evidence'])})</summary><ul>{evidence}</ul></details>{details}</article>")
    chart_html = []
    for path in chart_paths:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        chart_html.append(f'<figure><img alt="{html.escape(path.stem)}" src="data:image/png;base64,{encoded}"><figcaption>{html.escape(path.stem.replace("_", " ").title())}</figcaption></figure>')
    trail = "\n".join(json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for event in audit)
    findings = "".join(finding_html) or "<p>No findings crossed the configured heuristic or statistical thresholds.</p>"
    charts = "".join(chart_html) or "<p>No charts were available for this input.</p>"
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>DataLens analysis report</title><style>
body{{font:16px/1.55 "Segoe UI",Arial,sans-serif;color:#1e293b;background:#f5f7fa;margin:0}}main{{max-width:1100px;margin:0 auto;padding:32px 24px}}header,.panel,article{{background:white;border:1px solid #dbe2ea;border-radius:10px;padding:22px;margin:16px 0}}h1,h2,h3{{color:#173b63}}.label{{color:#854d0e;background:#fef3c7;padding:10px 14px;border-radius:6px}}table{{width:100%;border-collapse:collapse;background:white}}th,td{{text-align:left;border-bottom:1px solid #e2e8f0;padding:9px}}figure{{margin:20px 0;background:white;border:1px solid #dbe2ea;padding:14px;border-radius:8px}}img{{max-width:100%;height:auto}}figcaption{{color:#475569}}code,pre{{background:#f1f5f9;padding:3px 5px;border-radius:4px}}pre{{overflow:auto;padding:14px}}summary{{cursor:pointer;color:#174f80;font-weight:600}}li{{margin:5px 0}}
</style></head><body><main><header><h1>DataLens analysis report</h1><p class="label">Heuristic and statistical observations, not verified business facts. Trend estimates are not forecasts.</p><p>Source: <code>{html.escape(result['source'])}</code><br>SHA-256: <code>{result['file_sha256']}</code><br>{result['profile']['row_count']} rows, {result['profile']['column_count']} columns, {result['profile']['duplicate_row_count']} duplicate rows</p></header><section class="panel"><h2>Column profile</h2><table><thead><tr><th>Column</th><th>Type</th><th>Missing</th><th>Rate</th><th>Cardinality</th><th>Numeric range</th></tr></thead><tbody>{''.join(profile_rows)}</tbody></table></section><section class="panel"><h2>Findings</h2>{findings}</section><section class="panel"><h2>Charts</h2>{charts}</section><section class="panel"><h2>Interpretation notes</h2><p>Correlation is not causation. A flagged threshold is a review prompt, not a verified business fact. Trend estimates summarize observed values and are not forecasts.</p></section><section class="panel"><h2>Audit trail</h2><pre>{html.escape(trail)}</pre></section></main></body></html>'''


def write_report(result: dict[str, Any], chart_paths: list[Path], audit: list[dict[str, Any]], path: str | Path, output_format: str) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "md":
        content = render_markdown(result, chart_paths, audit, destination.parent)
        destination.write_text(content, encoding="utf-8", newline="\n")
    elif output_format == "html":
        destination.write_text(render_html(result, chart_paths, audit), encoding="utf-8", newline="\n")
    else:
        raise ValueError(f"Unknown report format: {output_format}")
