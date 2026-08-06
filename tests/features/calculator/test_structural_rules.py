from datetime import date, timedelta

from features.calculator.config import TaxpayerType
from features.calculator.structural_rules import (
    run_structural_validation,
    validate_cross_field_consistency,
    validate_dates,
    validate_required_fields,
    validate_vat_number_format,
)

VALID_DOCUMENT = {
    "seller_id": "SELLER-1",
    "buyer_id": "BUYER-1",
    "vat_number": "300000000000003",
    "issue_date": date.today(),
    "identity_number": "1010101010",
}


def test_valid_document_has_no_issues():
    issues = run_structural_validation(VALID_DOCUMENT, TaxpayerType.company)
    assert issues == []


def test_missing_identity_field_per_taxpayer_type():
    doc = {**VALID_DOCUMENT, "identity_number": None}
    issues = validate_required_fields(doc, TaxpayerType.company)
    assert any(i.rule_id == "missing_identity_field" and "CR number" in i.message for i in issues)

    issues2 = validate_required_fields(doc, TaxpayerType.individual)
    assert any(i.rule_id == "missing_identity_field" and "national ID" in i.message for i in issues2)


def test_malformed_vat_number_wrong_length():
    issues = validate_vat_number_format("12345")
    assert len(issues) == 1
    assert issues[0].rule_id == "invalid_vat_number_format"


def test_malformed_vat_number_wrong_start_end_digit():
    issues = validate_vat_number_format("400000000000004")
    assert len(issues) == 1
    assert issues[0].rule_id == "invalid_vat_number_format"


def test_valid_vat_number_passes():
    assert validate_vat_number_format("300000000000003") == []


def test_future_issue_date():
    issues = validate_dates(date.today() + timedelta(days=1))
    assert any(i.rule_id == "future_issue_date" for i in issues)


def test_supply_date_after_issue_date_is_a_warning():
    issues = validate_dates(date.today(), supply_date=date.today() + timedelta(days=1))
    assert any(i.rule_id == "supply_date_after_issue_date" and i.severity == "warning" for i in issues)


def test_buyer_equals_seller():
    issues = validate_cross_field_consistency("SAME-ID", "SAME-ID")
    assert len(issues) == 1
    assert issues[0].rule_id == "seller_equals_buyer"


def test_missing_required_fields_reported_individually():
    issues = validate_required_fields({}, TaxpayerType.company)
    reported_fields = {i.field for i in issues}
    assert {"seller_id", "buyer_id", "vat_number", "issue_date", "identity_number"} <= reported_fields
