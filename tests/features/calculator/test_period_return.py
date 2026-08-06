"""
Tests for features/calculator/period_return.py — job 2's period VAT
return preparation. Uses synthetic sources shaped like the real test
workbook this feature was built and manually verified against
(calculator_test_data_1.xlsx — net position -126.3, confirmed live), so
these stay fast/self-contained rather than depending on an external file.
"""

from decimal import Decimal

from features.calculator import period_return as pr

SALES_SOURCE = {
    "source_id": "0", "filename": "reg.xlsx", "sheet_label": "reg.xlsx (sheet 1 of 2)",
    "column_names": ["Invoice No", "Date", "Customer", "Tax Category", "Amount (SAR)", "Output VAT (SAR)"],
    "preamble": ["Test Co — Sales / Output VAT Register"],
    "rows": [
        {"Invoice No": "INV-1", "Date": "2026-07-01", "Customer": "A", "Tax Category": "Standard 15%",
         "Amount (SAR)": 1000, "Output VAT (SAR)": 150},
        {"Invoice No": "INV-2", "Date": "2026-07-02", "Customer": "B", "Tax Category": "Zero-rated (Export)",
         "Amount (SAR)": 500, "Output VAT (SAR)": 0},
        {"Invoice No": None, "Date": None, "Customer": None, "Tax Category": "TOTAL OUTPUT VAT:",
         "Amount (SAR)": None, "Output VAT (SAR)": 150},
    ],
    "guessed_role": "sales",
}

PURCHASE_SOURCE = {
    "source_id": "1", "filename": "reg.xlsx", "sheet_label": "reg.xlsx (sheet 2 of 2)",
    "column_names": ["Purchase Ref", "Date", "Vendor", "Expense Type", "Amount (SAR)", "Input VAT (SAR)", "Reclaimable?"],
    "preamble": ["Test Co — Purchases / Input VAT Register"],
    "rows": [
        {"Purchase Ref": "P1", "Date": "2026-07-03", "Vendor": "V1", "Expense Type": "Office Supplies",
         "Amount (SAR)": 200, "Input VAT (SAR)": 30, "Reclaimable?": "Yes"},
        {"Purchase Ref": "P2", "Date": "2026-07-04", "Vendor": "Restaurant XYZ", "Expense Type": "Client Entertainment",
         "Amount (SAR)": 800, "Input VAT (SAR)": 120, "Reclaimable?": None},
        {"Purchase Ref": "P3", "Date": "2026-07-05", "Vendor": "Fast Wheels", "Expense Type": "Vehicle Rental",
         "Amount (SAR)": 3000, "Input VAT (SAR)": 450, "Reclaimable?": None},
        {"Purchase Ref": None, "Date": None, "Vendor": None, "Expense Type": "TOTAL INPUT VAT:",
         "Amount (SAR)": None, "Input VAT (SAR)": 600, "Reclaimable?": None},
    ],
    "guessed_role": "purchases",
}


def test_find_description_col_prefers_expense_type_over_vendor():
    """Regression test: Vendor is listed before Expense Type in real
    exports, but the classification text needed for tier-1/tier-2
    matching is Expense Type ("Client Entertainment"), not the vendor's
    business name ("Restaurant XYZ") — a flat synonym list picking the
    first column match in file order used to return Vendor instead."""
    columns = ["Purchase Ref", "Date", "Vendor", "Expense Type", "Amount (SAR)", "Input VAT (SAR)"]
    assert pr._find_description_col(columns) == "Expense Type"


def test_find_description_col_falls_back_to_party_column():
    columns = ["Invoice No", "Date", "Customer", "Amount (SAR)"]
    assert pr._find_description_col(columns) == "Customer"


def test_category_rate_parsing():
    assert pr._category_rate("Standard 15%") == Decimal("0.15")
    assert pr._category_rate("Zero-rated (Export)") == Decimal("0.00")
    assert pr._category_rate("Exempt") == Decimal("0.00")
    assert pr._category_rate("Something unrecognized") is None
    assert pr._category_rate(None) is None


def test_is_total_row():
    assert pr._is_total_row({"A": None, "B": "TOTAL OUTPUT VAT:", "C": 150}) is True
    assert pr._is_total_row({"A": "INV-1", "B": "Standard 15%", "C": 150}) is False


