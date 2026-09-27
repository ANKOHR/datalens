"""CSV and XLSX ingestion with explicit, auditable cleanup decisions."""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook


class DataLensError(Exception):
    """An input or output error suitable for a concise CLI message."""


@dataclass
class Table:
    source_name: str
    headers: list[str]
    rows: list[list[Any]]
    row_numbers: list[int]
    decisions: list[dict[str, Any]]
    sha256: str


def value_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return format(value, ".15g")
    return str(value)


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip().casefold() in {"", "na", "n/a", "null", "none", "nan"})


def _unique_headers(values: list[str]) -> list[str]:
    result: list[str] = []
    counts: dict[str, int] = {}
    for index, original in enumerate(values, start=1):
        name = original.strip() or f"column_{index}"
        counts[name] = counts.get(name, 0) + 1
        result.append(name if counts[name] == 1 else f"{name}_{counts[name]}")
    return result


def _header_score(row: list[Any], width: int) -> float:
    cells = [value for value in row if not _empty(value)]
    if len(cells) < 2:
        return -100.0
    strings = sum(isinstance(value, str) and not value.strip().replace(".", "", 1).isdigit() for value in cells)
    unique = len({value_text(value).strip().casefold() for value in cells})
    density = len(cells) / max(width, 1)
    return len(cells) * 2.0 + strings * 1.4 + unique * 0.4 + density


def _choose_header(rows: list[list[Any]], width: int, limit: int = 12) -> int:
    candidates = range(min(len(rows), limit))
    best = max(candidates, key=lambda index: (_header_score(rows[index], width), -index), default=0)
    if _header_score(rows[best], width) < 0:
        raise DataLensError("Could not identify a header row with at least two non-empty cells")
    return best


def _table_from_rows(source: Path, raw_rows: list[list[Any]], decisions: list[dict[str, Any]], header_start: int, header_end: int, max_width: int) -> Table:
    if max_width == 0:
        raise DataLensError("The input contains no cells")
    header_parts: list[list[str]] = [[] for _ in range(max_width)]
    for row in raw_rows[header_start:header_end]:
        for column in range(max_width):
            value = row[column] if column < len(row) else None
            if not _empty(value):
                part = value_text(value).strip()
                if not header_parts[column] or header_parts[column][-1] != part:
                    header_parts[column].append(part)
    headers = _unique_headers([".".join(parts) for parts in header_parts])
    rows: list[list[Any]] = []
    row_numbers: list[int] = []
    padded = clipped = blank = 0
    for zero_index in range(header_end, len(raw_rows)):
        raw = list(raw_rows[zero_index])
        if all(_empty(value) for value in raw):
            blank += 1
            continue
        if len(raw) < max_width:
            padded += 1
            raw.extend([None] * (max_width - len(raw)))
        elif len(raw) > max_width:
            clipped += 1
            raw = raw[:max_width]
        rows.append(raw)
        row_numbers.append(zero_index + 1)
    if padded:
        decisions.append({"decision": "ragged_rows_padded", "row_count": padded, "width": max_width})
    if clipped:
        decisions.append({"decision": "ragged_rows_clipped", "row_count": clipped, "width": max_width})
    if blank:
        decisions.append({"decision": "blank_rows_skipped", "row_count": blank})
    decisions.append({"decision": "header_selected", "first_row": header_start + 1, "last_row": header_end, "columns": headers})
    decisions.append({"decision": "source_row_numbers", "basis": "physical one-based row numbers"})
    return Table(source.name, headers, rows, row_numbers, decisions, hashlib.sha256(source.read_bytes()).hexdigest())


def read_csv(source: Path) -> Table:
    raw_bytes = source.read_bytes()
    text = None
    encoding = None
    for candidate in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw_bytes.decode(candidate)
            encoding = candidate
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise DataLensError("Could not decode CSV input")
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
        sniffed = True
    except csv.Error:
        counts = {candidate: sample.count(candidate) for candidate in (",", ";", "\t", "|")}
        delimiter = max(counts, key=lambda candidate: (counts[candidate], -",;\t|".index(candidate)))
        sniffed = False
    parsed = [list(row) for row in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)]
    if not parsed:
        raise DataLensError("The CSV input is empty")
    width = max((len(row) for row in parsed), default=0)
    header_index = _choose_header(parsed, width)
    decisions: list[dict[str, Any]] = [
        {"decision": "encoding_selected", "encoding": encoding},
        {"decision": "delimiter_selected", "delimiter": "tab" if delimiter == "\t" else delimiter, "method": "sniffer" if sniffed else "count_fallback"},
        {"decision": "header_search", "rows_examined": min(len(parsed), 12), "selected_row": header_index + 1},
    ]
    return _table_from_rows(source, parsed, decisions, header_index, header_index + 1, width)


def read_xlsx(source: Path) -> Table:
    try:
        workbook = load_workbook(source, read_only=False, data_only=True)
    except Exception as exc:
        raise DataLensError(f"Could not read XLSX workbook: {exc}") from exc
    sheet = next((sheet for sheet in workbook.worksheets if sheet.max_row and sheet.max_column), None)
    if sheet is None:
        raise DataLensError("The XLSX workbook has no populated worksheets")
    raw = [list(row) for row in sheet.iter_rows(values_only=True)]
    width = max((len(row) for row in raw), default=0)
    merged_ranges = sorted(str(item) for item in sheet.merged_cells.ranges)
    merged_top = [item for item in sheet.merged_cells.ranges if item.min_row <= 5]
    if merged_top:
        header_start = min(item.min_row for item in merged_top) - 1
        header_end = max(item.max_row for item in merged_top) + 1
        if header_end >= len(raw) or _header_score(raw[header_end - 1], width) < 0:
            header_start = _choose_header(raw, width)
            header_end = header_start + 1
        decisions: list[dict[str, Any]] = [{"decision": "xlsx_sheet_selected", "sheet": sheet.title}]
        decisions.append({"decision": "merged_headers_flattened", "ranges": merged_ranges, "first_row": header_start + 1, "last_row": header_end})
    else:
        header_start = _choose_header(raw, width)
        header_end = header_start + 1
        decisions = [{"decision": "xlsx_sheet_selected", "sheet": sheet.title}, {"decision": "merged_headers_flattened", "ranges": []}]
        decisions.append({"decision": "header_search", "rows_examined": min(len(raw), 12), "selected_row": header_start + 1})
    for merged_range in merged_top:
        if merged_range.min_row > header_end:
            continue
        merged_value = raw[merged_range.min_row - 1][merged_range.min_col - 1]
        for row_index in range(merged_range.min_row - 1, min(merged_range.max_row, len(raw))):
            for column_index in range(merged_range.min_col - 1, min(merged_range.max_col, len(raw[row_index]))):
                raw[row_index][column_index] = merged_value
    workbook.close()
    return _table_from_rows(source, raw, decisions, header_start, header_end, width)


def load_table(filename: str | Path) -> Table:
    source = Path(filename)
    if not source.exists() or not source.is_file():
        raise DataLensError(f"Input file does not exist: {source}")
    suffix = source.suffix.casefold()
    if suffix == ".csv":
        return read_csv(source)
    if suffix == ".xlsx":
        return read_xlsx(source)
    raise DataLensError(f"Unsupported input format '{source.suffix}'. Use .csv or .xlsx")
