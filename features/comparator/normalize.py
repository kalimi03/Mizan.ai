"""
Mizan.ai — Comparator (Feature C) column normalization: turns
features/common/document_extraction.py's raw extraction output ("tables" of
{column_names, rows}, as produced by pandas for xlsx/csv or Docling for
PDF) into ReconciliationRow objects matching.py can work with.

Header-synonym heuristic, not a hardcoded Odoo/SAP schema — the handoff
doc's own Input Formats table was cut off exactly where it should have
specified the expected Odoo export column layout, so this is a best-effort
default, not a validated spec. Revisit against a real sample export.

Amounts are normalized to their absolute value everywhere (including the
debit/credit-column fallback) — reconciliation is about whether a
transaction shows up on both sides, and DR/CR sign conventions commonly
differ between a ledger export and an external statement for the exact
same real-world transaction.
"""

import re
from datetime import date as date_type
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from .models import ColumnMapping, ReconciliationRow, Source

_FIELD_SYNONYMS: Dict[str, List[str]] = {
    "reference": [
        "reference", "ref", "ref no", "ref no.", "ref number", "reference number",
        "doc no", "doc no.", "doc number", "document number", "document no",
        "invoice no", "invoice no.", "invoice number", "entry number", "move name",
        "transaction id", "txn id", "voucher no", "voucher number", "cheque no", "check no", "receipt no",
    ],
    "date": ["date", "posting date", "transaction date", "entry date", "invoice date", "value date"],
    # Deliberately no "balance" or "value" here: a Balance column on a
    # ledger export is a running total (not the transaction amount) and
    # commonly coexists with real Debit/Credit columns; "value" collides
    # with the very common "Value Date" header (a date field, not an
    # amount — confirmed matching it wrongly before this was removed).
    # Both would silently feed the wrong number into reconciliation rather
    # than the actual per-row transaction value. See _row_amount() for the
    # debit/credit-first fallback order this relies on.
    "amount": ["amount", "total"],
    # "dr"/"cr" are extremely common real bank/ERP abbreviations for
    # debit/credit — only safe to add as whole-word tokens (see
    # _find_column's word-boundary matching below); as a plain substring
    # check "dr" would false-positive on ordinary words like "Address".
    "debit": ["debit", "dr"],
    "credit": ["credit", "cr"],
    "description": [
        "description", "label", "narration", "narrative", "particulars", "partner", "memo",
        "communication", "details", "remarks", "vendor", "supplier", "customer", "counterparty", "payee",
    ],
}


class NormalizationError(RuntimeError):
    """Raised when no table in the extraction result has recognizable
    reference/date/amount columns — the caller should surface this as a
    422 asking for a spreadsheet-like or invoice-like input instead."""


def _normalize_header(header: str) -> str:
    return re.sub(r"\s+", " ", str(header).strip().lower()).strip(".:")


def _find_column(column_names: List[str], field: str) -> Optional[str]:
    normalized = {_normalize_header(c): c for c in column_names}
    synonyms = _FIELD_SYNONYMS[field]

    for syn in synonyms:
        if syn in normalized:
            return normalized[syn]
    # Word-boundary match, not a raw substring check — a plain "syn in
    # norm_header" would let a short synonym like "dr" match mid-word inside
    # an unrelated header (e.g. "Address"), which is exactly the class of
    # false-positive bug _FIELD_SYNONYMS's "balance"/"value" removal above
    # was fixing. \b still matches a synonym phrase inside a longer header
    # normally ("ref" in "ref no", "particulars" in "particulars of
    # transaction") since those are real word boundaries too.
    for norm_header, original in normalized.items():
        if any(re.search(rf"\b{re.escape(syn)}\b", norm_header) for syn in synonyms):
            return original
    return None


