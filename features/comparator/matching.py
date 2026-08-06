"""
Mizan.ai — Comparator (Feature C) matching engine (handoff doc Steps 2-3).
Pure deterministic Python, no LLM — matching is a mechanical process here,
never a judgment call (see features/comparator/config.py's module
docstring / the plan's reasoning for why this feature has no MCP tools).

Four passes, each stricter (or narrower in scope) than the last:
  1. Exact reference match, unique on both sides -> clean, no review. A
     reference match whose amounts disagree beyond tolerance becomes an
     "amount_mismatch" review item instead, tagged with a possible_vat_gap
     hint (see _is_vat_gap) when the gap is close to Saudi VAT's 15% —
     diagnostic only, never changes the match/mismatch decision itself.
  2. Exact amount match, unique on both sides (for whatever pass 1 left
     unmatched) -> also clean, no review.
  3. Amount-within-tolerance + date-within-proximity-window, for whatever
     passes 1-2 left unmatched -> surfaced for human review (fuzzy_match if
     exactly one candidate, ambiguous if more than one, unmatched if none).
  4. Group/consolidation match: 2-4 items still "unmatched" on one side
     whose amounts sum to a single still-"unmatched" item on the other
     (e.g. a vendor billing several of our purchase orders as one
     consolidated invoice) -> surfaced for review as "group_match". Only
     ever considers items that already share a real connecting signal with
     the target (date proximity or a meaningful shared description word,
     or a sequential reference number within the candidate cluster) —
     deliberately NOT a blind search over every leftover item's amount,
     since with enough leftovers some random subset will eventually sum to
     a target by pure chance, and presenting a coincidence as a likely
     match would be worse than leaving the items unmatched. See
     _group_candidate_pool / _find_group_matches.

Only passes 1-2 produce MatchedPair (the doc's "clean matches... no review
shown for these"). Passes 3-4 always produce a ReviewItem, even when
exactly one candidate/group is found — inherently less certain than an
exact reference/amount match, so a human confirms it before it becomes
final (see services/comparator/main.py's POST /confirm).

date_proximity_days/amount_tolerance are accepted as parameters, not read
as fixed globals — config.py's values are just the defaults. These are
"reasonable judgment, not a validated spec" per the handoff doc's own
framing, so services/comparator/main.py's POST /reconcile exposes them as
editable request fields (and echoes back whatever was actually used) for
a future review screen to surface and let the user adjust, rather than
locking them in as an invisible constant.
"""

import itertools
import re
from collections import defaultdict
from datetime import date as date_type
from decimal import Decimal
from typing import Dict, List, Optional, Set, Tuple

from .config import (
    AMOUNT_TOLERANCE, DATE_PROXIMITY_DAYS, GROUP_MATCH_MAX_SIZE, GROUP_MATCH_MIN_SIZE,
    SUM_MATCH_MAX_DATE_SPREAD_DAYS, VAT_GAP_RATIO, VAT_GAP_TOLERANCE,
)
from .models import MatchedPair, ReconciliationPreview, ReconciliationRow, ReviewItem


def _is_vat_gap(a: Decimal, b: Decimal) -> bool:
    """True if two amounts differ by approximately the Saudi VAT rate
    (15%) — a common real cause of an otherwise-unexplained mismatch: one
    side recorded a VAT-inclusive figure, the other VAT-exclusive, for the
    same underlying transaction. Diagnostic only — called AFTER
    amount_tolerance has already decided the pair counts as a mismatch;
    this never changes that decision, it only relabels an already-flagged
    mismatch with a more specific, actionable hint (see
    _match_by_reference's amount_mismatch branch, the only place this is
    used)."""
    smaller, larger = sorted([a, b])
    if smaller <= 0:
        return False
    gap_ratio = (larger - smaller) / smaller
    return abs(gap_ratio - VAT_GAP_RATIO) <= VAT_GAP_TOLERANCE


def _normalize_reference(reference: Optional[str]) -> Optional[str]:
    if not reference:
        return None
    normalized = "".join(ch for ch in reference.strip().lower() if ch.isalnum())
    return normalized or None


def _quantize(amount: Decimal, amount_tolerance: Decimal) -> Decimal:
    """Rounds to amount_tolerance's precision for exact/grouped matching —
    two amounts within the tolerance band should group together."""
    return (amount / amount_tolerance).to_integral_value() * amount_tolerance if amount_tolerance else amount


