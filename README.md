# DataLens

DataLens is a deterministic, CPU-only command-line tool for first-pass analysis of CSV and XLSX files. It profiles columns, audits missing values, flags heuristic anomalies, estimates trends, reports Pearson correlations, and produces Markdown, self-contained HTML, and PNG charts. Findings are heuristic or statistical observations, not verified business facts. Trend estimates describe the supplied data and are not forecasts.

The included spreadsheet fixtures are fictional synthetic examples created for testing. They contain planted issues and do not represent a real business.

## Quickstart

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m pytest
datalens profile tests/fixtures/messy.csv
datalens analyze tests/fixtures/messy.csv --output findings.json
datalens report tests/fixtures/messy.csv --format html --output report.html
datalens trace F001 --input findings.json
```

The `profile`, `analyze`, `chart`, and `report` commands accept `.csv` and `.xlsx` files. Each command writes a deterministic `datalens-audit.jsonl` in the current directory. The analyze command writes findings as JSON. Report output includes evidence pointers with source row numbers and observed values. The `trace` command resolves an ID from a findings JSON file.

## Architecture

```text
CSV or XLSX
    |
    v
Ingestion and header cleanup -> profiling -> deterministic rule engine
                                                  |          |
                                                  v          v
                                           findings JSON   audit JSONL
                                                  |
                                +-----------------+----------------+
                                v                 v                v
                           Markdown          self-contained HTML  PNG charts
```

CSV decoding tries UTF-8 with BOM, UTF-8, then Windows-1252. Delimiters are sniffed with a deterministic fallback. Ragged rows are padded or clipped to a stable table width. XLSX merged header cells are flattened to dotted names. These decisions are recorded in the audit trail. Source row pointers refer to physical one-based rows in the input file.

## Analysis rules and honesty

Numeric outliers use absolute z-scores of at least 3 and Tukey IQR fences at 1.5 times the interquartile range. Rare categories have a count at or below the larger of one row or one percent of the nonmissing population. Date gaps compare each positive interval to three times the median positive interval. Numeric correlations are pairwise Pearson coefficients and default to an absolute threshold of 0.5. Rule IDs and thresholds are included with every finding. All reported claims are labeled as heuristic or statistical observations, and the report states that correlation is not causation.

The analysis uses no randomness. The chart style fixes fonts, dimensions, colors, and layout; there is no RNG seed because no random operation is used. File hashes, ingestion decisions, fired rules, and emitted findings form the audit trail. Timestamps and machine-dependent paths are omitted to keep repeated runs byte-identical.

## HTML report screenshots

Run `datalens report input.xlsx --format html --output report.html`, open the report in a browser, and capture a full-page screenshot. HTML reports embed generated chart PNGs as data URLs, so the report can be shared as a single file.