def test_guess_role_purchases_from_reclaimable_column():
    assert pr._guess_role(PURCHASE_SOURCE["filename"], PURCHASE_SOURCE["preamble"], PURCHASE_SOURCE["column_names"]) == "purchases"


def test_guess_role_sales_from_preamble_text():
    assert pr._guess_role(SALES_SOURCE["filename"], SALES_SOURCE["preamble"], SALES_SOURCE["column_names"]) == "sales"


def test_guess_role_unknown_with_no_signals():
    assert pr._guess_role("data.xlsx", [], ["Line", "Amount"]) == "unknown"


def test_guess_business_name_from_preamble_em_dash():
    assert pr.guess_business_name([SALES_SOURCE]) == "Test Co"


def test_guess_business_name_skips_sources_with_no_matching_pattern_and_falls_back():
    no_name_source = {**SALES_SOURCE, "preamble": ["Period: Q3 2026 | Currency: SAR"]}
    assert pr.guess_business_name([no_name_source, PURCHASE_SOURCE]) is None or isinstance(
        pr.guess_business_name([no_name_source, PURCHASE_SOURCE]), str
    )
    # PURCHASE_SOURCE's own preamble does carry the pattern — a later
    # source in the list should still be checked if an earlier one doesn't
    # match, rather than giving up after the first miss.
    assert pr.guess_business_name([no_name_source, PURCHASE_SOURCE]) == "Test Co"


def test_guess_business_name_none_when_no_source_has_the_pattern():
    plain_source = {**SALES_SOURCE, "preamble": []}
    assert pr.guess_business_name([plain_source]) is None


def test_process_period_return_aggregates_and_flags_ambiguous_purchase():
    # PURCHASE_SOURCE has no tax-category column ("Expense Type" isn't one) —
    # answering with an explicit default here keeps this test focused on
    # aggregation/reclaimability, not the separate ask-first notice behavior
    # (see test_purchase_row_vat_recompute_falls_back_to_standard_rate).
    mapping = {"0": "sales", "1": "purchases"}
    result = pr.process_period_return([SALES_SOURCE, PURCHASE_SOURCE], mapping, default_rates={"1": "standard"})

    assert result["output_vat"] == "150"
    assert result["input_vat_total"] == "600"
    # P1 (30, source says Yes) reclaimable; P2 (120, entertainment) blocked;
    # P3 (450, vehicle) pending review -> excluded until resolved.
    assert result["reclaimable_input_vat"] == "30"
    assert result["net_position"] == "120"  # 150 - 30
    assert result["status"] == "awaiting_review"
    assert result["consistency_issues"] == []

    review_ids = {r["item_id"] for r in result["review_items"]}
    assert len(review_ids) == 1
    reviewed = result["review_items"][0]
    assert reviewed["description"] == "Vehicle Rental"
    assert reviewed["vat_amount"] == "450"


def test_apply_review_decisions_resolves_reclaimable():
    mapping = {"0": "sales", "1": "purchases"}
    result = pr.process_period_return([SALES_SOURCE, PURCHASE_SOURCE], mapping)
    pending_item_id = result["review_items"][0]["item_id"]

    final = pr.apply_review_decisions(result, {pending_item_id: True})

    assert final["status"] == "ready"
    assert final["review_items"] == []
    assert final["reclaimable_input_vat"] == "480"  # 30 (P1) + 450 (P3, now confirmed)
    assert final["net_position"] == "-330"  # 150 - 480


def test_apply_review_decisions_undecided_defaults_to_not_reclaimable():
    mapping = {"0": "sales", "1": "purchases"}
    result = pr.process_period_return([SALES_SOURCE, PURCHASE_SOURCE], mapping)

    final = pr.apply_review_decisions(result, {})  # nothing decided

    assert final["reclaimable_input_vat"] == "30"  # only P1 — P3 stays excluded, not defaulted to reclaimable
    assert final["status"] == "ready"


def test_declared_total_mismatch_is_flagged():
    bad_sales = {**SALES_SOURCE, "rows": [
        SALES_SOURCE["rows"][0], SALES_SOURCE["rows"][1],
        {"Invoice No": None, "Date": None, "Customer": None, "Tax Category": "TOTAL OUTPUT VAT:",
         "Amount (SAR)": None, "Output VAT (SAR)": 999},  # wrong on purpose
    ]}
    result = pr.process_period_return([bad_sales], {"0": "sales"})

    assert any("doesn't match" in i["message"] for i in result["consistency_issues"])


