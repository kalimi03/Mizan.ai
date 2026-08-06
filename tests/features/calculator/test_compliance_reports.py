"""
Tests for features/calculator/compliance_reports.py's summary-reason
logic — specifically the decision of what a clean invoice's summary row
says when it has a within-tolerance rounding difference: soft-labeled,
not fully silent (see _summary_reason's docstring for why).
"""

from features.calculator.compliance_reports import _summary_reason


def test_flagged_invoice_gets_its_full_reason():
    invoice = {
        "status": "flagged",
        "structural_issues": [{"severity": "error", "message": "VAT number must be 15 digits."}],
        "mismatches": [],
    }
    assert _summary_reason(invoice, "en") == "VAT number must be 15 digits."


def test_clean_invoice_with_no_mismatches_gets_no_note():
    invoice = {"status": "clean", "structural_issues": [], "mismatches": []}
    assert _summary_reason(invoice, "en") == ""


def test_clean_invoice_with_within_tolerance_mismatch_gets_soft_label():
    """The actual behavior change: a clean invoice isn't fully silent
    anymore when it has a within-tolerance difference — it gets a soft
    "rounding difference only" note instead of nothing at all, so the
    fact that a real (if harmless) difference was found and deliberately
    excused stays visible rather than looking identical to "nothing was
    ever different."."""
    invoice = {
        "status": "clean",
        "structural_issues": [],
        "mismatches": [
            {"field": "total_vat", "document_value": 150.02, "recalculated_value": 150.0, "within_tolerance": True},
            {"field": "grand_total", "document_value": 1150.02, "recalculated_value": 1150.0, "within_tolerance": True},
        ],
    }
    reason = _summary_reason(invoice, "en")
    assert "Rounding difference only" in reason
    assert "total_vat" in reason and "grand_total" in reason