def _match_by_reference(
    rows_a: List[ReconciliationRow], rows_b: List[ReconciliationRow], amount_tolerance: Decimal,
) -> Tuple[List[MatchedPair], List[ReviewItem], List[ReconciliationRow], List[ReconciliationRow]]:
    """Reference match, unique on both sides. A shared reference is
    necessary but not sufficient for a clean match — the whole point of
    reconciliation is catching cases where the SAME reference shows a
    DIFFERENT amount on each side (partial payment, pricing correction,
    duplicate, data entry error). So a reference-matched pair only becomes
    a clean MatchedPair if the amounts also agree within tolerance;
    otherwise it's surfaced as an "amount_mismatch" review item instead of
    being silently folded into the matched bucket. Either way the pair is
    claimed (removed from both leftover pools) — it's not sent on to the
    amount/fuzzy passes below, since its reference identity is already
    established."""
    groups_a: Dict[object, List[ReconciliationRow]] = defaultdict(list)
    groups_b: Dict[object, List[ReconciliationRow]] = defaultdict(list)
    for row in rows_a:
        key = _normalize_reference(row.reference)
        if key is not None:
            groups_a[key].append(row)
    for row in rows_b:
        key = _normalize_reference(row.reference)
        if key is not None:
            groups_b[key].append(row)

    matched: List[MatchedPair] = []
    mismatches: List[ReviewItem] = []
    matched_ids_a = set()
    matched_ids_b = set()
    counter = 0
    for key, a_rows in groups_a.items():
        b_rows = groups_b.get(key)
        if not (b_rows and len(a_rows) == 1 and len(b_rows) == 1):
            continue
        row_a, row_b = a_rows[0], b_rows[0]
        matched_ids_a.add(row_a.row_id)
        matched_ids_b.add(row_b.row_id)
        if abs(row_a.amount - row_b.amount) <= amount_tolerance:
            matched.append(MatchedPair(row_a=row_a, row_b=row_b, match_type="reference"))
        else:
            counter += 1
            mismatches.append(ReviewItem(
                item_id=f"review_amount_mismatch_{counter}", kind="amount_mismatch", row_a=row_a, row_b=row_b,
                possible_vat_gap=_is_vat_gap(row_a.amount, row_b.amount),
            ))

    leftover_a = [r for r in rows_a if r.row_id not in matched_ids_a]
    leftover_b = [r for r in rows_b if r.row_id not in matched_ids_b]
    return matched, mismatches, leftover_a, leftover_b


def _match_by_key(rows_a: List[ReconciliationRow], rows_b: List[ReconciliationRow], key_fn, match_type: str) -> Tuple[List[MatchedPair], List[ReconciliationRow], List[ReconciliationRow]]:
    """Groups both sides by key_fn, and for any key value that maps to
    exactly one row on each side, emits a clean MatchedPair. Returns
    (matched_pairs, leftover_a, leftover_b)."""
    groups_a: Dict[object, List[ReconciliationRow]] = defaultdict(list)
    groups_b: Dict[object, List[ReconciliationRow]] = defaultdict(list)
    for row in rows_a:
        key = key_fn(row)
        if key is not None:
            groups_a[key].append(row)
    for row in rows_b:
        key = key_fn(row)
        if key is not None:
            groups_b[key].append(row)

    matched_ids_a = set()
    matched_ids_b = set()
    pairs: List[MatchedPair] = []
    for key, a_rows in groups_a.items():
        b_rows = groups_b.get(key)
        if b_rows and len(a_rows) == 1 and len(b_rows) == 1:
            pairs.append(MatchedPair(row_a=a_rows[0], row_b=b_rows[0], match_type=match_type))
            matched_ids_a.add(a_rows[0].row_id)
            matched_ids_b.add(b_rows[0].row_id)

    leftover_a = [r for r in rows_a if r.row_id not in matched_ids_a]
    leftover_b = [r for r in rows_b if r.row_id not in matched_ids_b]
    return pairs, leftover_a, leftover_b


