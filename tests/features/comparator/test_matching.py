from datetime import date
from decimal import Decimal

from features.comparator.matching import build_leftover_pool, drop_stale_unmatched_items, find_leftover_sum_matches, reconcile
from features.comparator.models import MatchedPair, ReconciliationRow, ReviewItem


def _row(row_id, source, reference=None, d=None, amount=None, desc=None):
    return ReconciliationRow(
        row_id=row_id, source=source, reference=reference,
        date=date(*d) if d else None,
        amount=Decimal(str(amount)) if amount is not None else None,
        description=desc,
    )


def test_exact_reference_match_is_case_and_whitespace_insensitive():
    rows_a = [_row("a1", "sap_odoo", reference=" INV-001 ", d=(2026, 6, 1), amount=1500)]
    rows_b = [_row("b1", "second_doc", reference="inv-001", d=(2026, 6, 1), amount=1500)]

    result = reconcile(rows_a, rows_b)

    assert len(result.matched) == 1
    assert result.matched[0].match_type == "reference"
    assert not result.needs_review


def test_reference_match_with_differing_amount_becomes_amount_mismatch_review_item():
    """A shared reference alone doesn't make a clean match — the amounts
    must also agree within tolerance. This is the classic reconciliation
    exception (partial payment, pricing correction, data entry error), so
    it must be surfaced for review, not silently folded into matched."""
    rows_a = [_row("a1", "sap_odoo", reference="INV-2110", d=(2026, 6, 18), amount=4200.00)]
    rows_b = [_row("b1", "second_doc", reference="INV-2110", d=(2026, 6, 19), amount=4750.00)]

    result = reconcile(rows_a, rows_b)

    assert not result.matched
    assert len(result.needs_review) == 1
    item = result.needs_review[0]
    assert item.kind == "amount_mismatch"
    assert item.row_a.row_id == "a1"
    assert item.row_b.row_id == "b1"
    # 550/4200 = ~13.1% -- close to 15% but outside the +/-0.5pp band, so
    # this must NOT get mislabeled as a VAT gap.
    assert item.possible_vat_gap is False


def test_amount_mismatch_near_15_percent_gap_flagged_as_possible_vat_gap():
    """A gap right at Saudi VAT's 15% is the classic inclusive-vs-exclusive
    recording difference -- diagnostic only, doesn't change that this is
    still an amount_mismatch review item, just adds the hint."""
    rows_a = [_row("a1", "sap_odoo", reference="INV-500", d=(2026, 6, 1), amount=1000.00)]
    rows_b = [_row("b1", "second_doc", reference="INV-500", d=(2026, 6, 1), amount=1150.00)]  # exactly +15%

    result = reconcile(rows_a, rows_b)

    assert len(result.needs_review) == 1
    item = result.needs_review[0]
    assert item.kind == "amount_mismatch"
    assert item.possible_vat_gap is True


def test_vat_gap_tolerance_boundary():
    """+/-0.5 percentage points around 15% -- 14.5% and 15.5% both count,
    14.4% and 15.6% don't. Confirms the band is exactly what it's
    documented as, not accidentally wider or narrower."""
    base = Decimal("1000.00")

    def gap_result(pct: str):
        rows_a = [_row("a1", "sap_odoo", reference="INV-X", d=(2026, 6, 1), amount=base)]
        rows_b = [_row("b1", "second_doc", reference="INV-X", d=(2026, 6, 1), amount=base * (1 + Decimal(pct) / 100))]
        result = reconcile(rows_a, rows_b)
        return result.needs_review[0].possible_vat_gap

    assert gap_result("14.5") is True
    assert gap_result("15.5") is True
    assert gap_result("14.4") is False
    assert gap_result("15.6") is False


def test_reference_match_within_amount_tolerance_still_matches_cleanly():
    """A tiny rounding-level difference (well within tolerance) shouldn't
    be treated as a mismatch — only a difference exceeding amount_tolerance
    should divert a reference match to review."""
    rows_a = [_row("a1", "sap_odoo", reference="INV-9", d=(2026, 6, 1), amount=100.00)]
    rows_b = [_row("b1", "second_doc", reference="INV-9", d=(2026, 6, 1), amount=100.005)]

    result = reconcile(rows_a, rows_b, amount_tolerance=Decimal("0.01"))

    assert len(result.matched) == 1
    assert result.matched[0].match_type == "reference"
    assert not result.needs_review


