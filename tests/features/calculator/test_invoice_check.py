"""
Tests for features/calculator/invoice_check.py — job 1's automated,
no-human-in-the-loop per-invoice check. Real calculation engine is used
throughout (it's pure and fast); only the network/model-dependent edges
(doc-extraction, QwenBrain narration/classification) are mocked.
"""

from unittest.mock import MagicMock

import pytest

from features.calculator import invoice_check
from features.common.http_client import InternalServiceError

VALID_SELLER_VAT = "300000000000003"

CLEAN_EXTRACTION = {
    "seller": {"name": "Al Faisal Trading Est.", "vat_number": VALID_SELLER_VAT},
    "buyer": {"name": "Rawabi Contracting Co.", "vat_number": None},
    "issue_date": "2026-07-02",
    "line_items": [
        {"line_id": "1", "description": "Consulting services", "taxable_base": 1000.0,
         "tax_category_code": "S", "vat_amount": 150.0},
    ],
    "totals": {"tax_exclusive_amount": 1000.0, "tax_amount": 150.0, "tax_inclusive_amount": 1150.0},
}


@pytest.fixture(autouse=True)
def no_model_calls_by_default(monkeypatch):
    """QWEN_BRAIN_URL unset by default -> _narrate_flagged_invoice() returns
    None immediately, without ever calling out — the real fail-open
    behavior when no model endpoint is configured. classify() is mocked to
    fail loudly if called without a test explicitly expecting it. Tests
    that want to exercise narration turn QWEN_BRAIN_URL back on
    explicitly (see _enable_narration below)."""
    monkeypatch.setattr(invoice_check, "QWEN_BRAIN_URL", None)
    mock_agent = MagicMock()
    mock_agent.classify.side_effect = AssertionError("classification should not run for this test")
    monkeypatch.setattr(invoice_check, "_agent", mock_agent)
    return mock_agent


def _enable_narration(monkeypatch, content="A plain-language explanation."):
    """Turns narration on for a test and captures the exact prompt sent to
    the model, so tests can assert on what context it was given — not just
    that it was called."""
    captured = {}

    def fake_call_modal_json(url, payload, timeout=120):
        captured["prompt"] = payload["messages"][0]["content"]
        return {"content": content}

    monkeypatch.setattr(invoice_check, "QWEN_BRAIN_URL", "https://fake-qwenbrain.example")
    monkeypatch.setattr(invoice_check, "call_modal_json", fake_call_modal_json)
    return captured


def test_clean_invoice_is_not_flagged_and_skips_narration(monkeypatch):
    monkeypatch.setattr(invoice_check, "_extract_one", lambda filename, path: dict(CLEAN_EXTRACTION))

    result = invoice_check._check_one_invoice("inv1.xml", "/tmp/inv1.xml")

    assert result["status"] == "clean"
    assert result["structural_issues"] == []
    assert result["mismatches"] == []
    assert result["explanation"] is None


def test_invalid_vat_number_format_flags_even_when_totals_match(monkeypatch):
    extraction = {**CLEAN_EXTRACTION, "seller": {"name": "Al Faisal Trading Est.", "vat_number": "12345"}}
    monkeypatch.setattr(invoice_check, "_extract_one", lambda filename, path: extraction)
    _enable_narration(monkeypatch, "VAT number looks malformed.")

    result = invoice_check._check_one_invoice("inv2.xml", "/tmp/inv2.xml")

    assert result["status"] == "flagged"
    assert any(i["rule_id"] == "invalid_vat_number_format" for i in result["structural_issues"])
    assert result["explanation"] == "VAT number looks malformed."


def test_buyer_vat_number_absent_is_not_an_issue(monkeypatch):
    """Simplified (B2C) ZATCA tax invoices legitimately carry no buyer VAT
    number — its absence alone must never be flagged."""
    extraction = {**CLEAN_EXTRACTION, "buyer": {"name": "Rawabi Contracting Co.", "vat_number": None}}
    monkeypatch.setattr(invoice_check, "_extract_one", lambda filename, path: extraction)

    result = invoice_check._check_one_invoice("inv_b2c.xml", "/tmp/inv_b2c.xml")

    assert result["status"] == "clean"


def test_buyer_malformed_vat_number_is_flagged(monkeypatch):
    """Regression test for a real gap found via live testing: a malformed
    buyer VAT number used to sail through as "clean" — only the seller's
    format was ever checked. Now checked too, but only when a buyer VAT
    number is actually present (see the sibling test above)."""
    extraction = {**CLEAN_EXTRACTION, "buyer": {"name": "Rawabi Contracting Co.", "vat_number": "999"}}
    monkeypatch.setattr(invoice_check, "_extract_one", lambda filename, path: extraction)
    _enable_narration(monkeypatch, "Buyer VAT number looks malformed.")

    result = invoice_check._check_one_invoice("inv_bad_buyer_vat.xml", "/tmp/inv_bad_buyer_vat.xml")

    assert result["status"] == "flagged"
    assert any(i["rule_id"] == "invalid_buyer_vat_number_format" for i in result["structural_issues"])