def test_raw_total_vs_reclaimable_total_compared_separately():
    """Regression test: a real register can carry TWO total rows — a raw
    total (all purchases) and a reclaimable-only total — that must not be
    cross-compared against each other's wrong figure."""
    result = pr.process_period_return([PURCHASE_SOURCE], {"1": "purchases"}, default_rates={"1": "standard"})
    # PURCHASE_SOURCE's one total row says 600 (raw) and matches the raw
    # row sum (30+120+450=600) — no issue expected from that row alone.
    assert result["consistency_issues"] == []

    with_reclaimable_total = {
        **PURCHASE_SOURCE,
        "rows": PURCHASE_SOURCE["rows"] + [
            {"Purchase Ref": None, "Date": None, "Vendor": None, "Expense Type": "TOTAL RECLAIMABLE INPUT VAT:",
             "Amount (SAR)": None, "Input VAT (SAR)": 30, "Reclaimable?": None},
        ],
    }
    result2 = pr.process_period_return([with_reclaimable_total], {"1": "purchases"}, default_rates={"1": "standard"})
    # Reclaimable total (30, matching P1 alone) must be compared against
    # the reclaimable subtotal, not the raw 600 total — no false mismatch.
    assert result2["consistency_issues"] == []


def test_source_without_vat_column_is_skipped_not_crashed():
    no_vat_source = {
        "source_id": "2", "filename": "misc.xlsx", "sheet_label": "misc.xlsx",
        "column_names": ["Line", "Amount (SAR)"], "preamble": [],
        "rows": [{"Line": "Total Output VAT (from Sales sheet)", "Amount (SAR)": 150}],
        "guessed_role": "unknown",
    }
    result = pr.process_period_return([no_vat_source], {"2": "sales"})

    assert result["output_vat"] == "0"
    assert any("Could not find a VAT" in i["message"] for i in result["consistency_issues"])


def test_ignored_source_is_excluded_entirely():
    result = pr.process_period_return([SALES_SOURCE, PURCHASE_SOURCE], {"0": "sales", "1": "ignore"})
    assert result["output_vat"] == "150"
    assert result["input_vat_total"] == "0"
    assert result["purchase_row_count"] == 0


def test_reclaimability_categories_match_zatca_implementing_regulations_article_50():
    """Article 50(1)(a)-(b) — entertainment/sporting/cultural/catering — is
    blocked outright, no business-use carve-out; Article 50(1)(c)-(e) —
    vehicles, their upkeep, and their fuel — is genuinely conditional per
    Article 50(2)'s work-exclusive/resale carve-out, hence tier 2 not
    tier 1. Verified directly against the regulation text, not general
    VAT-system knowledge (see config.py's citation)."""
    no_reclaimable_col_source = {
        "source_id": "9", "filename": "purchases.xlsx", "sheet_label": "purchases.xlsx",
        "column_names": ["Ref", "Expense Type", "Amount (SAR)", "Input VAT (SAR)"],
        "preamble": [],
        "rows": [
            {"Ref": "A", "Expense Type": "Catering for staff event", "Amount (SAR)": 100, "Input VAT (SAR)": 15},
            {"Ref": "B", "Expense Type": "Fuel for delivery fleet", "Amount (SAR)": 200, "Input VAT (SAR)": 30},
        ],
        "guessed_role": "purchases",
    }
    result = pr.process_period_return([no_reclaimable_col_source], {"9": "purchases"})

    by_ref = {row["description"]: row for row in result["purchase_rows"]}
    assert by_ref["Catering for staff event"]["reclaimable"] is False
    assert by_ref["Catering for staff event"]["needs_review"] is False  # tier 1 — decided automatically
    assert by_ref["Fuel for delivery fleet"]["needs_review"] is True  # tier 2 — needs a human decision


