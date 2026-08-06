"""
Mizan.ai — VAT Compliance Center, job 2: period VAT return preparation.

Turns a period's sales and purchase registers into Output VAT / Reclaimable
Input VAT / Net position — the aggregate figure that goes into an actual
ZATCA VAT return, as opposed to job 1 (invoice_check.py), which checks one
invoice at a time. Per the approved design: job 2 leans on job 1 wherever
possible (skips its own structural checks when the input already went
through job 1) and otherwise runs a lightweight consistency check of its
own on raw register data.

Three-step flow, mirroring the fact this needs two human decision points
(confirm which uploaded sheet/file is which, then resolve any genuinely
ambiguous reclaimability calls) rather than job 1's fully automatic batch:
  1. extract every uploaded file/sheet, guess sales vs. purchases per
     source (never trusted silently — always shown for confirmation).
  2. once the user confirms/corrects the mapping, process every row:
     recompute-and-compare where possible, resolve reclaimable-vs-blocked
     per purchase row, collect the genuinely ambiguous ones for one
     collective human review rather than deciding them individually.
  3. once any review decisions are in, finalize the aggregate figures.

Column identification is a small, self-contained word-boundary matcher
(deliberately not features/comparator/normalize.py's _find_column, which
is hardcoded to Comparator's own reference/date/amount/debit/credit field
set — duplicating ~15 lines here was judged lower-risk than reaching into
an already-shipped, unrelated feature's tested code for a different
field set entirely).
"""

import logging
import os
import re
import shutil
import tempfile
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from features.calculator.config import (
    AMBIGUOUS_INPUT_VAT_CATEGORIES,
    BLOCKED_INPUT_VAT_CATEGORIES,
    DOC_EXTRACTION_URL,
    TaxCategory,
    VAT_RATES,
)
from features.calculator.engine import round_currency
from features.common.http_client import InternalServiceError, post_file

logger = logging.getLogger(__name__)


class PeriodReturnError(ValueError):
    """Raised for genuine input problems (unsupported file type, extraction
    failures, etc.) — the caller should turn this into a 422."""


# ---------------------------------------------------------------------------
# Column identification — self-contained, see module docstring
# ---------------------------------------------------------------------------

_AMOUNT_SYNONYMS = ["amount", "taxable amount", "net amount", "value"]
_VAT_SYNONYMS = ["vat", "vat amount", "tax amount", "output vat", "input vat"]
_CATEGORY_SYNONYMS = ["tax category", "vat category", "category", "rate", "vat rate"]
_RECLAIMABLE_SYNONYMS = ["reclaimable", "recoverable", "input vat recoverable"]
# Tried in this order — expense-type/description text is what actually
# describes the purchase (needed for tier-1/tier-2 keyword matching);
# vendor/customer name is only a fallback for row-labeling purposes, since
# a vendor's business name ("Restaurant XYZ") won't itself contain a
# category keyword ("entertainment") the way "Expense Type" would.
_EXPENSE_TYPE_SYNONYMS = ["expense type", "description", "particulars", "narration", "item"]
_PARTY_SYNONYMS = ["vendor", "supplier", "customer", "counterparty"]

_SALES_KEYWORDS = ["sales", "output", "revenue"]
_PURCHASE_KEYWORDS = ["purchase", "input", "expense", "vendor", "supplier"]


def _normalize_header(header: Any) -> str:
    return re.sub(r"\s+", " ", str(header).strip().lower()).strip(".:")


def _find_col(column_names: List[str], synonyms: List[str]) -> Optional[str]:
    normalized = {_normalize_header(c): c for c in column_names}
    for syn in synonyms:
        if syn in normalized:
            return normalized[syn]
    for norm_header, original in normalized.items():
        if any(re.search(rf"\b{re.escape(syn)}\b", norm_header) for syn in synonyms):
            return original
    return None


