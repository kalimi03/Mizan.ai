from datetime import date
from decimal import Decimal

import pytest

from features.comparator.normalize import (
    NormalizationError,
    normalize_extraction,
    normalize_line_items,
    normalize_tables,
)


def test_recognizes_common_header_synonyms():
    table = {
        "column_names": ["Reference", "Date", "Amount", "Partner"],
        "rows": [
            {"Reference": "INV-001", "Date": "2026-06-01", "Amount": 1500.00, "Partner": "Acme Trading"},
        ],
    }

    rows, mapping = normalize_tables([table], "sap_odoo")

    assert len(rows) == 1
    row = rows[0]
    assert row.reference == "INV-001"
    assert row.date == date(2026, 6, 1)
    assert row.amount == Decimal("1500.0")
    assert row.description == "Acme Trading"
    assert row.source == "sap_odoo"

    # The guessed mapping is returned so the caller can surface it for
    # review — it's a best-effort heuristic, not a validated spec.
    assert mapping.reference_column == "Reference"
    assert mapping.date_column == "Date"
    assert mapping.amount_column == "Amount"
    assert mapping.description_column == "Partner"
    assert mapping.debit_column is None
    assert mapping.credit_column is None


def test_iso_dates_are_not_misparsed_as_day_first():
    """dateutil's dayfirst=True (needed for genuinely ambiguous DD/MM/YYYY
    strings) has been confirmed to mis-swap month/day on unambiguous ISO
    strings unless ISO is tried first — regression guard for that fix."""
    table = {
        "column_names": ["Date", "Amount"],
        "rows": [{"Date": "2026-06-05", "Amount": 100}],  # unambiguously June 5th
    }

    rows, _ = normalize_tables([table], "sap_odoo")

    assert rows[0].date == date(2026, 6, 5)


def test_ambiguous_slash_dates_use_day_first_convention():
    table = {
        "column_names": ["Date", "Amount"],
        "rows": [{"Date": "05/06/2026", "Amount": 100}],  # day-first -> June 5th
    }

    rows, _ = normalize_tables([table], "sap_odoo")

    assert rows[0].date == date(2026, 6, 5)


def test_debit_credit_fallback_when_no_amount_column():
    table = {
        "column_names": ["Doc No", "Posting Date", "Debit", "Credit"],
        "rows": [
            {"Doc No": "DOC-1", "Posting Date": "2026-06-05", "Debit": 1500.0, "Credit": 0.0},
            {"Doc No": "DOC-2", "Posting Date": "2026-06-10", "Debit": 0.0, "Credit": 320.0},
        ],
    }

    rows, mapping = normalize_tables([table], "second_doc")

    assert len(rows) == 2
    assert rows[0].amount == Decimal("1500.0")
    assert rows[1].amount == Decimal("320.0")  # credit-only row still gets a positive amount
    assert mapping.amount_column is None
    assert mapping.debit_column == "Debit"
    assert mapping.credit_column == "Credit"


def test_amount_with_currency_symbol_and_thousands_separator():
    table = {
        "column_names": ["Amount"],
        "rows": [{"Amount": "SAR 1,234.56"}],
    }

    rows, _ = normalize_tables([table], "sap_odoo")

    assert rows[0].amount == Decimal("1234.56")


def test_row_with_unparseable_amount_is_skipped_not_crashed():
    table = {
        "column_names": ["Reference", "Amount"],
        "rows": [
            {"Reference": "INV-1", "Amount": 100},
            {"Reference": "INV-2", "Amount": None},
            {"Reference": "INV-3", "Amount": "n/a"},
        ],
    }

    rows, _ = normalize_tables([table], "sap_odoo")

    assert len(rows) == 1
    assert rows[0].reference == "INV-1"


def test_picks_first_table_with_a_usable_amount_column():
    """Multi-sheet xlsx exports: a legend/notes sheet with no amount column
    should be skipped in favor of the actual data sheet."""
    tables = [
        {"column_names": ["Notes", "Legend"], "rows": [{"Notes": "see below", "Legend": "x"}]},
        {"column_names": ["Reference", "Amount"], "rows": [{"Reference": "INV-1", "Amount": 50}]},
    ]

    rows, mapping = normalize_tables(tables, "sap_odoo")

    assert len(rows) == 1
    assert rows[0].reference == "INV-1"
    assert mapping.amount_column == "Amount"


def test_no_usable_table_raises_normalization_error():
    tables = [{"column_names": ["Name", "Notes"], "rows": [{"Name": "x", "Notes": "y"}]}]

    with pytest.raises(NormalizationError):
        normalize_tables(tables, "second_doc")


def test_normalize_extraction_dispatches_to_tables_path():
    extraction = {
        "tables": [{"column_names": ["Reference", "Amount"], "rows": [{"Reference": "INV-1", "Amount": 100}]}],
        "line_items": [],
    }

    rows, mapping = normalize_extraction(extraction, "sap_odoo")

    assert len(rows) == 1
    assert mapping.amount_column == "Amount"


def test_normalize_extraction_falls_back_to_line_items_path():
    extraction = {
        "tables": [],
        "line_items": [{"line_id": "1", "description": "Consulting", "taxable_base": 1000.0}],
        "issue_date": "2026-06-01",
    }

    rows, mapping = normalize_extraction(extraction, "sap_odoo")

    assert len(rows) == 1
    assert rows[0].amount == Decimal("1000.0")
    assert rows[0].date == date(2026, 6, 1)
    assert mapping.amount_column == "taxable_base"
    assert mapping.date_column == "issue_date"


def test_normalize_extraction_raises_when_nothing_usable():
    extraction = {"tables": [], "line_items": []}

    with pytest.raises(NormalizationError):
        normalize_extraction(extraction, "sap_odoo")


def test_normalize_line_items_skips_items_without_taxable_base():
    line_items = [
        {"line_id": "1", "description": "A", "taxable_base": 100.0},
        {"line_id": "2", "description": "B", "taxable_base": None},
    ]

    rows, _ = normalize_line_items(line_items, None, "sap_odoo")

    assert len(rows) == 1
    assert rows[0].reference == "1"
