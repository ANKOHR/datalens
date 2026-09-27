from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from datalens.analysis import analyze_table, profile_table
from datalens.ingest import load_table


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
MESSY = FIXTURES / "messy.csv"
MERGED = FIXTURES / "merged_headers.xlsx"


def run_cli(*arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "datalens", *arguments], cwd=cwd, env=environment, text=True, capture_output=True, check=False)


def test_csv_ingestion_handles_encoding_headers_and_ragged_rows() -> None:
    table = load_table(MESSY)
    assert table.headers[0] == "Date"
    assert table.headers[-1] == "column_10"
    assert len(table.rows) == 24
    assert table.rows[3][6] == "client’s note: café"
    assert table.decisions[0] == {"decision": "encoding_selected", "encoding": "cp1252"}
    assert any(item["decision"] == "ragged_rows_padded" for item in table.decisions)
    assert any(item["decision"] == "header_selected" and item["first_row"] == 2 for item in table.decisions)
    assert table.row_numbers[0] == 3 and table.row_numbers[-1] == 26


def test_xlsx_ingestion_flattens_merged_headers_and_logs_decision() -> None:
    table = load_table(MERGED)
    assert table.headers == ["Commercial.Date", "Commercial.Revenue", "Review.Transactions", "Review.Segment", "Notes"]
    assert len(table.rows) == 5
    assert any(item["decision"] == "merged_headers_flattened" and item["ranges"] == ["A1:B1", "C1:D1"] for item in table.decisions)
    assert table.row_numbers[0] == 3


def test_profile_returns_exact_fixture_counts_and_numeric_range() -> None:
    profile = profile_table(load_table(MESSY))
    assert profile["row_count"] == 24
    assert profile["column_count"] == 10
    assert profile["duplicate_row_count"] == 0
    by_name = {item["name"]: item for item in profile["columns"]}
    assert by_name["Sales"]["inferred_type"] == "numeric_mixed"
    assert by_name["Sales"]["missing_count"] == 3
    assert by_name["Sales"]["missing_rate"] == 0.125
    assert by_name["Sales"]["numeric"]["min"] == 100
    assert by_name["Sales"]["numeric"]["max"] == 146
    assert by_name["Segment"]["cardinality"] == 4
    assert by_name["Empty field"]["missing_rate"] == 1


def test_anomaly_rules_flag_planted_outliers_rare_category_and_date_gap() -> None:
    result = analyze_table(load_table(MESSY))
    findings = result["findings"]
    by_rule = {}
    for finding in findings:
        by_rule.setdefault(finding["rule_id"], []).append(finding)
    iqr = next(finding for finding in by_rule["NUMERIC_IQR_1_5"] if finding["evidence"][0]["column"] == "IQR probe")
    assert iqr["threshold"]["iqr_multiplier"] == 1.5
    assert iqr["evidence"] == [{"column": "IQR probe", "row": 26, "value": "35"}]
    zscore = next(finding for finding in by_rule["NUMERIC_ZSCORE_3"] if finding["evidence"][0]["column"] == "Z probe")
    assert zscore["threshold"]["absolute_zscore_at_least"] == 3
    assert zscore["evidence"][0]["row"] == 26
    rare = next(finding for finding in by_rule["RARE_CATEGORY_1PCT"] if finding["details"].get("category") == "One-off")
    assert rare["threshold"]["count_at_most"] == 1
    assert rare["evidence"] == [{"column": "Segment", "row": 26, "value": "One-off"}]
    gap = by_rule["DATE_GAP_3X_MEDIAN"][0]
    assert gap["threshold"]["multiplier"] == 3
    assert [item["row"] for item in gap["evidence"]] == [22, 23]
    assert not any(finding["rule_id"].startswith("NUMERIC_") and any(cell["column"] == "Control" for cell in finding["evidence"]) for finding in findings)
    assert any(finding["rule_id"] == "CONSTANT_COLUMN" and finding["evidence"][0]["column"] == "Region" for finding in findings)
    assert any(finding["rule_id"] == "ALL_NULL_COLUMN" and finding["evidence"][0]["column"] == "Empty field" for finding in findings)


def test_trend_is_labeled_as_estimate_with_cell_evidence() -> None:
    result = analyze_table(load_table(MESSY))
    finding = next(item for item in result["findings"] if item["rule_id"] == "TREND_DAILY_LINEAR_ESTIMATE" and item["details"]["value_column"] == "Sales")
    assert finding["details"]["direction"] == "up"
    assert finding["details"]["bucket_count"] >= 3
    assert "not a forecast" in finding["explanation"]
    assert {cell["column"] for cell in finding["evidence"]} == {"Date", "Sales"}


def test_correlations_report_strong_pair_and_suppress_weak_pair() -> None:
    result = analyze_table(load_table(MESSY))
    correlations = [item for item in result["findings"] if item["rule_id"] == "PEARSON_ABS_AT_LEAST_THRESHOLD"]
    strong = next(item for item in correlations if {item["details"]["left_column"], item["details"]["right_column"]} == {"Sales", "Control"})
    assert strong["details"]["pearson_r"] == 1
    assert strong["details"]["row_count"] == 20
    assert strong["threshold"]["absolute_pearson_at_least"] == 0.5
    assert not any({item["details"]["left_column"], item["details"]["right_column"]} == {"Control", "column_10"} for item in correlations)


