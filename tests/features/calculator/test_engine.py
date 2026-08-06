from decimal import Decimal

import pytest

from features.calculator.config import CalculationFlag, TaxCategory
from features.calculator.engine import (
    CalculationEngineError,
    LineItem,
    calculate_invoice,
    calculate_line,
)


def test_single_rate_invoice():
    result = calculate_invoice([
        LineItem("1", "Consulting services", Decimal("1000.00"), TaxCategory.standard),
    ])
    assert result.subtotal == Decimal("1000.00")
    assert result.total_vat == Decimal("150.00")
    assert result.grand_total == Decimal("1150.00")
    assert not result.has_flagged_lines


def test_multi_rate_invoice_uses_per_line_rounding_not_invoice_total_rounding():
    """Three lines whose individual (taxable_base * rate) each round cleanly,
    but whose SUM of bases, if VAT were computed once on the total instead
    of per line, would round to a different total_vat than summing the
    already-rounded per-line amounts. This is the exact class of mismatch
    the doc calls out as tripping up naive calculators."""
    lines = [
        LineItem("1", "Item A", Decimal("10.03"), TaxCategory.standard),   # 10.03*0.15=1.5045 -> 1.50
        LineItem("2", "Item B", Decimal("10.03"), TaxCategory.standard),   # 1.5045 -> 1.50
        LineItem("3", "Item C", Decimal("10.03"), TaxCategory.standard),   # 1.5045 -> 1.50
    ]
    result = calculate_invoice(lines)

    per_line_total_vat = result.total_vat  # 1.50 + 1.50 + 1.50 = 4.50
    naive_total_vat = (Decimal("10.03") * 3 * Decimal("0.15")).quantize(Decimal("0.01"))  # 30.09*0.15=4.5135 -> 4.51

    assert per_line_total_vat == Decimal("4.50")
    assert naive_total_vat == Decimal("4.51")
    assert per_line_total_vat != naive_total_vat, "fixture didn't actually produce a divergence"


def test_mixed_tax_categories_on_one_invoice():
    result = calculate_invoice([
        LineItem("1", "Standard item", Decimal("100.00"), TaxCategory.standard),
        LineItem("2", "Zero-rated export", Decimal("50.00"), TaxCategory.zero_rated),
        LineItem("3", "Exempt item", Decimal("25.00"), TaxCategory.exempt),
    ])
    assert result.lines[0].vat_amount == Decimal("15.00")
    assert result.lines[1].vat_amount == Decimal("0.00")
    assert result.lines[2].vat_amount == Decimal("0.00")
    assert result.subtotal == Decimal("175.00")
    assert result.total_vat == Decimal("15.00")
    assert result.grand_total == Decimal("190.00")


def test_flagged_line_still_computes_a_number_but_is_marked():
    item = LineItem(
        "1", "Imported consulting service", Decimal("1000.00"), TaxCategory.standard,
        flag=CalculationFlag.reverse_charge_import_of_services,
    )
    line_result = calculate_line(item)
    assert line_result.vat_amount == Decimal("150.00")  # still computed, never left blank
    assert line_result.flagged_for_manual_review is True
    assert "reverse charge" in line_result.flag_reason.lower()

    invoice_result = calculate_invoice([item])
    assert invoice_result.has_flagged_lines is True


def test_empty_line_items_raises():
    with pytest.raises(CalculationEngineError):
        calculate_invoice([])


def test_negative_taxable_base_computes_a_consistent_credit_note():
    """A credit note has consistently negative amounts throughout — the
    same base * rate formula must produce a consistently negative VAT,
    not reject the input. Regression test for a real gap found via live
    testing: a genuine, internally-consistent credit note used to be
    rejected outright with "not allowed", indistinguishable from an actual
    data error."""
    result = calculate_invoice([
        LineItem("1", "Returned goods", Decimal("-500.00"), TaxCategory.standard),
    ])
    assert result.subtotal == Decimal("-500.00")
    assert result.total_vat == Decimal("-75.00")
    assert result.grand_total == Decimal("-575.00")