def test_unique_amount_match_when_no_reference():
    rows_a = [_row("a1", "sap_odoo", d=(2026, 6, 5), amount=750.50)]
    rows_b = [_row("b1", "second_doc", d=(2026, 6, 5), amount=750.50)]

    result = reconcile(rows_a, rows_b)

    assert len(result.matched) == 1
    assert result.matched[0].match_type == "amount_unique"
    assert not result.needs_review


def test_duplicate_amount_does_not_auto_match_falls_to_fuzzy_pass():
    """Two rows sharing the exact same amount on each side must not be
    resolved by the unique-amount pass — it should fall through to the
    fuzzy (date-proximity) pass instead, and still require review."""
    rows_a = [_row("a1", "sap_odoo", d=(2026, 6, 10), amount=500.00)]
    rows_b = [
        _row("b1", "second_doc", d=(2026, 6, 11), amount=500.00),  # 1 day off -> in window
        _row("b2", "second_doc", d=(2026, 6, 25), amount=500.00),  # 15 days off -> out of window
    ]

    result = reconcile(rows_a, rows_b)

    assert not result.matched
    kinds = {item.item_id: item.kind for item in result.needs_review}
    fuzzy_items = [i for i in result.needs_review if i.kind == "fuzzy_match"]
    assert len(fuzzy_items) == 1
    assert fuzzy_items[0].row_a.row_id == "a1"
    assert fuzzy_items[0].row_b.row_id == "b1"
    unmatched_items = [i for i in result.needs_review if i.kind == "unmatched"]
    assert len(unmatched_items) == 1
    assert unmatched_items[0].row_b.row_id == "b2"


def test_two_equally_valid_row_a_candidates_for_one_row_b_are_both_ambiguous():
    """Regression test: two real payments, same amount, same vendor, no
    shared reference on either side, against a bank statement with only
    ONE matching receipt. A prior version resolved this as a silent
    fuzzy_match against whichever payment came first in the file, leaving
    the second one plainly "unmatched" with no trace it had ever been a
    candidate. Both should come back "ambiguous" instead — genuinely tied,
    not something the file's row order should get to decide."""
    rows_a = [
        _row("a1", "sap_odoo", reference="PMT-8801", d=(2026, 8, 5), amount=2400.00),
        _row("a2", "sap_odoo", reference="PMT-8802", d=(2026, 8, 6), amount=2400.00),
    ]
    rows_b = [_row("b1", "second_doc", reference=None, d=(2026, 8, 6), amount=2400.00)]

    result = reconcile(rows_a, rows_b)

    assert not result.matched
    assert len(result.needs_review) == 2
    kinds = {item.row_a.row_id: item.kind for item in result.needs_review}
    assert kinds == {"a1": "ambiguous", "a2": "ambiguous"}
    for item in result.needs_review:
        assert {c.row_id for c in item.candidates} == {"b1"}
    # b1 must not ALSO show up as its own standalone "unmatched" row_b item
    # — it's fully accounted for via both ambiguous items above.
    assert all(i.row_b is None for i in result.needs_review)


def test_ambiguous_when_multiple_candidates_in_window():
    rows_a = [_row("a1", "sap_odoo", d=(2026, 6, 10), amount=500.00)]
    rows_b = [
        _row("b1", "second_doc", d=(2026, 6, 11), amount=500.00),
        _row("b2", "second_doc", d=(2026, 6, 12), amount=500.00),
    ]

    result = reconcile(rows_a, rows_b)

    assert not result.matched
    assert len(result.needs_review) == 1
    item = result.needs_review[0]
    assert item.kind == "ambiguous"
    assert {c.row_id for c in item.candidates} == {"b1", "b2"}
    # Neither candidate gets its own standalone "unmatched" item — they're
    # accounted for as candidates, not double-listed.
    assert all(i.row_b is None or i.row_b.row_id not in ("b1", "b2") for i in result.needs_review)


