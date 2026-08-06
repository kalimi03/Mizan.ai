"""
Mizan.ai — Comparator service (Feature C, monthly reconciliation).

Owns the SAP/Odoo-export-vs-second-document reconciliation pipeline:
extract (via a real HTTP call to doc-extraction) -> normalize -> match ->
HITL review -> report. No MCP server, no LangGraph graph — see
features/comparator/config.py's module docstring for why: matching is
fully deterministic, and the only optional model call (exception
narration) is a single plain completion, not a tool-calling decision.

Multi-step flow (mirrors Calculator's preview -> confirm -> report shape,
same "no filing-id-keyed store yet, body carries data directly between
calls" pattern):
  1. POST /api/comparator/reconcile -> upload both files, get back
     {matched, needs_review}. Preview only, callable repeatedly.
  2. POST /api/comparator/confirm   -> the HITL "Confirm & Generate"
     action — client echoes back matched + needs_review (now annotated
     with each item's decision/explanation), gets the finalized result
     plus an optional QwenBrain exception summary.
  2.5. POST /api/comparator/reconcile-leftovers (optional, manually
     triggered via the frontend's "Check for more possible groupings"
     button only — never automatic) -> a signal-free sum-combination
     search (Pass 5) over whatever step 2 left dismissed/unresolved, for
     partial/installment payments Pass 4's signal-gated search can't
     catch. Returns more review items to run back through step 2.
  3. POST /api/comparator/report    -> renders the finalized result as a
     downloadable PDF/DOCX/XLSX.
"""

import logging
import os
import shutil
import tempfile
from collections import defaultdict
from datetime import date as date_type
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional, Tuple

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from features.common.cors import configure_cors
from features.common.http_client import InternalServiceError, post_file
from features.comparator.config import (
    AMOUNT_TOLERANCE, DATE_PROXIMITY_DAYS, DOC_EXTRACTION_URL, SUM_MATCH_MAX_DATE_SPREAD_DAYS,
)
from features.comparator.matching import build_leftover_pool, drop_stale_unmatched_items, find_leftover_sum_matches
from features.comparator.matching import reconcile as run_reconcile
from features.comparator.models import ColumnMapping, MatchedPair, ReconciliationResult, ReconciliationRow, ReviewItem
from features.comparator.narration import summarize_exceptions
from features.comparator.normalize import NormalizationError, normalize_extraction
from features.comparator.report import generate_reconciliation_report
from app.auth import get_current_user_id_full_access

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Mizan.ai — Comparator",
    description="Monthly reconciliation: SAP/Odoo export vs. a second document (Feature C).",
    version="0.1.0",
)
configure_cors(app)


@app.get("/health")
def health():
    return {"status": "ok", "version": app.version}


class Language(str, Enum):
    ar = "ar"
    en = "en"


class ReportFormat(str, Enum):
    pdf = "pdf"
    docx = "docx"
    xlsx = "xlsx"


# ---------------------------------------------------------------------------
# API <-> dataclass conversion
# ---------------------------------------------------------------------------


class ReconciliationRowModel(BaseModel):
    row_id: str
    source: str
    reference: Optional[str] = None
    date: Optional[date_type] = None
    amount: Optional[float] = None
    description: Optional[str] = None


class ColumnMappingModel(BaseModel):
    reference_column: Optional[str] = None
    date_column: Optional[str] = None
    amount_column: Optional[str] = None
    debit_column: Optional[str] = None
    credit_column: Optional[str] = None
    description_column: Optional[str] = None


def _mapping_to_model(mapping: ColumnMapping) -> ColumnMappingModel:
    return ColumnMappingModel(
        reference_column=mapping.reference_column, date_column=mapping.date_column,
        amount_column=mapping.amount_column, debit_column=mapping.debit_column,
        credit_column=mapping.credit_column, description_column=mapping.description_column,
    )


class MatchedPairModel(BaseModel):
    row_a: ReconciliationRowModel
    row_b: ReconciliationRowModel
    match_type: str


class ReviewItemModel(BaseModel):
    item_id: str
    kind: str
    row_a: Optional[ReconciliationRowModel] = None
    row_b: Optional[ReconciliationRowModel] = None
    candidates: List[ReconciliationRowModel] = Field(default_factory=list)
    # For kind="group_match": the several-item side (opposite of whichever
    # of row_a/row_b is set) whose amounts sum to that single row.
    group: List[ReconciliationRowModel] = Field(default_factory=list)
    # For kind="amount_mismatch": diagnostic hint, see models.ReviewItem.
    possible_vat_gap: bool = False
    decision: Optional[str] = None
    explanation: Optional[str] = None
    selected_candidate_id: Optional[str] = None