def _fuzzy_candidates(
    row_a: ReconciliationRow, pool_b: List[ReconciliationRow],
    amount_tolerance: Decimal, date_proximity_days: int,
) -> List[ReconciliationRow]:
    candidates = []
    for row_b in pool_b:
        if row_a.amount is None or row_b.amount is None:
            continue
        if abs(row_a.amount - row_b.amount) > amount_tolerance:
            continue
        if row_a.date is None or row_b.date is None:
            continue  # can't apply date-proximity without both dates
        if abs((row_a.date - row_b.date).days) > date_proximity_days:
            continue
        candidates.append(row_b)
    return candidates


# --- Pass 4: group/consolidation matching ------------------------------

# Generic transaction vocabulary excluded when comparing descriptions —
# without this, two totally unrelated purchases would look "related" just
# because they both say "Purchase" or "Invoice". What's left after
# filtering is the actual subject matter (e.g. "roofing", "sheets"),
# which is the real signal.
_DESCRIPTION_STOPWORDS = {
    "the", "and", "for", "from", "with", "this", "that", "order", "invoice",
    "payment", "purchase", "receipt", "transfer", "made", "received", "note",
    "batch", "phase", "consolidated", "delivery", "service", "services",
}


def _description_tokens(text: Optional[str]) -> Set[str]:
    if not text:
        return set()
    words = re.findall(r"[a-zA-Z]{4,}", text.lower())
    return {w for w in words if w not in _DESCRIPTION_STOPWORDS}


def _descriptions_related(a: Optional[str], b: Optional[str]) -> bool:
    return bool(_description_tokens(a) & _description_tokens(b))


_REFERENCE_SPLIT_RE = re.compile(r"^([A-Za-z]*-?)(\d+)")


def _reference_parts(ref: Optional[str]) -> Optional[Tuple[str, int]]:
    if not ref:
        return None
    match = _REFERENCE_SPLIT_RE.match(ref.strip())
    if not match:
        return None
    return match.group(1).upper(), int(match.group(2))


def _references_sequential(a: Optional[str], b: Optional[str], max_gap: int = 20) -> bool:
    parts_a, parts_b = _reference_parts(a), _reference_parts(b)
    if not parts_a or not parts_b:
        return False
    return parts_a[0] == parts_b[0] and abs(parts_a[1] - parts_b[1]) <= max_gap


def _connected_to_target(candidate: ReconciliationRow, target: ReconciliationRow, date_proximity_days: int) -> bool:
    """A candidate is only eligible for a summing group against `target`
    if it already looks plausibly related on grounds OTHER than the sum —
    date proximity, or a meaningful shared description word. This is the
    gate that keeps Pass 4 from blindly trying combinations of unrelated
    leftover rows until something happens to add up."""
    if candidate.date and target.date and abs((candidate.date - target.date).days) <= date_proximity_days:
        return True
    return _descriptions_related(candidate.description, target.description)


def _group_candidate_pool(
    target: ReconciliationRow, pool: List[ReconciliationRow], date_proximity_days: int,
) -> List[ReconciliationRow]:
    """Rows eligible to be considered as part of a summing group for
    `target`: either individually connected to the target (date proximity
    or description overlap), or — when a row shares neither of those with
    the target itself — part of a sequential-reference cluster with
    another row already in the pool. The second path covers e.g. two
    purchase orders numbered one apart, even when the target's own
    reference uses a completely different numbering scheme, which is the
    normal case between a company's PO numbers and a vendor's own invoice
    numbers."""
    directly_connected = [r for r in pool if _connected_to_target(r, target, date_proximity_days)]
    directly_connected_ids = {r.row_id for r in directly_connected}
    sequential_extra = [
        r for r in pool
        if r.row_id not in directly_connected_ids
        and any(_references_sequential(r.reference, other.reference) for other in pool if other.row_id != r.row_id)
    ]
    return directly_connected + sequential_extra


def _find_group_matches(
    target: ReconciliationRow, pool: List[ReconciliationRow],
    amount_tolerance: Decimal, date_proximity_days: int,
) -> List[List[ReconciliationRow]]:
    """Every distinct small group (GROUP_MATCH_MIN_SIZE..GROUP_MATCH_MAX_SIZE
    rows) from the signal-connected candidate pool whose amounts sum to
    within amount_tolerance of target.amount. Searches ONLY within the
    already-filtered, plausibly-related pool (see _group_candidate_pool),
    never the full leftover set. Returns 0, 1, or several groups — the
    caller only acts when exactly one is found; more than one equally-valid
    grouping is itself a reason not to auto-resolve (see reconcile())."""
    if target.amount is None:
        return []
    candidates = _group_candidate_pool(target, pool, date_proximity_days)
    if len(candidates) < GROUP_MATCH_MIN_SIZE:
        return []
    found: List[List[ReconciliationRow]] = []
    max_size = min(GROUP_MATCH_MAX_SIZE, len(candidates))
    for size in range(GROUP_MATCH_MIN_SIZE, max_size + 1):
        for combo in itertools.combinations(candidates, size):
            total = sum((r.amount for r in combo if r.amount is not None), Decimal("0"))
            if abs(total - target.amount) <= amount_tolerance:
                found.append(list(combo))
    return found