def test_purchase_row_vat_recompute_falls_back_to_standard_rate():
    """Regression test for a real gap found via live testing (against a
    dedicated isolated_per_row_mismatch_purchases.xlsx fixture, no
    category/rate column at all): with no explicit rate column, purchase
    rows went completely unchecked — a genuine SAR 10 per-row error was
    caught by nothing, since the register's own TOTAL row happened to be
    self-consistent with that same error. When no default_rates answer is
    given for a source with no category column, Standard 15% is now
    assumed (with a visible notice — see
    test_missing_rate_column_without_a_default_answer_notifies_and_assumes_standard)
    and this catches the error."""
    source = {
        "source_id": "5", "filename": "purchases.xlsx", "sheet_label": "purchases.xlsx",
        "column_names": ["Ref No", "Vendor", "Description", "Amount (SAR)", "VAT (SAR)"],
        "preamble": [],
        "rows": [
            # 1,000 at 15% should be 150.00 — register prints 160.00, a
            # genuine SAR 10 overstatement with no category column at all.
            {"Ref No": "PUR-301", "Vendor": "V1", "Description": "Office Chairs", "Amount (SAR)": 1000, "VAT (SAR)": 160},
            {"Ref No": "PUR-302", "Vendor": "V2", "Description": "Desk Lamps", "Amount (SAR)": 500, "VAT (SAR)": 75},
        ],
        "guessed_role": "purchases",
    }
    result = pr.process_period_return([source], {"5": "purchases"})

    messages = [i["message"] for i in result["consistency_issues"]]
    assert any("Office Chairs" in m and "150.00" in m and "160" in m for m in messages)
    assert not any("Desk Lamps" in m for m in messages)  # 500 * 15% = 75.00, exactly correct — no false positive


def _no_category_column_sales_source():
    return {
        "source_id": "6", "filename": "sales.xlsx", "sheet_label": "sales.xlsx",
        "column_names": ["Invoice No", "Customer", "Amount (SAR)", "Output VAT (SAR)"],
        "preamble": [],
        "rows": [
            # Genuinely zero-rated (an export) with no category column to say so.
            {"Invoice No": "INV-1", "Customer": "Overseas Co.", "Amount (SAR)": 1000, "Output VAT (SAR)": 0},
        ],
        "guessed_role": "sales",
    }


def test_missing_rate_column_without_a_default_answer_notifies_but_allows_zero_rated():
    """Superseded design, part 1: the standard-rate fallback used to be
    purchases-only and silent. Per explicit user direction, it's now
    symmetric (sales and purchases both) and never silent — no category
    column and no default_rates answer means we assume Standard 15% AND
    say so, rather than either staying silent (old purchases-only bug) or
    guessing without telling anyone (old asymmetric fallback).

    Superseded design, part 2: the assumed default used to be the ONLY
    rate a row was checked against, so a genuinely zero-rated row (like
    this one) got false-flagged just because the sheet's assumed rate
    wasn't 0% — confirmed via a real mixed-rate test file
    (sales_register_missing_category.xlsx) that this is a real, not
    hypothetical, false positive. 0% is now always allowed as an
    alternative alongside the assumed default."""
    result = pr.process_period_return([_no_category_column_sales_source()], {"6": "sales"})

    assert any(
        "no tax-rate column found and no default rate was given" in i["message"]
        for i in result["consistency_issues"]
    )
    # Genuinely zero-rated (0.00) no longer gets falsely flagged.
    assert not any("INV-1" in i["message"] or "Overseas Co." in i["message"] for i in result["consistency_issues"])


def test_explicit_default_rate_answer_is_honored_and_avoids_false_positive():
    """When the user answers the ask-first prompt (e.g. picks "Zero-rated"
    for a source with no category column), that answer is trusted directly
    — no notice, no false-positive mismatch — matching "if the values are
    available then no need to show and no need to ask user" applied to a
    user-supplied answer instead of a data column."""
    result = pr.process_period_return(
        [_no_category_column_sales_source()], {"6": "sales"}, default_rates={"6": "zero_rated"}
    )
    assert result["consistency_issues"] == []


def test_category_column_with_non_rate_content_falls_back_to_ask_first():
    """Regression test for a real gap found via live testing: a "Category"
    column matches _CATEGORY_SYNONYMS by NAME, but real files also use
    "Category" for expense classification ("Hotel", "Vehicle") rather than
    a VAT rate ("Standard 15%") — _category_rate() can't parse those, so
    trusting the column by name alone made the per-row VAT check silently
    check nothing, on every row, with zero notice (worse than a genuinely
    missing column, which at least triggers the ask-first fallback below).
    Confirmed live against purchases_register_full_category.xlsx, where a
    planted SAR 15 error (500.00 @ 15% should be 75.00, register showed
    90.00) went completely uncaught until this fix."""
    source = {
        "source_id": "7", "filename": "purchases.xlsx", "sheet_label": "purchases.xlsx",
        "column_names": ["Ref No", "Vendor", "Description", "Category", "Amount (SAR)", "VAT (SAR)"],
        "preamble": [],
        "rows": [
            {"Ref No": "PUR-901", "Vendor": "V1", "Description": "Business Cards Printing",
             "Category": "Marketing", "Amount (SAR)": 500, "VAT (SAR)": 90},
        ],
        "guessed_role": "purchases",
    }
    result = pr.process_period_return([source], {"7": "purchases"})

    messages = [i["message"] for i in result["consistency_issues"]]
    assert any("'Category' column's values weren't recognized" in m for m in messages)
    assert any("75.00" in m and "90" in m for m in messages)