def _row_to_model(row: ReconciliationRow) -> ReconciliationRowModel:
    return ReconciliationRowModel(
        row_id=row.row_id, source=row.source, reference=row.reference, date=row.date,
        amount=float(row.amount) if row.amount is not None else None, description=row.description,
    )


def _row_from_model(m: ReconciliationRowModel) -> ReconciliationRow:
    return ReconciliationRow(
        row_id=m.row_id, source=m.source, reference=m.reference, date=m.date,
        amount=Decimal(str(m.amount)) if m.amount is not None else None, description=m.description,
    )


def _matched_to_model(pair: MatchedPair) -> MatchedPairModel:
    return MatchedPairModel(row_a=_row_to_model(pair.row_a), row_b=_row_to_model(pair.row_b), match_type=pair.match_type)


def _matched_from_model(m: MatchedPairModel) -> MatchedPair:
    return MatchedPair(row_a=_row_from_model(m.row_a), row_b=_row_from_model(m.row_b), match_type=m.match_type)


def _review_to_model(item: ReviewItem) -> ReviewItemModel:
    return ReviewItemModel(
        item_id=item.item_id, kind=item.kind,
        row_a=_row_to_model(item.row_a) if item.row_a else None,
        row_b=_row_to_model(item.row_b) if item.row_b else None,
        candidates=[_row_to_model(c) for c in item.candidates],
        group=[_row_to_model(c) for c in item.group],
        possible_vat_gap=item.possible_vat_gap,
        decision=item.decision, explanation=item.explanation,
        selected_candidate_id=item.selected_candidate_id,
    )


def _review_from_model(m: ReviewItemModel) -> ReviewItem:
    return ReviewItem(
        item_id=m.item_id, kind=m.kind,
        row_a=_row_from_model(m.row_a) if m.row_a else None,
        row_b=_row_from_model(m.row_b) if m.row_b else None,
        candidates=[_row_from_model(c) for c in m.candidates],
        group=[_row_from_model(c) for c in m.group],
        possible_vat_gap=m.possible_vat_gap,
        decision=m.decision, explanation=m.explanation,
        selected_candidate_id=m.selected_candidate_id,
    )


# ---------------------------------------------------------------------------
# Step 1 — POST /reconcile
# ---------------------------------------------------------------------------


class ReconcilePreviewResponse(BaseModel):
    matched: List[MatchedPairModel]
    needs_review: List[ReviewItemModel]
    # Effective thresholds actually used for this match, echoed back so a
    # review screen can display them and let the user re-run with
    # different values — these are reasonable defaults (see
    # features/comparator/config.py), not a validated spec.
    date_proximity_days: int
    amount_tolerance: float
    # Which raw column was guessed for which field, per uploaded file — a
    # best-effort heuristic (see features/comparator/normalize.py),
    # surfaced so the user can review/approve it rather than trusting it
    # silently.
    sap_export_column_mapping: ColumnMappingModel
    second_document_column_mapping: ColumnMappingModel


