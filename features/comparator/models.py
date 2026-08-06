"""
Mizan.ai — Comparator (Feature C) data shapes. Pure dataclasses, no
framework/Pydantic dependency here — services/comparator/main.py maps these
to/from its own Pydantic request/response models at the API boundary, same
separation features/calculator/engine.py keeps from app/main.py's models.
"""

from dataclasses import dataclass, field
from datetime import date as date_type
from decimal import Decimal
from typing import List, Literal, Optional

Source = Literal["sap_odoo", "second_doc"]
MatchType = Literal["reference", "amount_unique", "amount_date_fuzzy"]
ReviewKind = Literal["fuzzy_match", "unmatched", "ambiguous", "amount_mismatch", "group_match", "sum_match"]
ReviewDecision = Literal["confirmed", "dismissed"]


@dataclass
class ReconciliationRow:
    row_id: str
    source: Source
    reference: Optional[str] = None
    date: Optional[date_type] = None
    amount: Optional[Decimal] = None
    description: Optional[str] = None
    raw: dict = field(default_factory=dict)  # original row values, for display/debugging


@dataclass
class ColumnMapping:
    """Which raw column normalize.py guessed maps to which semantic field —
    a best-effort header-synonym guess (see normalize.py's module
    docstring), not a validated spec. Returned alongside the normalized
    rows so a review screen can show "we mapped 'Debit' -> amount, is that
    right?" rather than silently trusting the guess."""
    reference_column: Optional[str] = None
    date_column: Optional[str] = None
    amount_column: Optional[str] = None
    debit_column: Optional[str] = None
    credit_column: Optional[str] = None
    description_column: Optional[str] = None


@dataclass
class MatchedPair:
    row_a: ReconciliationRow  # sap_odoo side
    row_b: ReconciliationRow  # second_doc side
    match_type: MatchType


@dataclass
class ReviewItem:
    """A leftover item surfaced to the human reviewer — either a fuzzy
    (amount+date-proximity) candidate pair, a genuinely unmatched row, an
    ambiguous row with multiple fuzzy candidates, an "amount_mismatch"
    (reference matched on both sides, but the amounts don't agree — the
    classic reconciliation exception: partial payment, pricing correction,
    duplicate, data entry error), a "group_match" (2-4 leftover items on
    one side summing to a single leftover item on the other, found via a
    real connecting signal — e.g. a vendor billing several of our purchase
    orders as one consolidated invoice; see matching.py's Pass 4), or a
    "sum_match" — the same "several items sum to one" shape as group_match,
    but found by matching.py's Pass 5 (manually triggered, post-review-only)
    with NO connecting signal at all, just amount + a wide date ceiling —
    weaker evidence, kept visually distinct for that reason. Never a clean,
    amount-verified Pass-1/2 match — those never reach this stage."""
    item_id: str
    kind: ReviewKind
    row_a: Optional[ReconciliationRow] = None  # sap_odoo side, when applicable
    row_b: Optional[ReconciliationRow] = None  # second_doc side, for a single fuzzy candidate
    candidates: List[ReconciliationRow] = field(default_factory=list)  # for kind="ambiguous"
    # For kind="group_match": the several-item side. Whichever of row_a/
    # row_b is set is the single item being summed against; group holds
    # the 2-4 rows (from the OTHER side) whose amounts sum to it.
    group: List[ReconciliationRow] = field(default_factory=list)
    # For kind="amount_mismatch": True when the gap between row_a.amount
    # and row_b.amount is close to Saudi VAT's 15% — a common real cause
    # (one side recorded VAT-inclusive, the other VAT-exclusive) rather
    # than a genuine data error. Diagnostic only, set by matching.py's
    # _is_vat_gap(); never changes whether the pair counts as a mismatch.
    possible_vat_gap: bool = False
    # Filled in by the /confirm step, not by matching.py:
    decision: Optional[ReviewDecision] = None
    explanation: Optional[str] = None
    selected_candidate_id: Optional[str] = None  # for kind="ambiguous", which candidate was picked


@dataclass
class ReconciliationPreview:
    """What POST /reconcile returns — preview only, nothing committed."""
    matched: List[MatchedPair] = field(default_factory=list)
    needs_review: List[ReviewItem] = field(default_factory=list)


@dataclass
class ReconciliationResult:
    """What POST /confirm returns — the finalized, HITL-confirmed result
    that POST /report renders."""
    matched: List[MatchedPair] = field(default_factory=list)
    reviewed: List[ReviewItem] = field(default_factory=list)  # needs_review items, now decided
    narration: Optional[str] = None  # optional QwenBrain plain-language summary
