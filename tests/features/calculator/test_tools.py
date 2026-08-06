import base64

import pytest

from features.calculator.tools import (
    ToolInputError,
    calculate_vat,
    classify_line_items,
    generate_report,
    validate_zatca_form,
)

LINE_ITEMS = [
    {"line_id": "1", "description": "Consulting services", "taxable_base": 1000.00, "tax_category": "standard"},
    {"line_id": "2", "description": "Export shipping fee", "taxable_base": 500.00, "tax_category": "zero_rated"},
]


def test_calculate_vat_basic():
    result = calculate_vat(LINE_ITEMS)
    assert result["total_vat"] == 150.0
    assert result["grand_total"] == 1650.0


def test_calculate_vat_empty_raises():
    with pytest.raises(ToolInputError):
        calculate_vat([])


def test_calculate_vat_bad_category_raises():
    with pytest.raises(ToolInputError):
        calculate_vat([{"line_id": "1", "taxable_base": 100, "tax_category": "bogus"}])


def test_validate_zatca_form_matching_totals():
    result = validate_zatca_form(LINE_ITEMS, {"total_vat": 150.0, "grand_total": 1650.0})
    assert result["match"] is True
    assert result["mismatches"] == []


def test_validate_zatca_form_mismatched_totals():
    result = validate_zatca_form(LINE_ITEMS, {"total_vat": 100.0, "grand_total": 1600.0})
    assert result["match"] is False
    assert len(result["mismatches"]) == 2
    fields = {m["field"] for m in result["mismatches"]}
    assert fields == {"total_vat", "grand_total"}


def test_classify_line_items_valid_and_invalid():
    result = classify_line_items([
        {"line_id": "1", "tax_category": "standard", "confidence": 0.9},
        {"line_id": "2", "tax_category": "not_a_real_category", "confidence": 0.5},
    ])
    assert len(result["classifications"]) == 1
    assert result["classifications"][0]["line_id"] == "1"
    assert len(result["invalid"]) == 1
    assert result["invalid"][0]["line_id"] == "2"


def test_generate_report_data_xlsx_default():
    calculation = calculate_vat(LINE_ITEMS)
    report = generate_report(calculation)
    assert report["filename"] == "vat_report.xlsx"
    assert len(base64.b64decode(report["content_base64"])) > 0


def test_generate_report_issues_summary_always_pdf():
    calculation = calculate_vat(LINE_ITEMS)
    report = generate_report(
        calculation, format="xlsx", report_type="issues_summary",
        structural_issues=[{"severity": "error", "message": "VAT number invalid"}],
        explanation="Test explanation.",
    )
    # report_type="issues_summary" ignores the format param and always renders PDF
    assert report["filename"] == "issues_summary.pdf"
    assert report["mimetype"] == "application/pdf"