def _find_description_col(column_names: List[str]) -> Optional[str]:
    return _find_col(column_names, _EXPENSE_TYPE_SYNONYMS) or _find_col(column_names, _PARTY_SYNONYMS)


def _parse_amount(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, float) and value != value:  # NaN
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    text = str(value).strip()
    if not text:
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", text)
    if not cleaned or cleaned in ("-", "."):
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _category_rate(text: Any) -> Optional[Decimal]:
    """Maps a register's own free-text tax-category cell (e.g. "Standard
    15%", "Zero-rated (Export)", "Exempt") onto a rate — used only for the
    row-level consistency check, never to override the register's own
    printed VAT amount. Unrecognized text returns None (skip the check for
    that row) rather than guessing."""
    if not text:
        return None
    t = str(text).lower()
    if "zero" in t or "0%" in t:
        return Decimal("0.00")
    if "exempt" in t:
        return Decimal("0.00")
    if "standard" in t or "15%" in t:
        return Decimal("0.15")
    return None


def _is_total_row(row: dict) -> bool:
    """Real registers commonly carry their own "TOTAL ..." summary row
    baked in as a data row (confirmed against a real test workbook) — must
    be excluded from per-row aggregation (or every total gets double-
    counted) and is exactly what the declared-total consistency check
    below compares against."""
    return any("total" in str(v).lower() for v in row.values() if v is not None)


def _category_col_is_usable(rows: List[dict], category_col: Optional[str]) -> bool:
    """A column matching _CATEGORY_SYNONYMS by name (e.g. "Category") isn't
    always a VAT-rate column — real registers also use "Category" for
    expense/purchase classification (e.g. "Hotel", "Vehicle"), which
    _category_rate() won't recognize as a rate at all. Confirmed via a real
    test file: trusting the column by name alone made the per-row VAT
    check silently check nothing, on every row, with no notice — worse
    than a genuinely missing column, which at least triggers the
    default_rates ask-first fallback. Only trust the column if at least
    one row's value actually maps to a known rate."""
    if category_col is None:
        return False
    return any(
        _category_rate(row.get(category_col)) is not None
        for row in rows if not _is_total_row(row)
    )


def _row_label(row: dict, description_col: Optional[str]) -> str:
    if description_col and row.get(description_col) not in (None, ""):
        return str(row[description_col])
    for v in row.values():
        if v not in (None, ""):
            return str(v)
    return "row"


def _guess_role(filename: str, preamble_lines: List[str], column_names: List[str]) -> str:
    """Never trusted silently — always returned for the user to confirm or
    correct (see services/calculator/main.py's confirm-mapping endpoint).
    A reclaimable-type column is the strongest signal (that concept only
    exists on the purchases side); filename/title text is the fallback."""
    if _find_col(column_names, _RECLAIMABLE_SYNONYMS):
        return "purchases"
    text = (filename + " " + " ".join(preamble_lines)).lower()
    if any(kw in text for kw in _PURCHASE_KEYWORDS):
        return "purchases"
    if any(kw in text for kw in _SALES_KEYWORDS):
        return "sales"
    return "unknown"


_PREAMBLE_NAME_SEPARATOR = re.compile(r"\s[—–-]\s")


def guess_business_name(sources: List[Dict[str, Any]]) -> Optional[str]:
    """Never trusted silently — returned as an editable, pre-filled
    suggestion for the report step's "Business / client name" field, same
    principle as _guess_role for the mapping step. Real registers commonly
    open with "{Business Name} — {register type}" as their first preamble
    line (confirmed across every real test file used this session) —
    parsed here as a best-effort starting point, not applied to any
    stored data on its own."""
    for source in sources:
        preamble = source.get("preamble") or []
        if not preamble:
            continue
        first_line = str(preamble[0]).strip()
        parts = _PREAMBLE_NAME_SEPARATOR.split(first_line, maxsplit=1)
        if len(parts) == 2 and parts[0].strip():
            return parts[0].strip()
    return None