def reconcile(
    rows_a: List[ReconciliationRow], rows_b: List[ReconciliationRow],
    date_proximity_days: int = DATE_PROXIMITY_DAYS,
    amount_tolerance: Decimal = AMOUNT_TOLERANCE,
) -> ReconciliationPreview:
    """rows_a = sap_odoo side, rows_b = second_doc side. date_proximity_days/
    amount_tolerance default to config.py's values but can be overridden
    per call — see this module's docstring for why."""
    # Pass 1 — exact reference match, unique on both sides (amount-verified;
    # see _match_by_reference — a reference match with disagreeing amounts
    # becomes an "amount_mismatch" review item, not a clean match).
    pairs_ref, mismatches_ref, leftover_a, leftover_b = _match_by_reference(rows_a, rows_b, amount_tolerance)

    # Pass 2 — exact amount match, unique on both sides, for what's left.
    pairs_amount, leftover_a, leftover_b = _match_by_key(
        leftover_a, leftover_b,
        lambda r: _quantize(r.amount, amount_tolerance) if r.amount is not None else None,
        "amount_unique",
    )

    matched = pairs_ref + pairs_amount
    needs_review: List[ReviewItem] = list(mismatches_ref)
    review_counter = 0

    # Pass 3 — fuzzy amount+date-proximity match, always surfaced for review.
    #
    # Candidates are computed for every row_a against the SAME full,
    # unmutated leftover_b — not a pool that gets depleted as earlier
    # row_a's greedily claim rows. An earlier version did use a shrinking
    # pool, which meant whichever row_a happened to come first in the
    # uploaded file's row order would silently claim a shared candidate,
    # and an equally-valid LATER row_a for that same candidate saw an
    # already-emptied pool and was marked "unmatched" with no record it had
    # ever been in the running (found via a real test: two payments of the
    # same amount to the same vendor, one bank receipt — the file-order-
    # first payment silently "won" it). Fix: a row_b claimed by more than
    # one row_a is sent to review as "ambiguous" for ALL of them, same as
    # a row_a with multiple row_b candidates already was — "genuinely
    # uncertain, let a human decide" instead of an unexplained pick,
    # consistent with every other judgment call in this module.
    candidates_by_a: Dict[str, List[ReconciliationRow]] = {
        row_a.row_id: _fuzzy_candidates(row_a, leftover_b, amount_tolerance, date_proximity_days)
        for row_a in leftover_a
    }
    a_count_by_b: Dict[str, int] = defaultdict(int)
    for candidates in candidates_by_a.values():
        for candidate in candidates:
            a_count_by_b[candidate.row_id] += 1

    referenced_b_ids = set()
    for row_a in leftover_a:
        candidates = candidates_by_a[row_a.row_id]
        review_counter += 1
        item_id = f"review_{review_counter}"
        if not candidates:
            needs_review.append(ReviewItem(item_id=item_id, kind="unmatched", row_a=row_a))
            continue

        referenced_b_ids.update(c.row_id for c in candidates)
        if len(candidates) == 1 and a_count_by_b[candidates[0].row_id] == 1:
            # This row_a's one and only candidate, and no other row_a is
            # also eyeing it -- a genuinely uncontested pairing.
            needs_review.append(ReviewItem(item_id=item_id, kind="fuzzy_match", row_a=row_a, row_b=candidates[0]))
        else:
            # Either row_a itself has more than one plausible row_b, or its
            # one candidate is also claimed by a different row_a -- either
            # way there's more than one equally-valid reading, so it's
            # surfaced as ambiguous rather than picked for the user.
            needs_review.append(ReviewItem(item_id=item_id, kind="ambiguous", row_a=row_a, candidates=candidates))

    # Anything never claimed as a fuzzy candidate and never listed among an
    # ambiguous row's candidates is genuinely unmatched.
    for row_b in leftover_b:
        if row_b.row_id in referenced_b_ids:
            continue
        review_counter += 1
        needs_review.append(ReviewItem(item_id=f"review_{review_counter}", kind="unmatched", row_b=row_b))

    # Pass 4 — group/consolidation match. Only items Pass 3 left as plain
    # "unmatched" are eligible; anything already explained by a
    # fuzzy_match/ambiguous/amount_mismatch keeps that explanation rather
    # than being pulled into a group instead.
    unmatched_items = [i for i in needs_review if i.kind == "unmatched"]
    other_items = [i for i in needs_review if i.kind != "unmatched"]
    pool_a = [i.row_a for i in unmatched_items if i.row_a is not None]
    pool_b = [i.row_b for i in unmatched_items if i.row_b is not None]

    group_items: List[ReviewItem] = []
    consumed_a_ids: Set[str] = set()
    consumed_b_ids: Set[str] = set()

    # Direction 1: several of ours -> one of theirs. Processed target-by-
    # target, claiming rows as it goes — same file-order sensitivity Pass 3
    # used to have is theoretically possible here too if two DIFFERENT
    # targets could each be explained by an overlapping set of candidate
    # rows (not just one target with multiple groupings, which IS guarded
    # against below via "skip if len(groups) != 1"). That specific
    # cross-target case isn't detected — a real, documented scope limit,
    # not an oversight; flagged rather than silently left unmentioned.
    for target_b in pool_b:
        available_a = [r for r in pool_a if r.row_id not in consumed_a_ids]
        groups = _find_group_matches(target_b, available_a, amount_tolerance, date_proximity_days)
        if len(groups) == 1:
            review_counter += 1
            group_items.append(ReviewItem(
                item_id=f"review_{review_counter}", kind="group_match", row_b=target_b, group=groups[0],
            ))
            consumed_a_ids.update(r.row_id for r in groups[0])
            consumed_b_ids.add(target_b.row_id)

    # Direction 2: one of ours -> several of theirs.
    for target_a in pool_a:
        if target_a.row_id in consumed_a_ids:
            continue
        available_b = [r for r in pool_b if r.row_id not in consumed_b_ids]
        groups = _find_group_matches(target_a, available_b, amount_tolerance, date_proximity_days)
        if len(groups) == 1:
            review_counter += 1
            group_items.append(ReviewItem(
                item_id=f"review_{review_counter}", kind="group_match", row_a=target_a, group=groups[0],
            ))
            consumed_a_ids.add(target_a.row_id)
            consumed_b_ids.update(r.row_id for r in groups[0])

    remaining_unmatched = [
        i for i in unmatched_items
        if not (
            (i.row_a is not None and i.row_a.row_id in consumed_a_ids)
            or (i.row_b is not None and i.row_b.row_id in consumed_b_ids)
        )
    ]
    needs_review = other_items + remaining_unmatched + group_items

    return ReconciliationPreview(matched=matched, needs_review=needs_review)