def test_default_rate_check_allows_zero_percent_alongside_the_assumed_rate_in_a_mixed_sheet():
    """Regression test mirroring the real sales_register_missing_category.xlsx
    file: no category column at all, mostly-standard-rated rows plus two
    genuinely zero-rated exports plus one genuine error, all in the same
    sheet. A single assumed default rate can never be "correct" for every
    row in a mixed sheet — the fix isn't to guess better, it's to also
    accept 0% as always-valid so real zero-rated rows pass while real
    errors (matching neither 0% nor the assumed rate) still get caught."""
    source = {
        "source_id": "10", "filename": "sales.xlsx", "sheet_label": "sales.xlsx",
        "column_names": ["Invoice No", "Customer", "Amount (SAR)", "Output VAT (SAR)"],
        "preamble": [],
        "rows": [
            {"Invoice No": "INV-5101", "Customer": "Local Buyer", "Amount (SAR)": 1000, "Output VAT (SAR)": 150},
            {"Invoice No": "INV-5102", "Customer": "Gulf Exports LLC", "Amount (SAR)": 4000, "Output VAT (SAR)": 0},
            {"Invoice No": "INV-5103", "Customer": "Noor Overseas Trading", "Amount (SAR)": 2500, "Output VAT (SAR)": 0},
            {"Invoice No": "INV-5104", "Customer": "Local Hardware Buyer", "Amount (SAR)": 800, "Output VAT (SAR)": 120},
            {"Invoice No": "INV-5105", "Customer": "Rawabi Contracting Co.", "Amount (SAR)": 600, "Output VAT (SAR)": 200},
        ],
        "guessed_role": "sales",
    }
    result = pr.process_period_return([source], {"10": "sales"}, default_rates={"10": "standard"})

    # No Description-synonym column exists here — _row_label falls back to
    # "Customer" (a party-synonym column), so messages are keyed by
    # customer name, not invoice number.
    messages = [i["message"] for i in result["consistency_issues"]]
    assert not any(
        name in m for m in messages
        for name in ("Local Buyer", "Gulf Exports LLC", "Noor Overseas Trading", "Local Hardware Buyer")
    )
    assert any("Rawabi Contracting Co." in m for m in messages)


def test_default_rate_safety_net_applies_per_row_not_per_sheet():
    """Regression test for a real gap found via live testing: earlier tests
    of the default+0% safety net only ever used sheets that were entirely
    one way or the other (all rows have a category, or none do). A sheet
    with MIXED presence — some rows filled, some blank — is a new shape:
    since category_col is truthy at the source level (some rows do have a
    usable category), a row with a blank cell got _category_rate() ->
    None and then skipped ALL checking, silently, no fallback, no notice —
    unlike a fully-blank sheet, which at least gets the assumed-default
    fallback. Mirrors the real final_sales_register_mixed.xlsx file."""
    source = {
        "source_id": "11", "filename": "sales.xlsx", "sheet_label": "sales.xlsx",
        "column_names": ["Invoice No", "Tax Category", "Amount (SAR)", "Output VAT (SAR)"],
        "preamble": [],
        "rows": [
            {"Invoice No": "INV-1", "Tax Category": "Standard 15%",
             "Amount (SAR)": 1000, "Output VAT (SAR)": 150},  # filled, correct
            {"Invoice No": "INV-2", "Tax Category": None,
             "Amount (SAR)": 2000, "Output VAT (SAR)": 0},  # blank, genuinely zero-rated, correct
            {"Invoice No": "INV-3", "Tax Category": None,
             "Amount (SAR)": 500, "Output VAT (SAR)": 175},  # blank, genuine error (neither 0 nor 75)
        ],
        "guessed_role": "sales",
    }
    result = pr.process_period_return([source], {"11": "sales"})

    messages = [i["message"] for i in result["consistency_issues"]]
    assert not any("INV-1" in m or "INV-2" in m for m in messages)
    assert any("INV-3" in m for m in messages)
    assert any("some rows had no usable value" in m for m in messages)


