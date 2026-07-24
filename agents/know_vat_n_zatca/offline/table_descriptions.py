"""
Mizan.ai — "Know VAT & ZATCA" table-pointer descriptions.

Deterministic, non-AI — per Mohammed's instruction (2026-07-20), this repo
does NOT call the Anthropic API. The real Claude-based version (a proper
authored summary of what the table is about) is specced, not implemented —
see CLAUDE_STEPS_SPEC.md.

This is NOT just "column names + row count" though — that version was
tested for real and confirmed to fail: a query like "What are the penalties
for late VAT registration?" scored the VAT Penalties table far too low to
be retrieved, because the embedded text ("...columns: violation_description,
penalty.") shares almost no vocabulary with real questions. This version
folds in actual row content — the real violation types and penalty
amounts — so the embedding has real semantic material to match against,
while staying 100% deterministic (no model call, just string formatting).
"""

from typing import Any, Dict, List


def _format_row(row: Dict[str, Any]) -> str:
    """One row -> one readable fragment. Column names that are just
    positional placeholders ("0", "1", "Unnamed: 3" — common on
    PDF-extracted tables where Docling didn't recover a real header row)
    are dropped; the cell values are kept either way, since the values
    themselves are where the real content is."""
    parts = []
    for key, value in row.items():
        value_str = "" if value is None else str(value).strip()
        if not value_str or value_str.lower() == "nan":
            continue
        key_str = "" if key is None else str(key).strip()
        if key_str and not key_str.isdigit() and not key_str.lower().startswith("unnamed"):
            parts.append(f"{key_str}: {value_str}")
        else:
            parts.append(value_str)
    return " | ".join(parts)


def generate_placeholder_description(
    document_type: str,
    column_names: List[str],
    rows: List[Dict[str, Any]],
    language: str,
    max_chars: int = 1500,
) -> str:
    """Column names + row count, same as before, PLUS as much real row
    content as fits in max_chars — small tables (the common case here)
    get their entire content embedded; large tables get a leading sample
    rather than nothing. max_chars keeps this bounded for the rare large
    table (e.g. the 152-row e-invoice Data Dictionary) so the embedding
    doesn't balloon — the full row data is always available verbatim via
    SQLite once retrieval matches the table at all, this text only has to
    be good enough to BE matched."""
    row_count = len(rows)
    columns_text = ", ".join(column_names)

    formatted_rows: List[str] = []
    used_chars = 0
    for row in rows:
        fragment = _format_row(row)
        if not fragment:
            continue
        if formatted_rows and used_chars + len(fragment) > max_chars:
            break
        formatted_rows.append(fragment)
        used_chars += len(fragment)
    sample_text = "; ".join(formatted_rows)

    if language == "ar":
        base = f"جدول من {document_type} يحتوي على {row_count} صف بالأعمدة التالية: {columns_text}."
        if sample_text:
            base += f" محتوى الجدول: {sample_text}"
        return base

    base = f"A table from {document_type} with {row_count} rows, containing the following columns: {columns_text}."
    if sample_text:
        base += f" Table content: {sample_text}"
    return base