def _parse_amount(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        if isinstance(value, float) and value != value:  # NaN
            return None
    except Exception:
        pass
    if isinstance(value, (int, float, Decimal)):
        try:
            return abs(Decimal(str(value)))
        except InvalidOperation:
            return None

    text = str(value).strip()
    if not text:
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", text)
    if not cleaned or cleaned in ("-", "."):
        return None
    try:
        return abs(Decimal(cleaned))
    except InvalidOperation:
        return None


_DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d"]


def _parse_date(value: Any) -> Optional[date_type]:
    if value is None:
        return None
    if isinstance(value, date_type):
        return value if not isinstance(value, datetime) else value.date()
    if hasattr(value, "date") and callable(value.date):
        try:
            return value.date()  # pandas.Timestamp
        except Exception:
            pass

    text = str(value).strip()
    if not text or text.lower() == "nat":
        return None

    # ISO (YYYY-MM-DD) is unambiguous regardless of day/month order
    # conventions — try it explicitly first. dateutil.parser's dayfirst=True
    # (needed below for genuinely ambiguous DD/MM/YYYY-style strings) has
    # been confirmed to incorrectly swap month/day even on ISO-formatted
    # strings if given a chance, so ISO must be resolved before dateutil
    # ever sees the string.
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        pass

    try:
        from dateutil import parser as dateutil_parser

        return dateutil_parser.parse(text, dayfirst=True).date()
    except Exception:
        pass

    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _row_amount(row: Dict[str, Any], amount_col: Optional[str], debit_col: Optional[str], credit_col: Optional[str]) -> Optional[Decimal]:
    # Debit/Credit checked first: when a table has a real debit/credit split,
    # it's the more specific, more reliable signal for the actual per-row
    # transaction value than whatever also matched the generic "amount"
    # synonyms (e.g. a "Total"/"Value" column with a different meaning on
    # that particular export) — see the comment on _FIELD_SYNONYMS above.
    if debit_col:
        debit = _parse_amount(row.get(debit_col))
        if debit:
            return debit
    if credit_col:
        credit = _parse_amount(row.get(credit_col))
        if credit:
            return credit
    if amount_col:
        parsed = _parse_amount(row.get(amount_col))
        if parsed is not None:
            return parsed
    return None


def _select_best_table(tables: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Picks the first table whose columns include a recognizable amount
    field (the one truly required field — reference/date help matching
    but a table with amounts alone is still usable)."""
    for table in tables:
        column_names = table.get("column_names", [])
        if _find_column(column_names, "amount") or (
            _find_column(column_names, "debit") or _find_column(column_names, "credit")
        ):
            return table
    return None


def normalize_tables(tables: List[Dict[str, Any]], source: Source) -> Tuple[List[ReconciliationRow], ColumnMapping]:
    """Converts the best-matching table's rows into ReconciliationRow
    objects. Raises NormalizationError if no table has a usable amount
    column. Also returns the guessed column mapping — since it's a
    best-effort heuristic, not a validated spec, the caller surfaces it
    for the user to review rather than trusting it silently."""
    table = _select_best_table(tables)
    if table is None:
        raise NormalizationError(
            "Could not find a recognizable amount/debit/credit column in the uploaded document — "
            "expected a spreadsheet-like table with reference/date/amount columns."
        )

    column_names = table["column_names"]
    reference_col = _find_column(column_names, "reference")
    date_col = _find_column(column_names, "date")
    amount_col = _find_column(column_names, "amount")
    debit_col = _find_column(column_names, "debit")
    credit_col = _find_column(column_names, "credit")
    description_col = _find_column(column_names, "description")

    mapping = ColumnMapping(
        reference_column=reference_col, date_column=date_col, amount_column=amount_col,
        debit_column=debit_col, credit_column=credit_col, description_column=description_col,
    )

    rows: List[ReconciliationRow] = []
    for i, raw_row in enumerate(table.get("rows", [])):
        amount = _row_amount(raw_row, amount_col, debit_col, credit_col)
        if amount is None:
            continue  # a row with no parseable amount can't be matched or reviewed meaningfully

        reference = str(raw_row.get(reference_col)).strip() if reference_col and raw_row.get(reference_col) not in (None, "") else None
        if reference == "nan":
            reference = None

        rows.append(ReconciliationRow(
            row_id=f"{source}_{i}",
            source=source,
            reference=reference,
            date=_parse_date(raw_row.get(date_col)) if date_col else None,
            amount=amount,
            description=str(raw_row.get(description_col)).strip() if description_col and raw_row.get(description_col) not in (None, "") else None,
            raw=raw_row,
        ))

    return rows, mapping


def normalize_line_items(line_items: List[Dict[str, Any]], issue_date: Optional[str], source: Source) -> Tuple[List[ReconciliationRow], ColumnMapping]:
    """Fallback path for the (unlikely for Comparator, but supported by
    document_extraction.py) UBL/XML case — line_items is already
    structured, just needs remapping onto ReconciliationRow's field names.
    The "mapping" here names the extraction fields used rather than
    spreadsheet columns, kept for the same reason — visibility into what
    was used, not a silent assumption."""
    mapping = ColumnMapping(
        reference_column="line_id",
        date_column="issue_date" if issue_date else None,
        amount_column="taxable_base",
        description_column="description",
    )
    parsed_date = _parse_date(issue_date) if issue_date else None
    rows: List[ReconciliationRow] = []
    for item in line_items:
        amount = _parse_amount(item.get("taxable_base"))
        if amount is None:
            continue
        rows.append(ReconciliationRow(
            row_id=f"{source}_{item.get('line_id', len(rows))}",
            source=source,
            reference=str(item.get("line_id")) if item.get("line_id") else None,
            date=parsed_date,
            amount=amount,
            description=item.get("description"),
            raw=item,
        ))
    return rows, mapping


def normalize_extraction(extraction: Dict[str, Any], source: Source) -> Tuple[List[ReconciliationRow], ColumnMapping]:
    """Single entry point services/comparator/main.py calls — dispatches
    to the tables path (xlsx/csv/pdf) or the line_items path (ubl_xml),
    whichever the extraction result actually populated."""
    tables = extraction.get("tables") or []
    if tables:
        return normalize_tables(tables, source)

    line_items = extraction.get("line_items") or []
    if line_items:
        return normalize_line_items(line_items, extraction.get("issue_date"), source)

    raise NormalizationError(
        "Extraction produced no structured table or line items — expected a spreadsheet-like "
        "or invoice-like input document."
    )