def test_fully_unmatched_on_both_sides():
    rows_a = [_row("a1", "sap_odoo", d=(2026, 6, 1), amount=99.99)]
    rows_b = [_row("b1", "second_doc", d=(2026, 7, 1), amount=55.00)]

    result = reconcile(rows_a, rows_b)

    assert not result.matched
    assert len(result.needs_review) == 2
    assert {i.kind for i in result.needs_review} == {"unmatched"}


def test_missing_date_prevents_fuzzy_match_not_a_crash():
    """A row missing a date can't be fuzzy-matched (date-proximity can't be
    evaluated) — with the amount not unique (so Pass 2 can't resolve it
    either), it should end up unmatched, not raise or silently match."""
    rows_a = [
        _row("a1", "sap_odoo", d=None, amount=500.00),
        _row("a2", "sap_odoo", d=(2026, 6, 11), amount=500.00),
    ]
    rows_b = [_row("b1", "second_doc", d=(2026, 6, 11), amount=500.00)]

    result = reconcile(rows_a, rows_b)

    assert not result.matched
    kinds = {i.item_id: i.kind for i in result.needs_review}
    assert set(kinds.values()) == {"fuzzy_match", "unmatched"}
    fuzzy_items = [i for i in result.needs_review if i.kind == "fuzzy_match"]
    assert fuzzy_items[0].row_a.row_id == "a2"  # the dated row matches b1
    unmatched_items = [i for i in result.needs_review if i.kind == "unmatched"]
    assert unmatched_items[0].row_a.row_id == "a1"  # the undated row can't be fuzzy-matched


def test_empty_inputs_produce_empty_result():
    result = reconcile([], [])
    assert result.matched == []
    assert result.needs_review == []


def test_default_date_proximity_excludes_a_pair_5_days_apart():
    """Regression guard for the default (config.py's DATE_PROXIMITY_DAYS=3).
    Uses a duplicate-amount decoy (a2) the same way
    test_duplicate_amount_does_not_auto_match_falls_to_fuzzy_pass does —
    without it, equal amounts unique on both sides would resolve in Pass 2
    regardless of date, and this test wouldn't actually be exercising the
    date-proximity check at all."""
    rows_a = [
        _row("a1", "sap_odoo", d=(2026, 6, 1), amount=500.00),
        _row("a2", "sap_odoo", d=(2026, 6, 20), amount=500.00),  # decoy: forces non-uniqueness, far from b1 either way
    ]
    rows_b = [_row("b1", "second_doc", d=(2026, 6, 6), amount=500.00)]  # 5 days from a1, 14 from a2

    result = reconcile(rows_a, rows_b)

    assert not result.matched
    assert len(result.needs_review) == 3
    assert {i.kind for i in result.needs_review} == {"unmatched"}


def test_custom_date_proximity_days_widens_the_fuzzy_window():
    """The same setup as the default-window test above, but with
    date_proximity_days widened to 10 — now a1 (5 days from b1) falls
    inside the window and becomes a fuzzy match, while a2 (14 days) still
    doesn't. Proves the override actually reaches the matching logic, not
    just accepted and ignored."""
    rows_a = [
        _row("a1", "sap_odoo", d=(2026, 6, 1), amount=500.00),
        _row("a2", "sap_odoo", d=(2026, 6, 20), amount=500.00),
    ]
    rows_b = [_row("b1", "second_doc", d=(2026, 6, 6), amount=500.00)]

    result = reconcile(rows_a, rows_b, date_proximity_days=10)

    assert not result.matched
    fuzzy_items = [i for i in result.needs_review if i.kind == "fuzzy_match"]
    assert len(fuzzy_items) == 1
    assert fuzzy_items[0].row_a.row_id == "a1"
    assert fuzzy_items[0].row_b.row_id == "b1"
    unmatched_items = [i for i in result.needs_review if i.kind == "unmatched"]
    assert len(unmatched_items) == 1
    assert unmatched_items[0].row_a.row_id == "a2"