# --- Pass 5: signal-free sum-combination search over the leftover pool -----
#
# Manually triggered ("Check for more possible groupings" in the frontend,
# POST /api/comparator/reconcile-leftovers) — run ONLY after a full human
# review pass on Passes 1-4's own findings, never automatically and never
# in parallel with that review. Unlike Pass 4, this does NOT require any
# connecting signal between a candidate and its target — that's the whole
# point: it exists for real installment/partial-payment cases that share
# none of Pass 4's signals (verified: generic "Payment 1/2/3" descriptions
# + non-sequential bank transaction IDs + dates spread over weeks are
# invisible to Pass 4, but are exactly the realistic worst case for many
# real bank statements). Bounded instead by only ever running on the
# already-human-thinned leftover pool, the same GROUP_MATCH_MIN/MAX_SIZE
# cap Pass 4 uses, and a wide-but-finite date ceiling. Same "more than one
# valid combination -> propose neither" safeguard as Pass 4 — matters even
# more here, since removing the signal gate raises the odds of a
# coincidental multi-way tie.


def build_leftover_pool(
    matched: List[MatchedPair], reviewed: List[ReviewItem],
) -> Tuple[List[ReconciliationRow], List[ReconciliationRow]]:
    """Reconstructs the pool Pass 5 is allowed to search from an already
    confirmed (human-reviewed) result. A row is SETTLED (excluded) if it's
    part of a clean Pass 1/2 match, or the specific side/candidate a
    CONFIRMED review item actually used. Everything else a review item
    touched — a dismissed pairing's rows (the pairing was wrong, not the
    item itself; it may still belong to a different combination), a
    still-unresolved item's rows, or a confirmed ambiguous item's UNUSED
    candidates — goes back into the leftover pool. If the same row ends up
    in both categories (e.g. one card in a contested group gets confirmed
    against a candidate while a sibling card's dismissed item still lists
    that same candidate), settled always wins — that row is genuinely
    used, regardless of what a different, wrong item says about it."""
    settled_a: Dict[str, ReconciliationRow] = {}
    settled_b: Dict[str, ReconciliationRow] = {}
    leftover_a: Dict[str, ReconciliationRow] = {}
    leftover_b: Dict[str, ReconciliationRow] = {}

    for pair in matched:
        settled_a[pair.row_a.row_id] = pair.row_a
        settled_b[pair.row_b.row_id] = pair.row_b

    for item in reviewed:
        if item.decision == "confirmed":
            if item.row_a is not None:
                settled_a[item.row_a.row_id] = item.row_a
            if item.row_b is not None:
                settled_b[item.row_b.row_id] = item.row_b
            if item.kind == "ambiguous":
                for c in item.candidates:
                    if c.row_id == item.selected_candidate_id:
                        settled_b[c.row_id] = c
                    else:
                        leftover_b[c.row_id] = c  # listed as a candidate, but never actually used
            if item.kind in ("group_match", "sum_match"):
                for g in item.group:
                    (settled_a if g.source == "sap_odoo" else settled_b)[g.row_id] = g
        else:
            # Dismissed, or still unresolved (decision is None) -- every
            # row this item touched goes back into the pool.
            if item.row_a is not None:
                leftover_a[item.row_a.row_id] = item.row_a
            if item.row_b is not None:
                leftover_b[item.row_b.row_id] = item.row_b
            for c in item.candidates:
                leftover_b[c.row_id] = c
            for g in item.group:
                (leftover_a if g.source == "sap_odoo" else leftover_b)[g.row_id] = g

    for row_id in settled_a:
        leftover_a.pop(row_id, None)
    for row_id in settled_b:
        leftover_b.pop(row_id, None)

    return list(leftover_a.values()), list(leftover_b.values())


