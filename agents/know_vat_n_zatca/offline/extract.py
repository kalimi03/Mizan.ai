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
            "rows": df.to_dict(orient="records"),
        })

    return {"markdown": markdown, "tables": tables}


def extract_xlsx_or_csv(path: str) -> List[Dict[str, Any]]:
    """Direct pandas parse — bypasses Docling entirely for already-
    structured files (the Data Dictionary, code lists). One table per
    non-empty sheet for XLSX — some workbooks carry real content on a
    later sheet (e.g. a version-history/column-legend sheet alongside the
    main data), not just the first one, so every sheet is considered and
    only genuinely blank sheets (no cells at all) are dropped. CSV has no
    sheet concept, so always exactly one table."""
    import pandas as pd

    if path.lower().endswith(".csv"):
        df = pd.read_csv(path)
        return [{"column_names": [str(c) for c in df.columns], "rows": df.to_dict(orient="records")}]

    sheets = pd.read_excel(path, sheet_name=None)
    tables = []
    for df in sheets.values():
        if df.dropna(how="all").empty:
            continue
        tables.append({"column_names": [str(c) for c in df.columns], "rows": df.to_dict(orient="records")})
    return tables