def test_custom_amount_tolerance_widens_what_counts_as_a_match():
    """Two amounts 0.50 apart don't match under the default AMOUNT_TOLERANCE
    (0.01), but do once the tolerance is widened past 0.50. Tolerance
    0.60 is deliberately not a round divisor of the amounts involved, so
    this stays a Pass-3 fuzzy match rather than accidentally landing both
    amounts in the same Pass-2 quantization bucket (which a tolerance like
    1.00 can do, since Pass 2 buckets on the same tolerance value — that's
    a real, separate behavior, not what this test is checking)."""
    rows_a = [_row("a1", "sap_odoo", d=(2026, 6, 1), amount=500.00)]
    rows_b = [_row("b1", "second_doc", d=(2026, 6, 1), amount=500.50)]

    default_result = reconcile(rows_a, rows_b)
    assert not default_result.matched
    assert {i.kind for i in default_result.needs_review} == {"unmatched"}

    widened_result = reconcile(rows_a, rows_b, amount_tolerance=Decimal("0.60"))
    assert not widened_result.matched
    assert len(widened_result.needs_review) == 1
    assert widened_result.needs_review[0].kind == "fuzzy_match"


# --- Pass 4: group/consolidation matching -----------------------------------


def test_two_of_ours_summing_to_one_of_theirs_is_flagged_group_match():
    """Real scenario: a vendor bills two of our purchase orders as one
    consolidated invoice. Same subject matter ("Roofing Sheets") and dates
    within the proximity window -- a real connecting signal, not just a
    coincidental sum -- so this should be proposed as a group_match, never
    auto-confirmed."""
    rows_a = [
        _row("a1", "sap_odoo", reference="PO-5540", d=(2026, 7, 15), amount=6200,
             desc="Purchase - Roofing Sheets (Order A)"),
        _row("a2", "sap_odoo", reference="PO-5541", d=(2026, 7, 15), amount=3800,
             desc="Purchase - Roofing Sheets (Order B)"),
    ]
    rows_b = [_row("b1", "second_doc", reference="ZBM-INV-8830", d=(2026, 7, 16), amount=10000,
                    desc="Roofing Sheets - Consolidated Order")]

    result = reconcile(rows_a, rows_b)

    assert not result.matched
    assert len(result.needs_review) == 1
    item = result.needs_review[0]
    assert item.kind == "group_match"
    assert item.row_b.row_id == "b1"
    assert item.row_a is None
    assert {r.row_id for r in item.group} == {"a1", "a2"}


def test_one_of_ours_summing_to_two_of_theirs_is_flagged_group_match():
    """Reverse direction: one of our invoices got paid via two separate
    vendor-side receipts."""
    rows_a = [_row("a1", "sap_odoo", reference="INV-100", d=(2026, 9, 1), amount=5000,
                    desc="Consulting Engagement Full Fee")]
    rows_b = [
        _row("b1", "second_doc", reference="RCPT-01", d=(2026, 9, 1), amount=3000,
             desc="Consulting Engagement Deposit"),
        _row("b2", "second_doc", reference="RCPT-02", d=(2026, 9, 2), amount=2000,
             desc="Consulting Engagement Balance"),
    ]

    result = reconcile(rows_a, rows_b)

    assert not result.matched
    assert len(result.needs_review) == 1
    item = result.needs_review[0]
    assert item.kind == "group_match"
    assert item.row_a.row_id == "a1"
    assert item.row_b is None
    assert {r.row_id for r in item.group} == {"b1", "b2"}


def test_coincidental_sum_without_connecting_signal_is_not_group_matched():
    """Two totally unrelated transactions (different months, unrelated
    subject matter and reference numbering) that happen to numerically sum
    to a third, equally unrelated transaction. This is the exact scenario
    the group-matching pass must NOT resolve -- no shared date/description/
    reference signal means it's excluded from the candidate pool entirely,
    so the coincidence never even reaches the sum check."""
    rows_a = [
        _row("a1", "sap_odoo", reference="XR-9910", d=(2026, 3, 2), amount=4123.50,
             desc="Office Supplies Restock"),
        _row("a2", "sap_odoo", reference="QZ-2201", d=(2026, 11, 28), amount=1876.50,
             desc="IT Equipment Warranty Renewal"),
    ]
    rows_b = [_row("b1", "second_doc", reference="ZK-7777", d=(2026, 6, 15), amount=6000.00,
                    desc="Legal Consulting Retainer")]

    result = reconcile(rows_a, rows_b)

    assert not result.matched
    assert len(result.needs_review) == 3
    assert {i.kind for i in result.needs_review} == {"unmatched"}