def _within_date_ceiling(a: Optional[date_type], b: Optional[date_type], max_days: int) -> bool:
    """Pass 5's only bound in place of Pass 4's signal requirement — wide
    but finite, so a candidate from an obviously unrelated period doesn't
    get pulled into a combination just because the amounts happen to add
    up. A missing date doesn't exclude a candidate here (the amount+
    group-size cap is still doing the real limiting work) — a hard date
    requirement would defeat the point of this pass for real installment
    data, where dates are exactly what's NOT reliably comparable."""
    if a is None or b is None:
        return True
    return abs((a - b).days) <= max_days


def _leftover_sum_combos(
    target: ReconciliationRow, pool: List[ReconciliationRow],
    amount_tolerance: Decimal, max_date_spread_days: int,
) -> List[List[ReconciliationRow]]:
    """Same bounded-combination search as Pass 4's _find_group_matches
    (same GROUP_MATCH_MIN/MAX_SIZE cap, same "return every qualifying
    combination, let the caller reject multi-combination cases as
    ambiguous" contract) but against a candidate pool bounded ONLY by
    _within_date_ceiling — no description/reference/date-proximity signal
    requirement, since finding cases with none of those is the reason this
    pass exists at all."""
    if target.amount is None:
        return []
    candidates = [
        r for r in pool
        if r.amount is not None and _within_date_ceiling(target.date, r.date, max_date_spread_days)
    ]
    if len(candidates) < GROUP_MATCH_MIN_SIZE:
        return []
    found: List[List[ReconciliationRow]] = []
    max_size = min(GROUP_MATCH_MAX_SIZE, len(candidates))
    for size in range(GROUP_MATCH_MIN_SIZE, max_size + 1):
        for combo in itertools.combinations(candidates, size):
            total = sum((r.amount for r in combo), Decimal("0"))
            if abs(total - target.amount) <= amount_tolerance:
                found.append(list(combo))
    return found