def _extract_and_normalize(file: UploadFile, source: str) -> Tuple[List[ReconciliationRow], ColumnMapping]:
    if not DOC_EXTRACTION_URL:
        raise HTTPException(status_code=500, detail="MIZAN_DOC_EXTRACTION_URL is not configured")

    filename = file.filename or "upload"
    upload_dir = tempfile.mkdtemp(prefix="mizan_comparator_upload_")
    input_path = os.path.join(upload_dir, filename)
    try:
        with open(input_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        try:
            extraction = post_file(f"{DOC_EXTRACTION_URL}/extract", input_path, filename, timeout=120)
        except InternalServiceError as exc:
            # A 4xx from doc-extraction means it looked at the file and
            # rejected it (unsupported format, scanned/photographed PDF,
            # etc.) — that's the caller's input, not our service being
            # down, so surface it as our own 422 with the real reason
            # rather than a generic 502.
            if exc.status_code and 400 <= exc.status_code < 500:
                raise HTTPException(status_code=422, detail=f"{filename}: {exc}")
            raise HTTPException(status_code=502, detail=f"Extraction failed for {filename}: {exc}")

        try:
            return normalize_extraction(extraction, source)
        except NormalizationError as exc:
            raise HTTPException(status_code=422, detail=f"{filename}: {exc}")
    finally:
        shutil.rmtree(upload_dir, ignore_errors=True)


@app.post("/api/comparator/reconcile", response_model=ReconcilePreviewResponse, tags=["C — comparator"])
async def reconcile_documents(
    sap_export: UploadFile = File(...),
    second_document: UploadFile = File(...),
    date_proximity_days: Optional[int] = Form(
        None, description="Days apart still considered a possible match during fuzzy matching. Defaults to a repo-wide setting if not provided."
    ),
    amount_tolerance: Optional[float] = Form(
        None, description="Amount difference still considered a possible match. Defaults to a repo-wide setting if not provided."
    ),
    user_id: str = Depends(get_current_user_id_full_access),
):
    """Step 1 — extract, normalize, and match both uploaded documents.
    Preview only, callable repeatedly — e.g. after fixing a bad upload, or
    adjusting date_proximity_days/amount_tolerance to see whether a
    borderline pair gets picked up. The effective values actually used are
    echoed back in the response, for a future review screen to surface and
    let the user edit rather than leaving them as an invisible constant."""
    if date_proximity_days is not None and date_proximity_days < 0:
        raise HTTPException(status_code=422, detail="date_proximity_days must be >= 0")
    if amount_tolerance is not None and amount_tolerance < 0:
        raise HTTPException(status_code=422, detail="amount_tolerance must be >= 0")

    effective_date_proximity_days = date_proximity_days if date_proximity_days is not None else DATE_PROXIMITY_DAYS
    effective_amount_tolerance = Decimal(str(amount_tolerance)) if amount_tolerance is not None else AMOUNT_TOLERANCE

    rows_a, mapping_a = _extract_and_normalize(sap_export, "sap_odoo")
    rows_b, mapping_b = _extract_and_normalize(second_document, "second_doc")

    result = run_reconcile(
        rows_a, rows_b,
        date_proximity_days=effective_date_proximity_days,
        amount_tolerance=effective_amount_tolerance,
    )
    return ReconcilePreviewResponse(
        matched=[_matched_to_model(m) for m in result.matched],
        needs_review=[_review_to_model(r) for r in result.needs_review],
        date_proximity_days=effective_date_proximity_days,
        amount_tolerance=float(effective_amount_tolerance),
        sap_export_column_mapping=_mapping_to_model(mapping_a),
        second_document_column_mapping=_mapping_to_model(mapping_b),
    )


# ---------------------------------------------------------------------------
# Step 2 — POST /confirm
# ---------------------------------------------------------------------------


class ConfirmRequest(BaseModel):
    matched: List[MatchedPairModel] = Field(default_factory=list)
    reviewed: List[ReviewItemModel] = Field(default_factory=list)
    language: Language = Language.en


class ConfirmResponse(BaseModel):
    matched: List[MatchedPairModel]
    reviewed: List[ReviewItemModel]
    narration: Optional[str] = None


@app.post("/api/comparator/confirm", response_model=ConfirmResponse, tags=["C — comparator"])
def confirm_reconciliation(req: ConfirmRequest, user_id: str = Depends(get_current_user_id_full_access)):
    """Step 2 — the single HITL 'Confirm & Generate' action. Validates
    that every confirmed ambiguous item actually picked one of its own
    candidates, then optionally summarizes the exceptions via QwenBrain
    (fails open to no summary)."""
    matched = [_matched_from_model(m) for m in req.matched]
    reviewed = [_review_from_model(r) for r in req.reviewed]

    for item in reviewed:
        if item.kind == "ambiguous" and item.decision == "confirmed":
            valid_ids = {c.row_id for c in item.candidates}
            if not item.selected_candidate_id or item.selected_candidate_id not in valid_ids:
                raise HTTPException(
                    status_code=422,
                    detail=f"Review item {item.item_id} is ambiguous and confirmed but "
                            "selected_candidate_id doesn't match one of its own candidates",
                )

    # Two different ambiguous items can legitimately list the SAME
    # candidate (that's the whole point of surfacing it as ambiguous to
    # both), but that candidate is one real row on the other side and can
    # only actually be the match for ONE of them. The per-item check above
    # only verifies each item picked one of its OWN candidates — it doesn't
    # catch two different items both picking the same one, which would
    # silently double-book a single real transaction against two of ours.
    # Confirmed as a real gap via a live test, not a hypothetical: this
    # previously returned 200 with two different items both "confirmed"
    # against the identical candidate row.
    claims: Dict[str, List[str]] = defaultdict(list)
    for item in reviewed:
        if item.kind == "ambiguous" and item.decision == "confirmed" and item.selected_candidate_id:
            claims[item.selected_candidate_id].append(item.item_id)
    conflicts = {candidate_id: item_ids for candidate_id, item_ids in claims.items() if len(item_ids) > 1}
    if conflicts:
        detail = "; ".join(f"candidate {cid} claimed by {', '.join(ids)}" for cid, ids in conflicts.items())
        raise HTTPException(
            status_code=422,
            detail=f"More than one confirmed item selected the same candidate row, but only one can "
                    f"actually be the real match — dismiss or re-pick all but one first: {detail}",
        )

    # A row can pick up a real, confirmed home in a LATER round (e.g. Pass
    # 5's "Check for more possible groupings") than the round that first
    # marked it "unmatched" — that earlier placeholder is now stale, not a
    # second, contradictory fact about the same row. See
    # drop_stale_unmatched_items's own docstring for the real case this
    # was found from.
    reviewed = drop_stale_unmatched_items(matched, reviewed)

    narration = summarize_exceptions(reviewed, language=req.language.value)

    return ConfirmResponse(
        matched=[_matched_to_model(m) for m in matched],
        reviewed=[_review_to_model(r) for r in reviewed],
        narration=narration,
    )


# ---------------------------------------------------------------------------
# Step 2.5 (optional, manually triggered) — POST /reconcile-leftovers
# ---------------------------------------------------------------------------


class ReconcileLeftoversRequest(BaseModel):
    matched: List[MatchedPairModel] = Field(default_factory=list)
    reviewed: List[ReviewItemModel] = Field(default_factory=list)
    amount_tolerance: Optional[float] = None
    max_date_spread_days: Optional[int] = None


class ReconcileLeftoversResponse(BaseModel):
    needs_review: List[ReviewItemModel]
    amount_tolerance: float
    max_date_spread_days: int


@app.post("/api/comparator/reconcile-leftovers", response_model=ReconcileLeftoversResponse, tags=["C — comparator"])
def reconcile_leftovers(req: ReconcileLeftoversRequest, user_id: str = Depends(get_current_user_id_full_access)):
    """The frontend's "Check for more possible groupings" button — manually
    triggered only, never called automatically and never before the person
    has been through POST /confirm at least once (that's what "the confirmed
    result" below actually means: the result of a completed human review
    pass on Passes 1-4's own findings). Rebuilds the leftover pool per
    build_leftover_pool()'s rules and runs matching.py's Pass 5
    (find_leftover_sum_matches) — a signal-free sum-combination search for
    partial/installment payments Pass 4 verifiably can't catch on its own
    (no shared date, description, or reference). Never returns a
    MatchedPair — every finding is a "sum_match" review item; the frontend
    appends these to its existing reviewed list and sends the combined set
    through POST /confirm again, same confirm/dismiss/unresolved pattern as
    everywhere else, never auto-resolved here."""
    matched = [_matched_from_model(m) for m in req.matched]
    reviewed = [_review_from_model(r) for r in req.reviewed]

    leftover_a, leftover_b = build_leftover_pool(matched, reviewed)

    effective_amount_tolerance = (
        Decimal(str(req.amount_tolerance)) if req.amount_tolerance is not None else AMOUNT_TOLERANCE
    )
    effective_max_date_spread = (
        req.max_date_spread_days if req.max_date_spread_days is not None else SUM_MATCH_MAX_DATE_SPREAD_DAYS
    )

    sum_matches = find_leftover_sum_matches(
        leftover_a, leftover_b,
        amount_tolerance=effective_amount_tolerance,
        max_date_spread_days=effective_max_date_spread,
    )

    return ReconcileLeftoversResponse(
        needs_review=[_review_to_model(r) for r in sum_matches],
        amount_tolerance=float(effective_amount_tolerance),
        max_date_spread_days=effective_max_date_spread,
    )


# ---------------------------------------------------------------------------
# Step 3 — POST /report
# ---------------------------------------------------------------------------

_MIMETYPES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}


class ReportRequest(BaseModel):
    matched: List[MatchedPairModel] = Field(default_factory=list)
    reviewed: List[ReviewItemModel] = Field(default_factory=list)
    narration: Optional[str] = None
    format: ReportFormat = ReportFormat.xlsx
    language: Language = Language.en


@app.post("/api/comparator/report", response_model=None, tags=["C — comparator"])
def comparator_report(req: ReportRequest, user_id: str = Depends(get_current_user_id_full_access)):
    """Step 3 — renders the finalized result as a downloadable file,
    returned directly as the response body (same pattern as Calculator's
    /report)."""
    result = ReconciliationResult(
        matched=[_matched_from_model(m) for m in req.matched],
        reviewed=[_review_from_model(r) for r in req.reviewed],
        narration=req.narration,
    )
    content = generate_reconciliation_report(result, format=req.format.value, language=req.language.value)
    return Response(
        content=content,
        media_type=_MIMETYPES[req.format.value],
        headers={"Content-Disposition": f'attachment; filename="reconciliation_report.{req.format.value}"'},
    )