def test_multiple_valid_groupings_are_left_unresolved_not_guessed():
    """Two DIFFERENT pairs, both connected to the target on date/description
    grounds, both summing to the target's amount within tolerance. Genuinely
    ambiguous which pair is real -- must not be resolved to either, per the
    explicit "don't guess between equally-valid readings" requirement."""
    rows_a = [
        _row("a1", "sap_odoo", reference="PO-1", d=(2026, 4, 1), amount=3000,
             desc="Purchase - Cement Delivery"),
        _row("a2", "sap_odoo", reference="PO-2", d=(2026, 4, 1), amount=2000,
             desc="Purchase - Cement Delivery"),
        _row("a3", "sap_odoo", reference="PO-3", d=(2026, 4, 1), amount=1000,
             desc="Purchase - Cement Delivery"),
        _row("a4", "sap_odoo", reference="PO-4", d=(2026, 4, 1), amount=4000,
             desc="Purchase - Cement Delivery"),
    ]
    # a1+a2 == 5000, AND a3+a4 == 5000 -- two equally valid groupings.
    rows_b = [_row("b1", "second_doc", reference="V-INV-1", d=(2026, 4, 2), amount=5000,
                    desc="Cement Delivery - Consolidated")]

    result = reconcile(rows_a, rows_b)

    assert not result.matched
    assert {i.kind for i in result.needs_review} == {"unmatched"}
    assert len(result.needs_review) == 5  # a1, a2, a3, a4, b1 all individually unmatched


def test_group_size_is_capped_and_a_five_way_sum_is_not_found():
    """GROUP_MATCH_MAX_SIZE defaults to 4 — a target only explainable by
    combining all 5 signal-connected candidates must not be found (and
    since no smaller subset sums correctly either here, everything stays
    unmatched rather than partially/incorrectly grouped)."""
    rows_a = [
        _row(f"a{i}", "sap_odoo", reference=f"PO-{600 + i}", d=(2026, 5, 1), amount=1000 + i,
             desc="Purchase - Bulk Cable Order")
        for i in range(5)
    ]
    total = sum(1000 + i for i in range(5))
    rows_b = [_row("b1", "second_doc", reference="V-9001", d=(2026, 5, 2), amount=total,
                    desc="Bulk Cable Order - Consolidated")]

    result = reconcile(rows_a, rows_b)

    assert not result.matched
    assert {i.kind for i in result.needs_review} == {"unmatched"}
    assert len(result.needs_review) == 6


# --- Pass 5: build_leftover_pool ---------------------------------------------


def test_leftover_pool_excludes_clean_matches_and_confirmed_pairs():
    matched = [MatchedPair(row_a=_row("a1", "sap_odoo", amount=100), row_b=_row("b1", "second_doc", amount=100), match_type="reference")]
    reviewed = [
        ReviewItem(item_id="r1", kind="fuzzy_match", row_a=_row("a2", "sap_odoo", amount=200), row_b=_row("b2", "second_doc", amount=200), decision="confirmed"),
    ]
    leftover_a, leftover_b = build_leftover_pool(matched, reviewed)
    assert leftover_a == []
    assert leftover_b == []


def test_leftover_pool_returns_dismissed_and_unresolved_rows():
    reviewed = [
        ReviewItem(item_id="r1", kind="fuzzy_match", row_a=_row("a1", "sap_odoo", amount=100), row_b=_row("b1", "second_doc", amount=100), decision="dismissed"),
        ReviewItem(item_id="r2", kind="unmatched", row_a=_row("a2", "sap_odoo", amount=50), decision=None),
    ]
    leftover_a, leftover_b = build_leftover_pool([], reviewed)
    assert {r.row_id for r in leftover_a} == {"a1", "a2"}
    assert {r.row_id for r in leftover_b} == {"b1"}


def test_leftover_pool_confirmed_ambiguous_keeps_only_selected_candidate_settled():
    c1 = _row("b1", "second_doc", amount=100)
    c2 = _row("b2", "second_doc", amount=100)
    reviewed = [
        ReviewItem(item_id="r1", kind="ambiguous", row_a=_row("a1", "sap_odoo", amount=100),
                   candidates=[c1, c2], decision="confirmed", selected_candidate_id="b1"),
    ]
    leftover_a, leftover_b = build_leftover_pool([], reviewed)
    assert leftover_a == []  # row_a settled
    assert {r.row_id for r in leftover_b} == {"b2"}  # only the unselected candidate returns


