"""
Mizan.ai — "Know VAT & ZATCA" offline extraction.

PDF -> Docling (structured markdown + preserved table structure).
XLSX/CSV -> direct pandas parse, bypassing Docling entirely for files that
are already structured. Docling/pandas are imported lazily inside each
function, not at module import time, so the rest of this package stays
importable in environments without the heavy offline-only dependencies
installed (see offline/requirements.txt).
"""

from typing import Any, Dict, List


def _sanitize_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """pandas' to_dict(orient="records") represents a missing cell as
    float('nan'), which json.dumps() (used by every FastAPI response,
    including services/data_extraction/main.py's) cannot serialize —
    "Out of range float values are not JSON compliant". NaN != NaN is the
    standard way to test for it without importing pandas/numpy here."""
    return [
        {k: (None if isinstance(v, float) and v != v else v) for k, v in record.items()}
        for record in records
    ]


def extract_pdf(path: str) -> Dict[str, Any]:
    """Converts a PDF to structured markdown, preserving table structure
    (not flattened). Returns {"markdown": str, "tables": [{"column_names":
    [...], "rows": [...]}]}. Language is NOT re-detected here — it's known
    from the source path/URL per the ingestion catalog, tagged by the
    caller (ingest.py)."""
    from docling.document_converter import DocumentConverter

    converter = DocumentConverter()
    result = converter.convert(path)
    document = result.document

    markdown = document.export_to_markdown()

    tables: List[Dict[str, Any]] = []
    for table in document.tables:
        df = table.export_to_dataframe()
        tables.append({
            "column_names": [str(c) for c in df.columns],
            "rows": _sanitize_records(df.to_dict(orient="records")),
        })

    return {"markdown": markdown, "tables": tables}


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # NaN
        return True
    return str(value).strip() == ""


def _find_header_row(raw: "Any", min_non_blank: int = 2) -> int:
    """Real-world exports (SAP/ERP ledgers, bank/customer statements, the
    ZATCA e-invoice Data Dictionary) commonly prepend 1-3 title/metadata
    rows above the actual header row — a report title and a
    "Customer: X | Period: Y" line, each with exactly one wide cell, then
    a blank spacer row. Blindly using row 0 as the header (pandas' default)
    turns the real header into a data row and leaves every column
    unrecognizable downstream. A title/metadata row has at most 1 non-blank
    cell; the real header row has several (one per column), so the first
    row with at least `min_non_blank` non-blank cells is a safe, general
    signal — confirmed against real ledger/statement exports and the
    existing well-formed sheets (header already on row 0), both of which
    this returns unchanged."""
    for i in range(len(raw)):
        row = raw.iloc[i]
        non_blank = sum(0 if _is_blank(v) else 1 for v in row)
        if non_blank >= min_non_blank:
            return i
    return 0


def _column_names_from_row(header_row: "Any") -> List[str]:
    return [
        f"Unnamed: {i}" if _is_blank(value) else str(value).strip()
        for i, value in enumerate(header_row)
    ]


def _preamble_lines(raw: "Any", header_idx: int) -> List[str]:
    """The title/metadata rows _find_header_row() skips over (e.g. a report
    title, or a "Period: X | Seller VAT No: Y" line) aren't data — but
    they're also not nothing: real-world exports often carry genuinely
    useful facts there that never appear anywhere else in the sheet. Kept
    as plain text lines (non-blank cells joined) rather than parsed here —
    parsing/guessing what they mean is the caller's job (see
    features/common/document_extraction.py's VAT-number sniff), same
    division of responsibility this module already uses for "tables" vs
    "line_items"."""
    lines = []
    for i in range(header_idx):
        row = raw.iloc[i]
        cells = [str(v).strip() for v in row if not _is_blank(v)]
        if cells:
            lines.append(" ".join(cells))
    return lines


def _table_from_raw(raw: "Any") -> Dict[str, Any]:
    header_idx = _find_header_row(raw)
    column_names = _column_names_from_row(raw.iloc[header_idx])
    data = raw.iloc[header_idx + 1:].copy()
    data.columns = column_names
    data = data.dropna(how="all")
    return {
        "column_names": column_names,
        "rows": _sanitize_records(data.to_dict(orient="records")),
        "preamble": _preamble_lines(raw, header_idx),
    }


def extract_xlsx_or_csv(path: str) -> List[Dict[str, Any]]:
    """Direct pandas parse — bypasses Docling entirely for already-
    structured files (the Data Dictionary, code lists). One table per
    non-empty sheet for XLSX — some workbooks carry real content on a
    later sheet (e.g. a version-history/column-legend sheet alongside the
    main data), not just the first one, so every sheet is considered and
    only genuinely blank sheets (no cells at all) are dropped. CSV has no
    sheet concept, so always exactly one table. Reads raw (header=None)
    first so the real header row can be auto-detected — see
    _find_header_row()."""
    import pandas as pd

    if path.lower().endswith(".csv"):
        raw = pd.read_csv(path, header=None)
        return [_table_from_raw(raw)]

    sheets = pd.read_excel(path, sheet_name=None, header=None)
    tables = []
    for raw in sheets.values():
        if raw.dropna(how="all").empty:
            continue
        tables.append(_table_from_raw(raw))
    return tables