def test_mismatch_at_tolerance_boundary_is_not_flagged(monkeypatch):
    """Regression test for a real gap found via live testing (against a
    dedicated rounding_at_tolerance_boundary.xml fixture): a SAR 0.02 gap
    purely from rounding methodology (per-line vs. aggregate rounding —
    both legitimate, see MISMATCH_TOLERANCE) used to be flagged exactly
    like a genuine calculation error."""
    extraction = {
        **CLEAN_EXTRACTION,
        "totals": {"tax_exclusive_amount": 1000.0, "tax_amount": 150.02, "tax_inclusive_amount": 1150.02},
    }
    monkeypatch.setattr(invoice_check, "_extract_one", lambda filename, path: extraction)

    result = invoice_check._check_one_invoice("inv_rounding_edge.xml", "/tmp/inv_rounding_edge.xml")

    assert result["status"] == "clean"
    assert result["mismatches"]  # still recorded, transparently, just not flagging-worthy
    assert all(m["within_tolerance"] for m in result["mismatches"])


def test_mismatch_beyond_tolerance_is_flagged(monkeypatch):
    """Mirrors the real rounding_beyond_tolerance_real_error.xml fixture
    (SAR 0.10 gap) — large enough that it must still flag."""
    extraction = {
        **CLEAN_EXTRACTION,
        "totals": {"tax_exclusive_amount": 1000.0, "tax_amount": 150.10, "tax_inclusive_amount": 1150.10},
    }
    monkeypatch.setattr(invoice_check, "_extract_one", lambda filename, path: extraction)
    _enable_narration(monkeypatch, "Off by ten cents.")

    result = invoice_check._check_one_invoice("inv_real_mismatch.xml", "/tmp/inv_real_mismatch.xml")

    assert result["status"] == "flagged"
    assert not any(m["within_tolerance"] for m in result["mismatches"])


def test_custom_tolerance_overrides_the_default(monkeypatch):
    """A user-supplied tolerance (run_invoice_check's optional parameter,
    threaded from the API's optional form field) must actually change the
    flagging decision — a SAR 0.10 gap that would flag under the default
    (0.02) should pass cleanly under an explicitly wider tolerance."""
    from decimal import Decimal

    extraction = {
        **CLEAN_EXTRACTION,
        "totals": {"tax_exclusive_amount": 1000.0, "tax_amount": 150.10, "tax_inclusive_amount": 1150.10},
    }
    monkeypatch.setattr(invoice_check, "_extract_one", lambda filename, path: extraction)

    result = invoice_check.run_invoice_check([("inv_custom_tolerance.xml", b"<Invoice/>")], tolerance=Decimal("0.20"))

    assert result["invoices"][0]["status"] == "clean"
    assert all(m["within_tolerance"] for m in result["invoices"][0]["mismatches"])


def test_both_seller_and_buyer_malformed_are_both_reported(monkeypatch):
    """Regression test for a real finding from live testing: when both the
    seller's AND the buyer's VAT numbers are malformed at once, both
    issues must be present in the data (they always were) AND both must
    survive into the summary report's combined reason text (they didn't —
    the old first-issue-only summary made the buyer's issue look dropped
    even though the underlying structural_issues list already had it)."""
    from features.calculator.compliance_reports import _flagged_reason_summary

    extraction = {
        **CLEAN_EXTRACTION,
        "seller": {"name": "Al Faisal Trading Est.", "vat_number": "12345"},
        "buyer": {"name": "Rawabi Contracting Co.", "vat_number": "300000000000009"},
    }
    monkeypatch.setattr(invoice_check, "_extract_one", lambda filename, path: extraction)
    _enable_narration(monkeypatch, "Both VAT numbers look malformed.")

    result = invoice_check._check_one_invoice("inv_both_malformed.xml", "/tmp/inv_both_malformed.xml")

    rule_ids = {i["rule_id"] for i in result["structural_issues"]}
    assert "invalid_vat_number_format" in rule_ids  # seller
    assert "invalid_buyer_vat_number_format" in rule_ids  # buyer

    summary = _flagged_reason_summary(result)
    assert "15 digits" in summary  # seller's reason
    assert "start and end" in summary  # buyer's reason