def test_leftover_pool_confirmed_group_match_settles_whole_group():
    group = [_row("a1", "sap_odoo", amount=60), _row("a2", "sap_odoo", amount=40)]
    reviewed = [
        ReviewItem(item_id="r1", kind="group_match", row_b=_row("b1", "second_doc", amount=100),
                   group=group, decision="confirmed"),
    ]
    leftover_a, leftover_b = build_leftover_pool([], reviewed)
    assert leftover_a == []
    assert leftover_b == []


def test_leftover_pool_settled_wins_over_leftover_for_contested_candidate():
    """The exact PMT-8801/PMT-8802 shape: one card confirmed against a
    shared candidate, the sibling card dismissed but still listing that
    same candidate. The candidate must NOT come back as leftover just
    because a different, wrong item also referenced it."""
    shared_candidate = _row("b1", "second_doc", amount=2400)
    reviewed = [
        ReviewItem(item_id="r1", kind="ambiguous", row_a=_row("a1", "sap_odoo", amount=2400),
                   candidates=[shared_candidate], decision="confirmed", selected_candidate_id="b1"),
        ReviewItem(item_id="r2", kind="ambiguous", row_a=_row("a2", "sap_odoo", amount=2400),
                   candidates=[shared_candidate], decision="dismissed"),
    ]
    leftover_a, leftover_b = build_leftover_pool([], reviewed)
    assert {r.row_id for r in leftover_a} == {"a2"}  # the dismissed card's own row still returns
    assert leftover_b == []  # but the shared candidate stays settled


# --- Pass 5: find_leftover_sum_matches ---------------------------------------


def test_leftover_sum_match_catches_installments_with_no_signal_at_all():
    """The exact real-world worst case Pass 4 verifiably can't catch:
    generic descriptions, non-sequential bank-style references, dates
    spread across weeks. Pass 5 has no signal requirement at all, so this
    should be found as long as it's within the leftover pool and the date
    ceiling."""
    target = _row("inv1", "sap_odoo", reference="INV-3001", d=(2026, 5, 1), amount=10000,
                   desc="Invoice - Consulting Services")
    installments = [
        _row("p1", "second_doc", reference="FT26051087234", d=(2026, 5, 10), amount=3000, desc="Payment 1"),
        _row("p2", "second_doc", reference="FT26052291567", d=(2026, 5, 25), amount=3000, desc="Payment 2"),
        _row("p3", "second_doc", reference="FT26061534982", d=(2026, 6, 15), amount=4000, desc="Payment 3"),
    ]

    sum_items = find_leftover_sum_matches([target], installments)

    assert len(sum_items) == 1
    item = sum_items[0]
    assert item.kind == "sum_match"
    assert item.row_a.row_id == "inv1"
    assert {r.row_id for r in item.group} == {"p1", "p2", "p3"}
    assert item.decision is None  # never auto-confirmed


def test_leftover_sum_match_direction_several_of_theirs_to_one_of_ours():
    target = _row("a1", "sap_odoo", reference="INV-1", d=(2026, 3, 1), amount=5000, desc="Consulting")
    receipts = [
        _row("b1", "second_doc", reference="X1", d=(2026, 3, 10), amount=3000, desc="Receipt 1"),
        _row("b2", "second_doc", reference="X2", d=(2026, 3, 30), amount=2000, desc="Receipt 2"),
    ]

    sum_items = find_leftover_sum_matches([target], receipts)

    assert len(sum_items) == 1
    item = sum_items[0]
    assert item.kind == "sum_match"
    assert item.row_b is None
    assert item.row_a.row_id == "a1"
    assert {r.row_id for r in item.group} == {"b1", "b2"}


def test_leftover_sum_match_respects_date_ceiling():
    """Same amounts/shape as the successful installment test, but pushed
    well past the 90-day default ceiling -- must NOT be found. Confirms
    the "wide but finite" bound is actually enforced, not just documented."""
    target = _row("inv1", "sap_odoo", reference="INV-1", d=(2026, 1, 1), amount=10000, desc="Consulting")
    installments = [
        _row("p1", "second_doc", reference="X1", d=(2026, 6, 1), amount=3000, desc="Payment 1"),
        _row("p2", "second_doc", reference="X2", d=(2026, 6, 15), amount=3000, desc="Payment 2"),
        _row("p3", "second_doc", reference="X3", d=(2026, 7, 1), amount=4000, desc="Payment 3"),
    ]

    sum_items = find_leftover_sum_matches([target], installments, max_date_spread_days=90)

    assert sum_items == []


