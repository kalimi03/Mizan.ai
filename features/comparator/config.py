"""
Mizan.ai — "Document Comparator" (Feature C) config: monthly reconciliation
of a SAP/Odoo export against a second document.

Companion doc: docs/mizan_calculator_comparator_handoff.pdf.
"""

import os
from decimal import Decimal

# Fallback matching thresholds (Pass 2 in matching.py) — the handoff doc
# leaves the exact threshold unspecified ("use reasonable judgment or
# confirm with Mohammed before finalizing"). These are defaults, not a
# validated spec — revisit against real reconciliation data.
DATE_PROXIMITY_DAYS = 3
AMOUNT_TOLERANCE = Decimal("0.01")

# Pass 4 (group/consolidation matching, matching.py) — how many leftover
# items on one side are allowed to be bundled together when checking
# whether they sum to a single leftover item on the other side (e.g. a
# vendor billing several of our purchase orders as one consolidated
# invoice). Bounded deliberately small: a big combination happening to
# sum to the right number by pure chance is far likelier than a small one,
# and this only ever runs against items that already share a real
# connecting signal (date proximity, description overlap, or a sequential
# reference number) with the target row, not a blind search over every
# leftover item.
GROUP_MATCH_MIN_SIZE = 2
GROUP_MATCH_MAX_SIZE = 4

# Pass 5 (matching.py's find_leftover_sum_matches, "Check for more possible
# groupings" — manually triggered, only run after a full human review pass
# on Passes 1-4's own findings, never automatically). Unlike Pass 4, this
# deliberately does NOT require a connecting signal — it exists specifically
# for real installment/partial-payment cases with no shared date/
# description/reference (e.g. generic "Payment 1/2/3" descriptions and
# non-sequential bank transaction IDs), which Pass 4 verifiably can't catch.
# Bounded instead by: only ever running on the already-thinned leftover pool
# (see build_leftover_pool), the same GROUP_MATCH_MIN/MAX_SIZE cap as Pass 4,
# and this wide-but-finite date ceiling between a target and any candidate —
# generous enough for realistic installment schedules, still finite enough
# to rule out combining transactions from obviously unrelated periods.
SUM_MATCH_MAX_DATE_SPREAD_DAYS = 90

# VAT-inclusive/exclusive mismatch diagnostic (matching.py's amount_mismatch
# detection, Pass 1) — Saudi VAT is exactly 15%, so a genuine
# inclusive-vs-exclusive recording difference should land very close to
# that ratio; the +/-0.5 percentage point band only absorbs line-item
# rounding noise, not genuine ambiguity, so a random unrelated discrepancy
# is unlikely to land inside it by chance. Diagnostic only — this never
# changes whether a pair counts as a match, which AMOUNT_TOLERANCE alone
# already decided before this is ever checked; it only relabels an
# already-flagged mismatch with a more specific, actionable hint.
VAT_GAP_RATIO = Decimal("0.15")
VAT_GAP_TOLERANCE = Decimal("0.005")

# Triple-fallback env var convention matching features/calculator/config.py.
QWEN_BRAIN_URL = (
    os.getenv("MIZAN_QWEN_BRAIN_URL")
    or os.getenv("QWEN_BRAIN_URL")
    or os.getenv("MODAL_QWEN_BRAIN_URL")
)

# Translator service's internal, unauthenticated endpoint — same pattern as
# features/calculator/config.py's TRANSLATOR_INTERNAL_URL.
TRANSLATOR_INTERNAL_URL = (
    os.getenv("MIZAN_TRANSLATOR_INTERNAL_URL")
    or os.getenv("TRANSLATOR_INTERNAL_URL")
)

# doc-extraction service — same pattern as features/calculator's use
# (MIZAN_DOC_EXTRACTION_URL, wired but never actually exercised for
# Calculator since its own upload/extraction endpoints are still mocked;
# this is the first real caller of doc-extraction in the repo).
DOC_EXTRACTION_URL = (
    os.getenv("MIZAN_DOC_EXTRACTION_URL")
    or os.getenv("DOC_EXTRACTION_URL")
)