# ---------------------------------------------------------------------------
# Step 1 — extract + guess roles
# ---------------------------------------------------------------------------


def extract_sources(files: List[Tuple[str, bytes]]) -> List[Dict[str, Any]]:
    """One "source" per sheet (xlsx) or per file (csv) — a multi-sheet
    workbook and several separate single-sheet files are handled
    identically from here on, per the approved design.

    Calls doc-extraction over HTTP (same pattern as invoice_check.py's
    _extract_one()) rather than importing
    features/common/document_extraction.py's xlsx/csv parsing directly —
    that path needs pandas, which only doc-extraction's own container has
    installed; Calculator is deliberately kept lightweight, and this
    reuses the extraction *service*, not its heavy in-process deps."""
    if not DOC_EXTRACTION_URL:
        raise PeriodReturnError("MIZAN_DOC_EXTRACTION_URL is not configured")

    sources: List[Dict[str, Any]] = []
    for filename, content in files:
        ext = os.path.splitext(filename)[1].lower()
        if ext not in (".xlsx", ".csv"):
            raise PeriodReturnError(f"{filename}: only .xlsx or .csv files are supported here.")

        upload_dir = tempfile.mkdtemp(prefix="mizan_period_return_")
        try:
            path = os.path.join(upload_dir, filename)
            with open(path, "wb") as f:
                f.write(content)
            try:
                extraction = post_file(f"{DOC_EXTRACTION_URL}/extract", path, filename, timeout=120)
            except InternalServiceError as exc:
                raise PeriodReturnError(f"{filename}: {exc}")
        finally:
            shutil.rmtree(upload_dir, ignore_errors=True)

        tables = extraction.get("tables") or []
        for idx, table in enumerate(tables):
            sheet_label = filename if len(tables) == 1 else f"{filename} (sheet {idx + 1} of {len(tables)})"
            column_names = table.get("column_names", [])
            preamble = table.get("preamble", [])
            sources.append({
                "source_id": str(len(sources)),
                "filename": filename,
                "sheet_label": sheet_label,
                "column_names": column_names,
                "preamble": preamble,
                "rows": table.get("rows", []),
                "guessed_role": _guess_role(filename, preamble, column_names),
                # Independent of role — a sheet either has a recognizable
                # tax-category/rate column or it doesn't. Exposed upfront
                # so the confirm-mapping step can ask the user for a
                # default rate right when it's needed, rather than
                # guessing silently (purchases used to) or skipping the
                # check entirely (sales used to) — see
                # process_period_return()'s default_rates parameter.
                "has_category_column": _category_col_is_usable(
                    table.get("rows", []), _find_col(column_names, _CATEGORY_SYNONYMS)
                ),
            })
    return sources


# ---------------------------------------------------------------------------
# Step 2 — process every row under the confirmed mapping
# ---------------------------------------------------------------------------


_RECLAIMABLE_TRUE_TOKENS = {"y", "yes", "true"}
_RECLAIMABLE_FALSE_TOKENS = {"n", "no", "false"}