def test_reclaimability_keyword_matching_also_checks_the_category_column():
    """Regression test for a real gap found via live testing: tier-1/tier-2
    keyword matching only ever scanned description_col text. A real test
    file separates a clean "Category" column ("Restaurant") from a free-
    text "Description" ("Client Business Lunch", no blocked keyword in it)
    — the stated Restaurant category was invisible to the matcher and the
    row fell through to "default: reclaimable" instead of being excluded,
    inflating reclaimable_input_vat by exactly the missed row's VAT."""
    source = {
        "source_id": "8", "filename": "purchases.xlsx", "sheet_label": "purchases.xlsx",
        "column_names": ["Ref No", "Vendor", "Description", "Category", "Amount (SAR)", "VAT (SAR)"],
        "preamble": [],
        "rows": [
            {"Ref No": "PUR-902", "Vendor": "Al Noor Restaurant", "Description": "Client Business Lunch",
             "Category": "Restaurant", "Amount (SAR)": 350, "VAT (SAR)": 52.5},
        ],
        "guessed_role": "purchases",
    }
    result = pr.process_period_return([source], {"8": "purchases"}, default_rates={"8": "standard"})

    row = result["purchase_rows"][0]
    assert row["reclaimable"] is False
    assert row["needs_review"] is False


def test_stated_reclaimability_wins_over_blocked_category():
    """Regression test for a real gap found via live testing (against a
    dedicated conflict_reclaimability_purchases.xlsx fixture): a hotel
    expense (tier-1 blocked category) with the register's own column
    stating bare "Y" used to still get auto-excluded — the stated value
    was never actually being applied, "Y"/"N" (far more common in real
    exports than the full words "Yes"/"No") matched neither
    "yes".startswith nor "no".startswith."""
    source = {
        "source_id": "7", "filename": "purchases.xlsx", "sheet_label": "purchases.xlsx",
        "column_names": ["Ref", "Description", "Amount (SAR)", "VAT (SAR)", "Reclaimable"],
        "preamble": [],
        "rows": [
            {"Ref": "PUR-201", "Description": "Hotel Accommodation - Business Trip",
             "Amount (SAR)": 1000, "VAT (SAR)": 150, "Reclaimable": "Y"},
            {"Ref": "PUR-202", "Description": "Office Stationery",
             "Amount (SAR)": 400, "VAT (SAR)": 60, "Reclaimable": "N"},
            {"Ref": "PUR-203", "Description": "Vehicle Lease Payment",
             "Amount (SAR)": 1500, "VAT (SAR)": 225, "Reclaimable": "Y"},
            {"Ref": "PUR-204", "Description": "Vehicle Insurance Premium",
             "Amount (SAR)": 600, "VAT (SAR)": 90, "Reclaimable": None},
        ],
        "guessed_role": "purchases",
    }
    result = pr.process_period_return([source], {"7": "purchases"})
    by_ref = {row["description"]: row for row in result["purchase_rows"]}

    # Stated "Y" on a tier-1 BLOCKED category (hotel) must win — included.
    hotel = by_ref["Hotel Accommodation - Business Trip"]
    assert hotel["reclaimable"] is True and hotel["needs_review"] is False

    # Stated "N" on an otherwise-fine category must win — excluded.
    supplies = by_ref["Office Stationery"]
    assert supplies["reclaimable"] is False and supplies["needs_review"] is False

    # Stated "Y" on a tier-2 AMBIGUOUS category resolves directly — never
    # reaches the collective human-review bucket at all.
    vehicle = by_ref["Vehicle Lease Payment"]
    assert vehicle["reclaimable"] is True and vehicle["needs_review"] is False

    # Control row: no stated value at all -> still falls through to tier-2,
    # confirming the fix didn't break the "nothing stated" case.
    insurance = by_ref["Vehicle Insurance Premium"]
    assert insurance["needs_review"] is True
    review_ids = {r["item_id"] for r in result["review_items"]}
    assert insurance["item_id"] in review_ids