def test_numeric_mismatch_flags_and_requests_narration(monkeypatch):
    extraction = {
        **CLEAN_EXTRACTION,
        "totals": {"tax_exclusive_amount": 1000.0, "tax_amount": 100.0, "tax_inclusive_amount": 1100.0},
    }
    monkeypatch.setattr(invoice_check, "_extract_one", lambda filename, path: extraction)
    _enable_narration(monkeypatch, "Recalculated VAT is 150, not 100.")

    result = invoice_check._check_one_invoice("inv3.xml", "/tmp/inv3.xml")

    assert result["status"] == "flagged"
    assert len(result["mismatches"]) >= 1
    assert result["explanation"] == "Recalculated VAT is 150, not 100."


def test_narration_prompt_includes_every_finding_and_forbids_saying_correct(monkeypatch):
    """Regression test for a real bug found via live testing: narration
    used to be generated from only the numeric-mismatch check, with zero
    awareness of structural issues computed elsewhere — so an invoice
    flagged purely for a malformed VAT number got a narration saying
    "no mismatches found... correct", which reads as contradicting the
    structural issue sitting right next to it. The fix feeds the model the
    complete finding list in one prompt and explicitly forbids it from
    calling anything correct/compliant while findings are listed."""
    extraction = {**CLEAN_EXTRACTION, "seller": {"name": "Al Faisal Trading Est.", "vat_number": "12345"}}
    monkeypatch.setattr(invoice_check, "_extract_one", lambda filename, path: extraction)
    captured = _enable_narration(monkeypatch)

    result = invoice_check._check_one_invoice("inv_bad_vat.xml", "/tmp/inv_bad_vat.xml")

    assert result["status"] == "flagged"
    assert "VAT number must be 15 digits" in captured["prompt"]
    assert "correct" in captured["prompt"].lower()  # the "do not say it's correct" instruction is present


def test_no_line_items_flags_as_unreadable_and_skips_narration_when_unconfigured(monkeypatch):
    extraction = {
        "seller": {"name": None, "vat_number": None}, "buyer": {"name": None, "vat_number": None},
        "issue_date": None, "line_items": [], "totals": {},
    }
    monkeypatch.setattr(invoice_check, "_extract_one", lambda filename, path: extraction)

    result = invoice_check._check_one_invoice("not_an_invoice.xlsx", "/tmp/not_an_invoice.xlsx")

    assert result["status"] == "flagged"
    assert any(i["rule_id"] == "not_readable_as_invoice" for i in result["structural_issues"])
    assert result["explanation"] is None  # QWEN_BRAIN_URL unset in this test -> fails open, no call attempted


def test_extraction_failure_is_flagged_not_raised(monkeypatch):
    def _raise(filename, path):
        raise InternalServiceError("doc-extraction rejected the file")

    monkeypatch.setattr(invoice_check, "_extract_one", _raise)

    result = invoice_check._check_one_invoice("broken.pdf", "/tmp/broken.pdf")

    assert result["status"] == "flagged"
    assert result["structural_issues"][0]["rule_id"] == "extraction_failed"


def test_unrecognized_tax_category_code_falls_back_to_classification(monkeypatch, no_model_calls_by_default):
    extraction = {
        **CLEAN_EXTRACTION,
        "line_items": [
            {"line_id": "1", "description": "Mystery service", "taxable_base": 1000.0,
             "tax_category_code": None, "vat_amount": 150.0},
        ],
    }
    monkeypatch.setattr(invoice_check, "_extract_one", lambda filename, path: extraction)
    no_model_calls_by_default.classify.side_effect = None
    no_model_calls_by_default.classify.return_value = {
        "classifications": [{"line_id": "1", "tax_category": "standard", "confidence": 0.8}],
    }

    result = invoice_check._check_one_invoice("inv4.xml", "/tmp/inv4.xml")

    no_model_calls_by_default.classify.assert_called_once()
    assert result["status"] == "clean"  # classification resolved it to "standard", matches printed totals


def test_run_invoice_check_aggregates_clean_and_flagged_counts(monkeypatch):
    def fake_extract_one(filename, path):
        if filename == "clean.xml":
            return dict(CLEAN_EXTRACTION)
        return {
            "seller": {"name": None, "vat_number": None}, "buyer": {"name": None, "vat_number": None},
            "issue_date": None, "line_items": [], "totals": {},
        }

    monkeypatch.setattr(invoice_check, "_extract_one", fake_extract_one)

    result = invoice_check.run_invoice_check([
        ("clean.xml", b"<Invoice/>"),
        ("broken.xml", b"<Invoice/>"),
    ])

    assert result["total"] == 2
    assert result["clean"] == 1
    assert result["flagged"] == 1
    assert {inv["filename"] for inv in result["invoices"]} == {"clean.xml", "broken.xml"}