def _resolve_reclaimability(
    row: dict, reclaimable_col: Optional[str], description_col: Optional[str], item_index: int,
    category_col: Optional[str] = None,
) -> Dict[str, Any]:
    item_id = f"pr_{item_index}"

    if reclaimable_col:
        raw = str(row.get(reclaimable_col) or "").strip()
        if raw:
            lower = raw.lower()
            # First WORD only, not startswith() on the whole string — found
            # via live testing that a real register using bare "Y"/"N"
            # (extremely common, more so than the full words "Yes"/"No")
            # matched neither "yes".startswith nor "no".startswith, so the
            # stated value was silently ignored for every "Y"/"N" cell and
            # fell straight through to the tier-1/tier-2 category rules —
            # i.e. the source's own stated reclaimability was never
            # actually being applied at all, only ever coincidentally
            # agreeing or disagreeing with the category guess.
            first_word = lower.split()[0].rstrip(".,;:") if lower.split() else ""
            if first_word in _RECLAIMABLE_TRUE_TOKENS:
                return {"item_id": item_id, "reclaimable": True, "reason": f"Source states: {raw!r}.", "needs_review": False}
            if first_word in _RECLAIMABLE_FALSE_TOKENS or "n/a" in lower or "exempt" in lower:
                return {"item_id": item_id, "reclaimable": False, "reason": f"Source states: {raw!r}.", "needs_review": False}

    # Real files sometimes carry a separate classification column ("Category":
    # "Hotel", "Vehicle") distinct from a free-text description ("Client
    # Business Lunch") that doesn't itself contain the tell-tale keyword —
    # confirmed via a real test file where checking description text alone
    # missed a plainly-stated "Restaurant" category entirely. Scan both.
    text = " ".join(
        str(row.get(col) or "") for col in (description_col, category_col) if col
    ).lower()
    for kw in BLOCKED_INPUT_VAT_CATEGORIES:
        if kw in text:
            return {"item_id": item_id, "reclaimable": False, "reason": f"Blocked category ({kw!r}).", "needs_review": False}
    for kw in AMBIGUOUS_INPUT_VAT_CATEGORIES:
        if kw in text:
            return {"item_id": item_id, "reclaimable": True, "reason": f"Possibly not reclaimable ({kw!r}) — needs review.", "needs_review": True}

    return {"item_id": item_id, "reclaimable": True, "reason": "Default: reclaimable.", "needs_review": False}


_DEFAULT_RATE_KEY_TO_DECIMAL = {
    "standard": VAT_RATES[TaxCategory.standard],
    "zero_rated": Decimal("0.00"),
    "exempt": Decimal("0.00"),
}