def test_analyze_is_byte_deterministic_for_findings_and_audit(tmp_path: Path) -> None:
    first = run_cli("analyze", str(MESSY), "--output", "one.json", cwd=tmp_path)
    assert first.returncode == 0, first.stderr
    findings_one = (tmp_path / "one.json").read_bytes()
    audit_one = (tmp_path / "datalens-audit.jsonl").read_bytes()
    second = run_cli("analyze", str(MESSY), "--output", "two.json", cwd=tmp_path)
    assert second.returncode == 0, second.stderr
    assert findings_one == (tmp_path / "two.json").read_bytes()
    assert audit_one == (tmp_path / "datalens-audit.jsonl").read_bytes()
    audit_events = [json.loads(line) for line in audit_one.decode("utf-8").splitlines()]
    assert any(event["event"] == "ingestion_decision" and event["decision"] == "encoding_selected" for event in audit_events)
    assert any(event["event"] == "rule_fired" and event["rule_id"] == "DATE_GAP_3X_MEDIAN" and "threshold" in event for event in audit_events)
    assert any(event["event"] == "finding_emitted" for event in audit_events)


def test_chart_pngs_are_byte_deterministic(tmp_path: Path) -> None:
    first = run_cli("chart", str(MESSY), "--output-dir", str(tmp_path / "charts_one"), cwd=tmp_path)
    second = run_cli("chart", str(MESSY), "--output-dir", str(tmp_path / "charts_two"), cwd=tmp_path)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    charts_one = {path.name: path.read_bytes() for path in (tmp_path / "charts_one").glob("*.png")}
    charts_two = {path.name: path.read_bytes() for path in (tmp_path / "charts_two").glob("*.png")}
    assert charts_one
    assert charts_one == charts_two


def test_report_formats_and_every_report_finding_traces(tmp_path: Path) -> None:
    findings_path = tmp_path / "findings.json"
    analyzed = run_cli("analyze", str(MESSY), "--output", str(findings_path), cwd=tmp_path)
    assert analyzed.returncode == 0, analyzed.stderr
    result = json.loads(findings_path.read_text(encoding="utf-8"))
    markdown = run_cli("report", str(MESSY), "--format", "md", "--output", str(tmp_path / "report.md"), cwd=tmp_path)
    assert markdown.returncode == 0, markdown.stderr
    html_report = run_cli("report", str(MESSY), "--format", "html", "--output", str(tmp_path / "report.html"), cwd=tmp_path)
    assert html_report.returncode == 0, html_report.stderr
    md_text = (tmp_path / "report.md").read_text(encoding="utf-8")
    html_text = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "Correlation is not causation" in md_text
    assert "Correlation is not causation" in html_text
    assert "data:image/png;base64," in html_text
    for finding in result["findings"]:
        assert finding["finding_id"] in md_text
        assert finding["finding_id"] in html_text
        traced = run_cli("trace", finding["finding_id"], "--input", str(findings_path), cwd=tmp_path)
        assert traced.returncode == 0, traced.stderr
        traced_finding = json.loads(traced.stdout)
        assert traced_finding["rule_id"] == finding["rule_id"]
        assert traced_finding["evidence"] == finding["evidence"]


@pytest.mark.parametrize("command", ["profile", "analyze", "chart", "report", "trace"])
def test_cli_smoke_each_subcommand(command: str, tmp_path: Path) -> None:
    if command == "profile":
        arguments = ("profile", str(MESSY), "--format", "json", "--output", str(tmp_path / "profile.json"))
    elif command == "analyze":
        arguments = ("analyze", str(MESSY), "--output", str(tmp_path / "findings.json"))
    elif command == "chart":
        arguments = ("chart", str(MESSY), "--output-dir", str(tmp_path / "charts"))
    elif command == "report":
        arguments = ("report", str(MESSY), "--format", "md", "--output", str(tmp_path / "report.md"))
    else:
        (tmp_path / "findings.json").write_text(json.dumps({"findings": [{"finding_id": "F0001", "rule_id": "TEST", "evidence": []}]}), encoding="utf-8")
        arguments = ("trace", "F0001", "--input", str(tmp_path / "findings.json"))
    completed = run_cli(*arguments, cwd=tmp_path)
    assert completed.returncode == 0, completed.stderr


def test_unknown_file_format_fails_loudly(tmp_path: Path) -> None:
    unsupported = tmp_path / "input.txt"
    unsupported.write_text("x", encoding="utf-8")
    completed = run_cli("profile", str(unsupported), cwd=tmp_path)
    assert completed.returncode == 2
    assert "Unsupported input format" in completed.stderr


def test_profile_default_outputs_a_readable_table(tmp_path: Path) -> None:
    completed = run_cli("profile", str(MESSY), cwd=tmp_path)
    assert completed.returncode == 0, completed.stderr
    assert "COLUMN" in completed.stdout
    assert "MISSING" in completed.stdout
    assert "Sales" in completed.stdout