def test_leftover_sum_match_multiple_valid_combinations_are_skipped():
    target = _row("b1", "second_doc", reference="V1", d=(2026, 4, 2), amount=5000, desc="Consolidated")
    candidates = [
        _row("a1", "sap_odoo", reference="P1", d=(2026, 4, 1), amount=3000, desc="Item"),
        _row("a2", "sap_odoo", reference="P2", d=(2026, 4, 1), amount=2000, desc="Item"),
        _row("a3", "sap_odoo", reference="P3", d=(2026, 4, 1), amount=1000, desc="Item"),
        _row("a4", "sap_odoo", reference="P4", d=(2026, 4, 1), amount=4000, desc="Item"),
    ]  # a1+a2 == 5000 AND a3+a4 == 5000 -- genuinely ambiguous

    sum_items = find_leftover_sum_matches(candidates, [target])

    assert sum_items == []


# --- Pass 5: drop_stale_unmatched_items --------------------------------------


def test_stale_unmatched_items_are_dropped_once_a_sum_match_settles_the_row():
    """The exact real bug found via a live test: round 1 leaves an invoice
    and its 3 payments as 4 separate "unmatched" items; round 2's
    sum_match groups them and gets confirmed. The 4 original "unmatched"
    placeholders must not survive alongside the new confirmed grouping —
    they're now factually wrong (each claims its row has no match at all)."""
    invoice = _row("inv1", "sap_odoo", amount=12000)
    payments = [_row(f"p{i}", "second_doc", amount=amt) for i, amt in enumerate([5000, 4000, 3000], start=1)]

    round1_unmatched = [
        ReviewItem(item_id="r1", kind="unmatched", row_a=invoice),
        *(ReviewItem(item_id=f"r{i+1}", kind="unmatched", row_b=p) for i, p in enumerate(payments, start=1)),
    ]
    sum_match = ReviewItem(item_id="sum_1", kind="sum_match", row_a=invoice, group=payments, decision="confirmed")

    cleaned = drop_stale_unmatched_items([], round1_unmatched + [sum_match])

    assert cleaned == [sum_match]  # all 4 stale placeholders dropped, the real resolution kept


def test_dismissed_items_are_not_treated_as_stale():
    """Only "unmatched" placeholders get cleaned up -- a DISMISSED pairing
    remains a true, non-contradictory statement ("this candidate wasn't
    it") even after the row finds a home elsewhere via a different item,
    so it must survive."""
    row = _row("a1", "sap_odoo", amount=500)
    wrong_candidate = _row("b1", "second_doc", amount=500)
    real_candidate = _row("b2", "second_doc", amount=500)

    dismissed_item = ReviewItem(item_id="r1", kind="fuzzy_match", row_a=row, row_b=wrong_candidate, decision="dismissed")
    confirmed_item = ReviewItem(item_id="r2", kind="fuzzy_match", row_a=row, row_b=real_candidate, decision="confirmed")

    cleaned = drop_stale_unmatched_items([], [dismissed_item, confirmed_item])

    assert dismissed_item in cleaned
    assert confirmed_item in cleaned


def test_unmatched_item_survives_if_its_row_is_still_genuinely_unsettled():
    """Sanity check the cleanup isn't overzealous -- an unmatched item
    whose row genuinely has no confirmed home anywhere else must stay."""
    genuinely_unmatched = ReviewItem(item_id="r1", kind="unmatched", row_a=_row("a1", "sap_odoo", amount=999))
    unrelated_confirmed = ReviewItem(
        item_id="r2", kind="fuzzy_match",
        row_a=_row("a2", "sap_odoo", amount=1), row_b=_row("b2", "second_doc", amount=1), decision="confirmed",
    )

    cleaned = drop_stale_unmatched_items([], [genuinely_unmatched, unrelated_confirmed])

    assert genuinely_unmatched in cleaned
