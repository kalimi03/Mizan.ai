"""
Mizan.ai — "ZATCA/VAT Calculator & Validator" (Feature E) config.

Companion doc: docs/mizan_calculator_comparator_handoff.pdf. Scope for this
build: the shared calculation engine (calculate_vat/validate_zatca_form),
tax-category classification, and report generation — reused as a single
engine behind two thin tools, per the doc's own framing. Feature C
(document comparator) lives in features/comparator/, its own service.
"""

import os
from decimal import Decimal
from enum import Enum

# Triple-fallback env var convention matching the rest of this repo (e.g.
# features/explainer/config.py, features/chatbot/filing_notes_qa.py).
QWEN_BRAIN_URL = (
    os.getenv("MIZAN_QWEN_BRAIN_URL")
    or os.getenv("QWEN_BRAIN_URL")
    or os.getenv("MODAL_QWEN_BRAIN_URL")
)

# Translator service's internal, unauthenticated endpoint (same pattern as
# doc-extraction — never called directly by end users, only server-to-server
# within the docker-compose/K8s network). Used by report.py's
# _maybe_translate() now that Calculator and Translator are separate
# services and can no longer share an in-process function call.
TRANSLATOR_INTERNAL_URL = (
    os.getenv("MIZAN_TRANSLATOR_INTERNAL_URL")
    or os.getenv("TRANSLATOR_INTERNAL_URL")
)

# doc-extraction service — used by /api/compliance/upload to extract real
# data from uploaded invoices, same pattern as features/comparator/config.py.
DOC_EXTRACTION_URL = (
    os.getenv("MIZAN_DOC_EXTRACTION_URL")
    or os.getenv("DOC_EXTRACTION_URL")
)


class TaxCategory(str, Enum):
    standard = "standard"        # 15%
    zero_rated = "zero_rated"    # 0%, input VAT reclaimable
    exempt = "exempt"            # 0%, input VAT NOT reclaimable


class TaxpayerType(str, Enum):
    company = "company"          # identity field: CR number
    individual = "individual"    # identity field: national ID / Iqama


class CalculationFlag(str, Enum):
    reverse_charge_import_of_services = "reverse_charge_import_of_services"
    related_party_zero_rating = "related_party_zero_rating"


VAT_RATES = {
    TaxCategory.standard: Decimal("0.15"),
    TaxCategory.zero_rated: Decimal("0.00"),
    TaxCategory.exempt: Decimal("0.00"),
}

ROUNDING_DECIMALS = 2

# ZATCA VAT registration numbers: 15 digits, starting and ending with "3".
VAT_NUMBER_LENGTH = 15

# Job 1 — a small tolerance for the printed-vs-recalculated VAT/total
# comparison (invoice_check.py). Real invoicing software commonly computes
# VAT via aggregate rounding (sum the taxable bases, apply the rate once,
# round once) rather than this app's own per-line rounding (the more
# precise ZATCA-compliant method — see engine.py's module docstring) — the
# two methods can legitimately differ by a few cents on an otherwise-
# correct invoice. Without this, that harmless rounding noise is
# indistinguishable from a genuine calculation error, which would flag a
# steady stream of false positives and bury the real ones. Found via live
# testing, not assumed upfront — 0.02 confirmed as the real boundary
# against a dedicated rounding-tolerance-boundary test invoice (SAR 0.02
# gap, expected to pass) alongside a genuine-error test invoice (SAR 0.10
# gap, expected to still flag).
MISMATCH_TOLERANCE = Decimal("0.02")

# Period VAT return preparation (job 2) — reclaimable-vs-blocked input VAT.
# Keyword-matched against a purchase row's expense-type/description text,
# only when the source data doesn't already state reclaimability itself
# (see period_return.py's _resolve_reclaimability() — a register's own
# "Reclaimable?" column, when present, is trusted directly and these lists
# are never consulted).
#
# Sourced from Article 50 of the Saudi VAT Implementing Regulations,
# "Goods and Services Deemed to be Received Outside of Economic Activity"
# (cross-referenced by name at Article 49(2)(c)/(3)(d) — "restricted from
# deduction, as prescribed in Article fifty of these Regulations"; text
# confirmed directly against the regulation, not general VAT-system
# knowledge). Article 50(1) lists six categories; five map to something a
# purchase register's expense-type text could plausibly say — the sixth,
# "any other goods/services used for a private or non-business purpose"
# (50(1)(f)), is a catch-all about actual usage, not a describable
# category, so it can't be keyword-matched and isn't attempted here. A
# narrow exception in 50(4) — goods/services bought for onward resale are
# still deductible even if otherwise listed here — also isn't detected
# from expense-type text alone; a human reviewing the workpapers' stated
# reason is the backstop for that case, consistent with this feature never
# silently overriding a source-stated "Reclaimable?" value.
#
# Tier 1 — categories 50(1)(a)-(b): entertainment/sporting/cultural
# services and catering in hotels/restaurants. Blocked outright, no
# business-use carve-out in the regulation text (unlike the vehicle
# categories below) — safe to apply automatically with no human decision.
BLOCKED_INPUT_VAT_CATEGORIES = ["entertainment", "sporting", "cultural", "catering", "hospitality", "hotel", "restaurant"]

# Tier 2 — categories 50(1)(c)-(e): purchase/lease, repair/maintenance,
# and fuel for a "Restricted Motor Vehicle" — but 50(2) defines that term
# with a genuine business-use carve-out (NOT restricted, i.e. reclaimable,
# if used exclusively for work with no private use, or if the vehicle is
# itself for resale or for a car-rental-type Economic Activity) — exactly
# the "depends on context a keyword can't determine" case this tier
# exists for. Collected across the whole batch and surfaced together for
# one human decision, never auto-decided either way.
AMBIGUOUS_INPUT_VAT_CATEGORIES = ["vehicle", "motor", "car rental", "car lease", "fuel", "petrol", "gasoline"]
