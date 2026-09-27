"""Deterministic profiles, anomaly heuristics, trends, and correlations."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import date, datetime
from statistics import mean, median
from typing import Any

from .ingest import Table, value_text

MISSING_TEXT = {"", "na", "n/a", "null", "none", "nan"}
CORRELATION_THRESHOLD = 0.5
ZSCORE_THRESHOLD = 3.0
IQR_MULTIPLIER = 1.5
RARE_SHARE = 0.01
DATE_GAP_MULTIPLIER = 3.0


def is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip().casefold() in MISSING_TEXT)


def parse_number(value: Any) -> float | None:
    if isinstance(value, bool) or is_missing(value):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = str(value).strip().replace(",", "")
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    text = re.sub(r"^[\$£€]\s*", "", text)
    text = text.removesuffix("%")
    try:
        number = float(text)
    except ValueError:
        return None
    if negative:
        number = -number
    return number if math.isfinite(number) else None


def parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or is_missing(value):
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for pattern in ("%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y", "%d-%b-%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def clean_number(value: float | None) -> float | None:
    if value is None:
        return None
    rounded = round(float(value), 8)
    return 0.0 if rounded == 0 else rounded


def _quartile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def infer_column(values: list[Any]) -> tuple[str, list[float], list[date]]:
    present = [value for value in values if not is_missing(value)]
    if not present:
        return "all_null", [], []
    numeric = [parse_number(value) for value in present]
    dates = [parse_date(value) for value in present]
    numeric_count = sum(value is not None for value in numeric)
    date_count = sum(value is not None for value in dates)
    if date_count / len(present) >= 0.8 and date_count >= 2:
        return ("date" if date_count == len(present) else "date_mixed"), [], [value for value in dates if value is not None]
    if numeric_count / len(present) >= 0.8:
        return ("numeric" if numeric_count == len(present) else "numeric_mixed"), [value for value in numeric if value is not None], []
    if all(isinstance(value, bool) for value in present):
        return "boolean", [], []
    return "categorical", [], []


def profile_table(table: Table) -> dict[str, Any]:
    columns: list[dict[str, Any]] = []
    for index, name in enumerate(table.headers):
        values = [row[index] if index < len(row) else None for row in table.rows]
        present = [value for value in values if not is_missing(value)]
        missing = len(values) - len(present)
        inferred, numbers, dates = infer_column(values)
        item: dict[str, Any] = {
            "name": name,
            "inferred_type": inferred,
            "row_count": len(values),
            "non_missing_count": len(present),
            "missing_count": missing,
            "missing_rate": clean_number(missing / len(values)) if values else 0.0,
            "cardinality": len({value_text(value).strip().casefold() for value in present}),
        }
        if inferred.startswith("numeric") and numbers:
            item["numeric"] = {
                "parsed_count": len(numbers),
                "min": clean_number(min(numbers)),
                "max": clean_number(max(numbers)),
                "mean": clean_number(mean(numbers)),
                "median": clean_number(median(numbers)),
            }
        elif inferred.startswith("date") and dates:
            item["date_min"] = min(dates).isoformat()
            item["date_max"] = max(dates).isoformat()
        else:
            counter = Counter(value_text(value) for value in present)
            item["top_values"] = [{"value": key, "count": count} for key, count in sorted(counter.items(), key=lambda pair: (-pair[1], pair[0].casefold(), pair[0]))[:5]]
        columns.append(item)
    canonical_rows = [tuple("" if is_missing(value) else value_text(value).strip().casefold() for value in row) for row in table.rows]
    duplicate_rows = len(canonical_rows) - len(set(canonical_rows))
    return {"source": table.source_name, "row_count": len(table.rows), "column_count": len(table.headers), "duplicate_row_count": duplicate_rows, "columns": columns}


def _cell(table: Table, column_index: int, row_index: int) -> dict[str, Any]:
    value = table.rows[row_index][column_index] if column_index < len(table.rows[row_index]) else None
    if isinstance(value, (date, datetime)):
        value = value_text(value)
    return {"column": table.headers[column_index], "row": table.row_numbers[row_index], "value": value}


def _column_values(table: Table, index: int) -> list[Any]:
    return [row[index] if index < len(row) else None for row in table.rows]


def _pearson(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None
    left = [pair[0] for pair in pairs]
    right = [pair[1] for pair in pairs]
    left_mean, right_mean = mean(left), mean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in pairs)
    left_sum = sum((x - left_mean) ** 2 for x in left)
    right_sum = sum((y - right_mean) ** 2 for y in right)
    if left_sum == 0 or right_sum == 0:
        return None
    return numerator / math.sqrt(left_sum * right_sum)


def _make_finding(rule_id: str, title: str, explanation: str, threshold: Any, evidence: list[dict[str, Any]], details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "title": title,
        "observation_type": "heuristic or statistical observation",
        "rule_id": rule_id,
        "threshold": threshold,
        "explanation": explanation,
        "evidence": evidence,
        "details": details or {},
    }


def _anomalies(table: Table, profiles: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for column_index, column in enumerate(profiles["columns"]):
        values = _column_values(table, column_index)
        present_indices = [index for index, value in enumerate(values) if not is_missing(value)]
        inferred = column["inferred_type"]
        if inferred == "all_null":
            findings.append(_make_finding("ALL_NULL_COLUMN", f"Column {column['name']} is entirely missing", "Every source cell in this column is missing. This is a structural data-quality observation.", {"missing_count": len(values)}, [_cell(table, column_index, index) for index in range(len(values))]))
            continue
        unique_values = {value_text(values[index]).strip().casefold() for index in present_indices}
        if len(unique_values) == 1 and present_indices:
            findings.append(_make_finding("CONSTANT_COLUMN", f"Column {column['name']} is constant", "All observed non-missing cells have the same value, so this column carries no variation in this file.", {"unique_values": 1}, [_cell(table, column_index, index) for index in present_indices]))
        if inferred.startswith("numeric"):
            parsed = [(index, parse_number(values[index])) for index in present_indices]
            parsed = [(index, number) for index, number in parsed if number is not None]
            numbers = [number for _, number in parsed]
            if len(numbers) >= 4:
                avg, deviation = mean(numbers), math.sqrt(sum((number - mean(numbers)) ** 2 for number in numbers) / len(numbers))
                if deviation > 0:
                    flagged = [(index, number, abs(number - avg) / deviation) for index, number in parsed if abs(number - avg) / deviation >= ZSCORE_THRESHOLD]
                    if flagged:
                        evidence = [_cell(table, column_index, index) for index, _, _ in flagged]
                        findings.append(_make_finding("NUMERIC_ZSCORE_3", f"Column {column['name']} has z-score outliers", f"{len(flagged)} observed value(s) are at least {ZSCORE_THRESHOLD:g} population standard deviations from the column mean. This threshold is a heuristic screen.", {"absolute_zscore_at_least": ZSCORE_THRESHOLD}, evidence, {"z_scores": [{"row": table.row_numbers[index], "value": value, "absolute_zscore": clean_number(score)} for index, value, score in flagged]}))
                q1, q3 = _quartile(numbers, 0.25), _quartile(numbers, 0.75)
                spread = q3 - q1
                if spread > 0:
                    lower, upper = q1 - IQR_MULTIPLIER * spread, q3 + IQR_MULTIPLIER * spread
                    flagged_iqr = [(index, number) for index, number in parsed if number < lower or number > upper]
                    if flagged_iqr:
                        evidence = [_cell(table, column_index, index) for index, _ in flagged_iqr]
                        findings.append(_make_finding("NUMERIC_IQR_1_5", f"Column {column['name']} has IQR outliers", f"{len(flagged_iqr)} observed value(s) fall outside Tukey fences at {IQR_MULTIPLIER:g} times the interquartile range. This threshold is a heuristic screen.", {"iqr_multiplier": IQR_MULTIPLIER, "lower_fence": clean_number(lower), "upper_fence": clean_number(upper)}, evidence))
        elif inferred == "categorical":
            counts = Counter(value_text(values[index]).strip() for index in present_indices)
            rare_limit = max(1, math.floor(len(present_indices) * RARE_SHARE))
            likely_identifier = len(counts) > 20 and len(counts) / max(len(present_indices), 1) >= 0.8
            if not likely_identifier:
                for category, count in sorted(counts.items(), key=lambda pair: (pair[0].casefold(), pair[0])):
                    if count <= rare_limit:
                        category_indices = [index for index in present_indices if value_text(values[index]).strip() == category]
                        findings.append(_make_finding("RARE_CATEGORY_1PCT", f"Column {column['name']} contains a rare category", f"Category {category!r} appears {count} time(s), at or below the rare-category limit of {rare_limit}. This is a heuristic review prompt.", {"count_at_most": rare_limit, "share_target": RARE_SHARE, "minimum_count_floor": 1}, [_cell(table, column_index, index) for index in category_indices], {"category": category, "count": count}))
        elif inferred.startswith("date"):
            dated = [(index, parse_date(values[index])) for index in present_indices]
            dated = [(index, value) for index, value in dated if value is not None]
            ordered = sorted(dated, key=lambda pair: (pair[1], pair[0]))
            gaps = [(ordered[index - 1], ordered[index], (ordered[index][1] - ordered[index - 1][1]).days) for index in range(1, len(ordered))]
            intervals = [gap[2] for gap in gaps if gap[2] > 0]
            if intervals:
                typical = median(intervals)
                flagged_gaps = [gap for gap in gaps if gap[2] > 0 and gap[2] > DATE_GAP_MULTIPLIER * typical]
                if flagged_gaps:
                    evidence = []
                    details = []
                    for before, after, gap_days in flagged_gaps:
                        evidence.extend((_cell(table, column_index, before[0]), _cell(table, column_index, after[0])))
                        details.append({"from_row": table.row_numbers[before[0]], "to_row": table.row_numbers[after[0]], "gap_days": gap_days})
                    findings.append(_make_finding("DATE_GAP_3X_MEDIAN", f"Column {column['name']} has a large date gap", f"{len(flagged_gaps)} interval(s) exceed three times the median positive interval of {clean_number(typical)} day(s). This is a heuristic gap screen.", {"interval_days_greater_than": clean_number(DATE_GAP_MULTIPLIER * typical), "median_interval_days": clean_number(typical), "multiplier": DATE_GAP_MULTIPLIER}, evidence, {"gaps": details}))
    return findings


def _trends(table: Table, profiles: dict[str, Any]) -> list[dict[str, Any]]:
    date_columns = [index for index, column in enumerate(profiles["columns"]) if column["inferred_type"].startswith("date")]
    numeric_columns = [index for index, column in enumerate(profiles["columns"]) if column["inferred_type"].startswith("numeric")]
    findings = []
    for date_index in date_columns:
        parsed_dates = [parse_date(value) for value in _column_values(table, date_index)]
        for numeric_index in numeric_columns:
            buckets: dict[date, list[tuple[int, float]]] = {}
            for row_index, (day, value) in enumerate(zip(parsed_dates, _column_values(table, numeric_index))):
                number = parse_number(value)
                if day is not None and number is not None:
                    buckets.setdefault(day, []).append((row_index, number))
            ordered = sorted((day, mean(value for _, value in items), items) for day, items in buckets.items())
            if len(ordered) < 3:
                continue
            x = [(day - ordered[0][0]).days for day, _, _ in ordered]
            y = [value for _, value, _ in ordered]
            x_mean, y_mean = mean(x), mean(y)
            variance = sum((item - x_mean) ** 2 for item in x)
            slope = sum((x_value - x_mean) * (y_value - y_mean) for x_value, y_value in zip(x, y)) / variance if variance else 0.0
            tolerance = max(abs(y_mean), 1.0) * 1e-9
            direction = "up" if slope > tolerance else "down" if slope < -tolerance else "flat"
            differences = [abs(y[index] - y[index - 1]) for index in range(1, len(y))]
            typical_change = median(differences) if differences else 0.0
            change_points = []
            for index in range(1, len(y)):
                if abs(y[index] - y[index - 1]) > max(2.0 * typical_change, tolerance):
                    change_points.append({"date": ordered[index][0].isoformat(), "before": clean_number(y[index - 1]), "after": clean_number(y[index])})
            evidence: list[dict[str, Any]] = []
            for _, _, source_items in ordered:
                for row_index, _ in source_items:
                    evidence.extend((_cell(table, date_index, row_index), _cell(table, numeric_index, row_index)))
            date_name = table.headers[date_index]
            numeric_name = table.headers[numeric_index]
            findings.append(_make_finding("TREND_DAILY_LINEAR_ESTIMATE", f"Column {numeric_name} has a {direction} daily trend estimate", f"A heuristic linear slope across {len(ordered)} daily buckets is {direction}. This describes these observations and is not a forecast.", {"bucket": "daily", "minimum_distinct_buckets": 3, "flat_slope_tolerance": clean_number(tolerance)}, evidence, {"date_column": date_name, "value_column": numeric_name, "direction": direction, "slope_per_day": clean_number(slope), "bucket_count": len(ordered), "buckets": [{"date": day.isoformat(), "mean": clean_number(value), "count": len(items)} for day, value, items in ordered], "change_points": change_points}))
    return findings


def _correlations(table: Table, profiles: dict[str, Any], threshold: float = CORRELATION_THRESHOLD) -> list[dict[str, Any]]:
    numeric_indices = [index for index, column in enumerate(profiles["columns"]) if column["inferred_type"].startswith("numeric")]
    findings = []
    for left_position, left_index in enumerate(numeric_indices):
        left_values = _column_values(table, left_index)
        for right_index in numeric_indices[left_position + 1:]:
            right_values = _column_values(table, right_index)
            pairs = [(row_index, parse_number(left), parse_number(right)) for row_index, (left, right) in enumerate(zip(left_values, right_values))]
            pairs = [(index, left, right) for index, left, right in pairs if left is not None and right is not None]
            coefficient = _pearson([(left, right) for _, left, right in pairs])
            if coefficient is None or abs(coefficient) < threshold:
                continue
            evidence = []
            for row_index, _, _ in pairs:
                evidence.extend((_cell(table, left_index, row_index), _cell(table, right_index, row_index)))
            direction = "positive" if coefficient >= 0 else "negative"
            findings.append(_make_finding("PEARSON_ABS_AT_LEAST_THRESHOLD", f"Columns {table.headers[left_index]} and {table.headers[right_index]} have a {direction} correlation", f"The pairwise Pearson coefficient is {clean_number(coefficient)} across {len(pairs)} complete row(s). This is a statistical association, not evidence of causation.", {"absolute_pearson_at_least": threshold}, evidence, {"left_column": table.headers[left_index], "right_column": table.headers[right_index], "pearson_r": clean_number(coefficient), "row_count": len(pairs)}))
    return findings


def analyze_table(table: Table, correlation_threshold: float = CORRELATION_THRESHOLD) -> dict[str, Any]:
    profile = profile_table(table)
    findings: list[dict[str, Any]] = []
    for column_index, column in enumerate(profile["columns"]):
        missing_indices = [index for index, value in enumerate(_column_values(table, column_index)) if is_missing(value)]
        if missing_indices:
            rate = len(missing_indices) / max(len(table.rows), 1)
            findings.append(_make_finding("MISSING_VALUE_AUDIT", f"Column {column['name']} has missing values", f"{len(missing_indices)} of {len(table.rows)} cells ({rate:.1%}) are missing. This is a data-quality observation, not an explanation of why values are absent.", {"minimum_missing_count": 1}, [_cell(table, column_index, index) for index in missing_indices], {"missing_count": len(missing_indices), "row_count": len(table.rows), "missing_rate": clean_number(rate)}))
    findings.extend(_anomalies(table, profile))
    findings.extend(_trends(table, profile))
    findings.extend(_correlations(table, profile, correlation_threshold))
    for number, finding in enumerate(findings, start=1):
        finding["finding_id"] = f"F{number:04d}"
    return {
        "schema_version": 1,
        "source": table.source_name,
        "file_sha256": table.sha256,
        "label": "Heuristic and statistical observations, not verified business facts",
        "profile": profile,
        "findings": findings,
        "ingestion_decisions": table.decisions,
    }


def json_bytes(value: Any, pretty: bool = True) -> bytes:
    if pretty:
        content = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    else:
        content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return (content + "\n").encode("utf-8")
