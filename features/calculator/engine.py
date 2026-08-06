"""
Mizan.ai — VAT calculation engine (Feature E, Step 4). Pure deterministic
Python, no LLM — this is the shared heart behind both calculate_vat and
validate_zatca_form.

Uses decimal.Decimal throughout, not float, for every money field —
binary-float rounding errors are exactly the kind of subtle mismatch the
validator exists to catch, so the engine itself must not introduce any.

Rounding happens PER LINE ITEM, not once at the end: each line's
taxable_base * rate is rounded to 2dp individually, and the invoice total
VAT is the sum of those already-rounded amounts — never re-derived by
summing bases first and rounding once. This is the detail that trips up
naive calculators and is the main source of "small SAR amount" mismatches
between what's printed on a real document and a naive recalculation.
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import List, Optional

from .config import VAT_RATES, CalculationFlag, TaxCategory


class CalculationEngineError(ValueError):
    """Raised for malformed input the caller should turn into a 422."""


@dataclass
class LineItem:
    line_id: str
    description: str
    taxable_base: Decimal
    tax_category: TaxCategory
    flag: Optional[CalculationFlag] = None  # reverse charge / related-party — never auto-calculated silently


@dataclass
class LineItemResult:
    line_id: str
    description: str
    taxable_base: Decimal
    tax_category: TaxCategory
    rate: Decimal
    vat_amount: Decimal              # rounded to 2dp AT THE LINE LEVEL
    flagged_for_manual_review: bool
    flag_reason: Optional[str] = None


@dataclass
class CalculationResult:
    lines: List[LineItemResult] = field(default_factory=list)
    subtotal: Decimal = Decimal("0")   # sum of line taxable bases
    total_vat: Decimal = Decimal("0")  # sum of already-rounded line VAT
    grand_total: Decimal = Decimal("0")  # subtotal + total_vat
    currency: str = "SAR"
    has_flagged_lines: bool = False


def round_currency(value: Decimal) -> Decimal:
    """The single rounding primitive every line uses — ROUND_HALF_UP to 2dp."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


_FLAG_REASONS = {
    CalculationFlag.reverse_charge_import_of_services: (
        "Reverse charge / import of services — verify manually, not auto-calculated."
    ),
    CalculationFlag.related_party_zero_rating: (
        "Related-party or tax-unit zero-rating — flag for confirmation, not assumed."
    ),
}


def calculate_line(item: LineItem) -> LineItemResult:
    # Negative taxable_base is allowed, not rejected — a credit note has
    # consistently negative amounts throughout (base, VAT, and the
    # document's own printed totals), and the same base * rate formula
    # produces a consistently negative VAT that correctly compares against
    # those negative printed totals. A blanket rejection here used to
    # reject genuine credit notes outright (found via live testing with a
    # real, internally-consistent credit note). A negative amount that's
    # actually a data error on what's meant to be a normal positive
    # invoice is still caught — just by the numeric comparison against
    # that invoice's own (positive) printed totals not matching, the same
    # transparent mechanism that catches every other calculation error,
    # rather than a separate blanket guard that couldn't tell a real
    # credit note apart from a typo.
    rate = VAT_RATES[item.tax_category]
    vat_amount = round_currency(item.taxable_base * rate)

    return LineItemResult(
        line_id=item.line_id,
        description=item.description,
        taxable_base=item.taxable_base,
        tax_category=item.tax_category,
        rate=rate,
        vat_amount=vat_amount,
        flagged_for_manual_review=item.flag is not None,
        flag_reason=_FLAG_REASONS.get(item.flag) if item.flag else None,
    )


def calculate_invoice(line_items: List[LineItem], currency: str = "SAR") -> CalculationResult:
    if not line_items:
        raise CalculationEngineError("line_items must not be empty")

    lines = [calculate_line(item) for item in line_items]

    subtotal = sum((line.taxable_base for line in lines), Decimal("0"))
    # Sum already-rounded per-line VAT — never re-derive from a summed base.
    # This is what makes multi-rate invoices (mixed standard/zero-rated/
    # exempt lines) correct: the invoice total is never itself re-rounded.
    total_vat = sum((line.vat_amount for line in lines), Decimal("0"))
    grand_total = subtotal + total_vat

    return CalculationResult(
        lines=lines,
        subtotal=subtotal,
        total_vat=total_vat,
        grand_total=grand_total,
        currency=currency,
        has_flagged_lines=any(line.flagged_for_manual_review for line in lines),
    )