def process_period_return(
    sources: List[Dict[str, Any]], mapping: Dict[str, str], default_rates: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """mapping: {source_id: "sales" | "purchases" | "ignore"}, the user-
    confirmed roles from step 1. default_rates: {source_id: "standard" |
    "zero_rated" | "exempt"} — only meaningful for a source that has no
    tax-category column of its own (see extract_sources()'s
    has_category_column); the user is asked for this explicitly in the
    confirm-mapping step rather than it being guessed silently. A source
    left unanswered falls back to the standard rate and says so in
    consistency_issues — never silent, and (deliberately, per the
    approved design) applied the same way to sales and purchases alike,
    not one-sided.

    Returns headline figures (reclaimable input VAT excludes any still-
    pending tier-2 items — see apply_review_decisions), consistency
    issues, and review items."""
    default_rates = default_rates or {}
    output_vat = Decimal("0")
    input_vat_total = Decimal("0")
    reclaimable_input_vat = Decimal("0")
    consistency_issues: List[Dict[str, Any]] = []
    purchase_rows: List[Dict[str, Any]] = []
    sales_row_count = 0
    purchase_row_count = 0

    for source in sources:
        role = mapping.get(source["source_id"], source.get("guessed_role"))
        if role not in ("sales", "purchases"):
            continue

        column_names = source["column_names"]
        amount_col = _find_col(column_names, _AMOUNT_SYNONYMS)
        vat_col = _find_col(column_names, _VAT_SYNONYMS)
        # raw_category_col is whatever column matches the name synonyms
        # ("Category", "Tax Category", ...), used below as extra keyword
        # text for reclaimability tier-1/tier-2 matching regardless of its
        # content. category_col is the same column but only kept for
        # VAT-rate purposes if its values actually look like rate labels
        # ("Standard 15%") — a "Category" column can just as easily hold
        # expense-classification text ("Hotel", "Vehicle") instead, which
        # _category_rate() won't recognize; using it for rate lookups
        # anyway would silently check nothing on every row.
        raw_category_col = _find_col(column_names, _CATEGORY_SYNONYMS)
        category_col = raw_category_col if _category_col_is_usable(source["rows"], raw_category_col) else None
        reclaimable_col = _find_col(column_names, _RECLAIMABLE_SYNONYMS)
        description_col = _find_description_col(column_names)

        if vat_col is None:
            consistency_issues.append({
                "source": source["sheet_label"], "severity": "warning",
                "message": f"Could not find a VAT/tax amount column in {source['sheet_label']!r} — this source was skipped.",
            })
            continue

        # Resolved once per source, not per row, but usable per row: even
        # a sheet with a perfectly good category column can have some rows
        # with a blank cell (confirmed via a real mixed-presence test file
        # — earlier tests only ever saw a sheet as entirely one way or the
        # other). Whatever rate the user specified for this source in
        # confirm-mapping (or Standard 15% if left unanswered) is kept
        # ready as a per-row fallback regardless of whether category_col
        # exists at all — used below for any row whose own rate can't be
        # resolved, not just when the whole sheet lacks a category column.
        chosen = default_rates.get(source["source_id"])
        if chosen in _DEFAULT_RATE_KEY_TO_DECIMAL:
            source_default_rate = _DEFAULT_RATE_KEY_TO_DECIMAL[chosen]
            default_was_unanswered = False
        else:
            source_default_rate = VAT_RATES[TaxCategory.standard]
            default_was_unanswered = True
        used_unanswered_default = False  # only set True once the fallback is actually used by some row

        data_rows = [r for r in source["rows"] if not _is_total_row(r)]
        total_rows = [r for r in source["rows"] if _is_total_row(r)]
        row_vat_sum = Decimal("0")
        # Tracked separately so a total row that specifically says
        # "reclaimable" (real registers commonly print both a raw total
        # AND a reclaimable-only total as two separate rows — confirmed
        # against a real test workbook) gets compared against the right
        # figure instead of being flagged as "inconsistent" against the
        # raw sum it was never meant to equal.
        source_reclaimable_sum = Decimal("0")

        for row in data_rows:
            vat_amount = _parse_amount(row.get(vat_col))
            if vat_amount is None:
                continue
            row_vat_sum += vat_amount

            if amount_col:
                amount = _parse_amount(row.get(amount_col))
                # Resolved per row, not per sheet: a sheet can have a good
                # category column overall and still have individual rows
                # with a blank/unrecognized cell (confirmed via a real
                # mixed-presence test file) — those rows must still get
                # the same fallback safety net as a sheet with no category
                # column at all, not silently skip every check.
                row_rate = _category_rate(row.get(category_col)) if category_col else None
                if row_rate is not None:
                    # This row declares its own rate explicitly — no
                    # ambiguity, check against exactly that.
                    if amount is not None:
                        expected = round_currency(amount * row_rate)
                        if abs(expected - vat_amount) > Decimal("0.01"):
                            consistency_issues.append({
                                "source": source["sheet_label"], "severity": "warning",
                                "message": f"{_row_label(row, description_col)}: recalculated VAT is {expected}, register shows {vat_amount}.",
                            })
                elif amount is not None:
                    # No usable rate for THIS row. Confirmed via a real
                    # mixed-rate test file that checking against the
                    # assumed default alone false-flags genuinely
                    # zero-rated rows (exports, etc.): 0% is always a
                    # legitimate rate regardless of what's assumed, so
                    # only flag if VAT matches NEITHER the assumed default
                    # NOR 0%.
                    if default_was_unanswered:
                        used_unanswered_default = True
                    zero_rate = Decimal("0.00")
                    expecteds = sorted({round_currency(amount * r) for r in {source_default_rate, zero_rate}})
                    if all(abs(e - vat_amount) > Decimal("0.01") for e in expecteds):
                        expected_text = " or ".join(str(e) for e in expecteds)
                        consistency_issues.append({
                            "source": source["sheet_label"], "severity": "warning",
                            "message": f"{_row_label(row, description_col)}: recalculated VAT is {expected_text} "
                                       f"(no per-row rate available — checked against the assumed default and 0%), "
                                       f"register shows {vat_amount}.",
                        })

            if role == "sales":
                output_vat += vat_amount
                sales_row_count += 1
            else:
                input_vat_total += vat_amount
                purchase_row_count += 1
                resolution = _resolve_reclaimability(
                    row, reclaimable_col, description_col, len(purchase_rows), category_col=raw_category_col
                )
                purchase_row = {
                    **resolution, "source": source["sheet_label"],
                    "description": _row_label(row, description_col), "vat_amount": str(vat_amount),
                }
                purchase_rows.append(purchase_row)
                if not resolution["needs_review"] and resolution["reclaimable"]:
                    reclaimable_input_vat += vat_amount
                    source_reclaimable_sum += vat_amount

        if used_unanswered_default:
            if category_col:
                reason = f"some rows had no usable value in the {raw_category_col!r} column"
            elif raw_category_col:
                reason = f"the {raw_category_col!r} column's values weren't recognized VAT-rate labels"
            else:
                reason = "no tax-rate column found"
            consistency_issues.append({
                "source": source["sheet_label"], "severity": "warning",
                "message": f"{source['sheet_label']}: {reason} and no default rate was given — "
                           "assumed Standard 15% for this sheet's VAT check.",
            })

        for total_row in total_rows:
            declared = _parse_amount(total_row.get(vat_col))
            if declared is None:
                continue
            label_text = " ".join(str(v) for v in total_row.values() if v).lower()
            is_reclaimable_total = role == "purchases" and "reclaimable" in label_text
            compare_to = source_reclaimable_sum if is_reclaimable_total else row_vat_sum
            if abs(declared - compare_to) > Decimal("0.01"):
                what = "reclaimable total" if is_reclaimable_total else "total"
                consistency_issues.append({
                    "source": source["sheet_label"], "severity": "warning",
                    "message": f"{source['sheet_label']}: the register's own {what} ({declared}) doesn't match what was computed from its rows ({compare_to}).",
                })

    review_items = [r for r in purchase_rows if r["needs_review"]]
    status = "awaiting_review" if review_items else "ready"

    return {
        "status": status,
        "output_vat": str(output_vat),
        "input_vat_total": str(input_vat_total),
        "reclaimable_input_vat": str(reclaimable_input_vat),
        "net_position": str(output_vat - reclaimable_input_vat),
        "consistency_issues": consistency_issues,
        "review_items": review_items,
        "purchase_rows": purchase_rows,
        "sales_row_count": sales_row_count,
        "purchase_row_count": purchase_row_count,
    }


# ---------------------------------------------------------------------------
# Step 3 — apply the human's tier-2 decisions and finalize
# ---------------------------------------------------------------------------


def apply_review_decisions(result: Dict[str, Any], decisions: Dict[str, bool]) -> Dict[str, Any]:
    """decisions: {item_id: reclaimable}. Anything left undecided defaults
    to NOT reclaimable — the safer failure mode (understating a refund is
    a missed opportunity; overstating one is a compliance risk in an
    audit), same reasoning already applied throughout this feature."""
    output_vat = Decimal(result["output_vat"])
    reclaimable_input_vat = Decimal("0")
    resolved_rows = []

    for row in result["purchase_rows"]:
        reclaimable = decisions.get(row["item_id"], False) if row["needs_review"] else row["reclaimable"]
        resolved_rows.append({**row, "reclaimable": reclaimable, "needs_review": False})
        if reclaimable:
            reclaimable_input_vat += Decimal(row["vat_amount"])

    return {
        **result,
        "status": "ready",
        "reclaimable_input_vat": str(reclaimable_input_vat),
        "net_position": str(output_vat - reclaimable_input_vat),
        "purchase_rows": resolved_rows,
        "review_items": [],
    }