def find_leftover_sum_matches(
    leftover_a: List[ReconciliationRow], leftover_b: List[ReconciliationRow],
    amount_tolerance: Decimal = AMOUNT_TOLERANCE,
    max_date_spread_days: int = SUM_MATCH_MAX_DATE_SPREAD_DAYS,
) -> List[ReviewItem]:
    """Pass 5 itself, called from services/comparator/main.py's POST
    /reconcile-leftovers. Same two-direction, claim-as-you-go structure as
    Pass 4's group_match search (including the same documented cross-target
    limitation — see reconcile()'s Direction 1 comment), but running
    directly against leftover_a/leftover_b — build_leftover_pool() is what
    already bounds this to items a full human review pass left open, not
    the original dataset. Always returns "sum_match" ReviewItems, never a
    MatchedPair — confirm/dismiss/unresolved is left entirely to the
    person, same as every other review kind."""
    review_counter = 0
    sum_items: List[ReviewItem] = []
    consumed_a_ids: Set[str] = set()
    consumed_b_ids: Set[str] = set()

    for target_b in leftover_b:
        available_a = [r for r in leftover_a if r.row_id not in consumed_a_ids]
        combos = _leftover_sum_combos(target_b, available_a, amount_tolerance, max_date_spread_days)
        if len(combos) == 1:
            review_counter += 1
            sum_items.append(ReviewItem(
                item_id=f"sum_review_{review_counter}", kind="sum_match", row_b=target_b, group=combos[0],
            ))
            consumed_a_ids.update(r.row_id for r in combos[0])
            consumed_b_ids.add(target_b.row_id)

    for target_a in leftover_a:
        if target_a.row_id in consumed_a_ids:
            continue
        available_b = [r for r in leftover_b if r.row_id not in consumed_b_ids]
        combos = _leftover_sum_combos(target_a, available_b, amount_tolerance, max_date_spread_days)
        if len(combos) == 1:
            review_counter += 1
            sum_items.append(ReviewItem(
                item_id=f"sum_review_{review_counter}", kind="sum_match", row_a=target_a, group=combos[0],
            ))
            consumed_a_ids.add(target_a.row_id)
            consumed_b_ids.update(r.row_id for r in combos[0])

    return sum_items


def drop_stale_unmatched_items(matched: List[MatchedPair], reviewed: List[ReviewItem]) -> List[ReviewItem]:
    """Called from services/comparator/main.py's POST /confirm, on every
    call (not just after a Pass-5 round). Once a row gets a real, confirmed
    home — a clean match, a confirmed fuzzy_match/amount_mismatch pair, a
    confirmed ambiguous item's selected candidate, or a confirmed
    group_match/sum_match's single side or group members — any OTHER item
    still sitting in `reviewed` as a plain "unmatched" placeholder FOR THAT
    SAME ROW is now factually wrong (it asserts the row has no match at
    all) and gets dropped. Found via a real test: confirming a Pass-5
    sum_match left both the new "Confirmed" grouping row AND the original
    round-1 "Unmatched / Unresolved" rows for the exact same underlying
    transactions in the same final report, contradicting each other.
    Deliberately scoped to "unmatched" only — a DISMISSED or still-
    unresolved item for a DIFFERENT candidate pairing on that row stays
    (dismissing one specific pairing is a true, non-contradictory
    statement regardless of what else later happens to that row; only
    "unmatched" claims the row has no home anywhere, which stops being
    true once it does)."""
    settled_ids: Set[str] = set()
    for pair in matched:
        settled_ids.add(pair.row_a.row_id)
        settled_ids.add(pair.row_b.row_id)
    for item in reviewed:
        if item.decision != "confirmed":
            continue
        if item.row_a is not None:
            settled_ids.add(item.row_a.row_id)
        if item.row_b is not None:
            settled_ids.add(item.row_b.row_id)
        if item.kind == "ambiguous" and item.selected_candidate_id:
            settled_ids.add(item.selected_candidate_id)
        for g in item.group:
            settled_ids.add(g.row_id)

    cleaned = []
    for item in reviewed:
        if item.kind == "unmatched":
            row = item.row_a or item.row_b
            if row is not None and row.row_id in settled_ids:
                continue  # stale -- this row now has a real home elsewhere
        cleaned.append(item)
    return cleaned
