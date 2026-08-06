"""
Mizan.ai — Calculator + MCP service, now "VAT Compliance Center".

Owns the ZATCA/VAT calculation engine, structural validation, tax-category
classification, and report generation — all behind the MCP server in
features/calculator/mcp_server.py, which has no consumer besides this
service (that's why MCP wasn't split into its own container: a network hop
with no second caller buys nothing).

Also owns /api/compliance/filing-notes/qa, a direct Postgres read against
work.user_filing_notes (owned at the schema level by Chatbot+common, but
read here — see features/common/db.py's module docstring for why this is a
direct DB read rather than an HTTP call to Chatbot+common: single consumer,
no independent-scaling need, and Postgres is already the shared,
independently-running component).

Job 1 — invoice compliance check (features/calculator/invoice_check.py):
  1. POST /api/compliance/invoice-check                  -> upload N
     invoices, every one checked automatically (no human input mid-batch —
     extraction, structural checks, and a VAT recompute-vs-printed
     comparison run for each file with no pause in between). Returns a
     batch_id + clean/flagged counts.
  2. GET  /api/compliance/invoice-check/{batch_id}/summary -> the
     per-invoice status list, for an on-screen summary table.
  3. POST /api/compliance/invoice-check/{batch_id}/report  -> a ZIP: one
     summary file covering every invoice, plus one detail file per
     FLAGGED invoice only — clean invoices produce no file, since there's
     nothing to act on.

This replaces the previous single-invoice wizard (upload -> extract ->
correct -> line items -> filing details -> confirm -> report) — that shape
assumed one human correcting one invoice interactively, which doesn't hold
up once real use is a pile of 20-30 invoices at once. The wizard's
individual pieces (extraction, the calculation engine, structural
validation) are reused inside invoice_check.py; its own step-by-step
orchestration and endpoints are retired.

Job 2 — period VAT return preparation (features/calculator/period_return.py):
  1. POST /api/compliance/period-return/upload            -> upload sales/
     purchase files (or one multi-sheet workbook covering both). Every
     sheet/file is extracted and role-guessed (sales/purchases), but never
     trusted silently — the guess is returned for the user to confirm.
  2. POST /api/compliance/period-return/{id}/confirm-mapping -> processes
     every row under the confirmed roles: recompute-and-compare where
     possible, resolve reclaimable-vs-blocked per purchase row, and
     collect genuinely ambiguous ones into one collective review instead
     of asking per row. Returns headline figures + anything that needs a
     look.
  3. POST /api/compliance/period-return/{id}/review        -> only needed
     if step 2 returned review_items; applies the human's tier-2 decisions
     and finalizes the figures.
  4. POST /api/compliance/period-return/{id}/report        -> a ZIP with
     two files: the filing-ready headline figures, and the full workpapers
     trail (every excluded purchase + why, every consistency-check
     finding) — same two-file split agreed for this feature, distinct
     naming from job 1's ZIP so both can sit in one downloads folder
     without confusion.
"""

import io
import logging
import re
import zipfile
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.auth import get_current_user_id_full_access
from features.calculator.batch_store import get_batch, initialize_compliance_batches_schema, store_batch, update_batch
from features.calculator.compliance_reports import (
    generate_invoice_check_summary,
    generate_invoice_issue_detail,
    generate_period_return_filing,
    generate_period_return_workpapers,
)
from features.calculator.invoice_check import run_invoice_check
from features.calculator.period_return import (
    PeriodReturnError, apply_review_decisions, extract_sources, guess_business_name, process_period_return,
)
from features.chatbot.filing_notes_qa import answer_filing_question
from features.common.cors import configure_cors
from features.common.db import initialize_auth_schema, store_filing_note

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Idempotent (all IF NOT EXISTS DDL) — safe to call at this service's own
# startup regardless of whether Chatbot+common has started yet. See
# features/common/db.py's initialize_auth_schema() docstring.
initialize_auth_schema()
initialize_compliance_batches_schema()

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("VAT Compliance Center service starting up …")
    state["ready"] = True
    yield
    state.clear()


app = FastAPI(
    title="Mizan.ai — VAT Compliance Center",
    description="Batch invoice compliance checking + period VAT return preparation (Feature E).",
    version="0.2.0",
    lifespan=lifespan,
)
configure_cors(app)


class Language(str, Enum):
    ar = "ar"
    en = "en"


class ReportFormat(str, Enum):
    pdf = "pdf"
    docx = "docx"
    xlsx = "xlsx"


@app.get("/health")
def health():
    return {"status": "ok" if state.get("ready") else "not ok", "version": app.version}


# ---------------------------------------------------------------------------
# Job 1 — invoice compliance check
# ---------------------------------------------------------------------------


class InvoiceCheckResponse(BaseModel):
    batch_id: str
    total: int
    clean: int
    flagged: int


@app.post("/api/compliance/invoice-check", response_model=InvoiceCheckResponse, tags=["E — invoice check"])
def invoice_check(
    files: List[UploadFile] = File(...),
    tolerance: Optional[float] = Form(
        None, description="Optional SAR tolerance for the printed-vs-recalculated VAT/total comparison — "
                           "the app's own default (see config.MISMATCH_TOLERANCE) is used if not set."
    ),
    user_id: str = Depends(get_current_user_id_full_access),
):
    """Select one or more invoices, check them all. No per-invoice human
    step — see features/calculator/invoice_check.py for what "checked"
    means (structural validation + independent VAT recomputation compared
    against whatever's printed).

    Deliberately a plain (not async) endpoint: invoice_check.py calls
    ZatcaCalculatorAgent.run()/.classify(), which each do their own
    asyncio.run() internally (see graph.py) — that only works when there's
    no event loop already running in the current thread. FastAPI runs a
    plain `def` endpoint in its worker thread pool (no loop there); an
    `async def` endpoint runs directly on the main event loop, which broke
    this the first time (confirmed live: "asyncio.run() cannot be called
    from a running event loop") — same reasoning already governs every
    other endpoint in this service that calls the agent."""
    parsed_tolerance = None
    if tolerance is not None:
        try:
            parsed_tolerance = Decimal(str(tolerance))
        except InvalidOperation:
            raise HTTPException(status_code=422, detail=f"Invalid tolerance value: {tolerance!r}")
        if parsed_tolerance < 0:
            raise HTTPException(status_code=422, detail="tolerance must not be negative")

    file_bytes = [(file.filename or "upload", file.file.read()) for file in files]
    result = run_invoice_check(file_bytes, tolerance=parsed_tolerance)
    batch_id = store_batch(user_id, kind="invoice_check", status="done", data=result)
    return {"batch_id": batch_id, "total": result["total"], "clean": result["clean"], "flagged": result["flagged"]}


class InvoiceCheckSummaryItem(BaseModel):
    filename: str
    status: str  # "clean" | "flagged"
    structural_issues: List[dict] = Field(default_factory=list)
    mismatches: List[dict] = Field(default_factory=list)


class InvoiceCheckSummaryResponse(BaseModel):
    batch_id: str
    total: int
    clean: int
    flagged: int
    invoices: List[InvoiceCheckSummaryItem]


def _get_invoice_check_batch(batch_id: str, user_id: str) -> dict:
    batch = get_batch(batch_id, user_id)
    if batch is None or batch["kind"] != "invoice_check":
        raise HTTPException(status_code=404, detail="Batch not found or expired")
    return batch["data"]


@app.get(
    "/api/compliance/invoice-check/{batch_id}/summary",
    response_model=InvoiceCheckSummaryResponse,
    tags=["E — invoice check"],
)
def invoice_check_summary(batch_id: str, user_id: str = Depends(get_current_user_id_full_access)):
    data = _get_invoice_check_batch(batch_id, user_id)
    return {
        "batch_id": batch_id,
        "total": data["total"],
        "clean": data["clean"],
        "flagged": data["flagged"],
        "invoices": [
            {
                "filename": inv["filename"], "status": inv["status"],
                "structural_issues": inv["structural_issues"], "mismatches": inv["mismatches"],
            }
            for inv in data["invoices"]
        ],
    }


class InvoiceCheckReportRequest(BaseModel):
    format: ReportFormat = ReportFormat.xlsx
    language: Language = Language.en


@app.post("/api/compliance/invoice-check/{batch_id}/report", response_model=None, tags=["E — invoice check"])
def invoice_check_report(
    batch_id: str,
    req: InvoiceCheckReportRequest,
    user_id: str = Depends(get_current_user_id_full_access),
):
    """Downloads a ZIP: summary.<format> covering every invoice's status,
    plus one <file>_issues.pdf per FLAGGED invoice only — a clean invoice
    produces no file, since there's nothing in it to act on."""
    data = _get_invoice_check_batch(batch_id, user_id)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        summary_bytes = generate_invoice_check_summary(
            data["invoices"], format=req.format.value, language=req.language.value,
        )
        zf.writestr(f"summary.{req.format.value}", summary_bytes)

        for inv in data["invoices"]:
            if inv["status"] != "flagged":
                continue
            detail_bytes = generate_invoice_issue_detail(inv, language=req.language.value)
            safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", inv["filename"])
            zf.writestr(f"{safe_name}_issues.pdf", detail_bytes)

    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="invoice_compliance_check.zip"'},
    )


# ---------------------------------------------------------------------------
# Job 2 — period VAT return preparation
# ---------------------------------------------------------------------------


class PeriodReturnSource(BaseModel):
    source_id: str
    filename: str
    sheet_label: str
    column_names: List[str]
    preamble: List[str]
    guessed_role: str  # "sales" | "purchases" | "unknown"
    has_category_column: bool


class PeriodReturnUploadResponse(BaseModel):
    batch_id: str
    sources: List[PeriodReturnSource]
    guessed_business_name: Optional[str] = Field(
        None, description="Best-effort guess from a source's preamble text — an editable "
        "suggestion for the report step's business_name field, never applied silently."
    )


def _get_period_return_batch(batch_id: str, user_id: str) -> dict:
    batch = get_batch(batch_id, user_id)
    if batch is None or batch["kind"] != "period_return":
        raise HTTPException(status_code=404, detail="Batch not found or expired")
    return batch


@app.post("/api/compliance/period-return/upload", response_model=PeriodReturnUploadResponse, tags=["E — period return"])
def period_return_upload(
    files: List[UploadFile] = File(...),
    user_id: str = Depends(get_current_user_id_full_access),
):
    """Step 1 — upload sales/purchase files or one multi-sheet workbook
    covering both. Every sheet/file becomes its own "source" with a
    guessed role — never trusted silently, see /confirm-mapping."""
    file_bytes = [(file.filename or "upload", file.file.read()) for file in files]
    try:
        sources = extract_sources(file_bytes)
    except PeriodReturnError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    batch_id = store_batch(user_id, kind="period_return", status="awaiting_mapping", data={"sources": sources})
    return {
        "batch_id": batch_id,
        "sources": [
            {k: v for k, v in s.items() if k != "rows"}  # row data stays server-side only
            for s in sources
        ],
        "guessed_business_name": guess_business_name(sources),
    }


class PeriodReturnHeadline(BaseModel):
    status: str  # "awaiting_review" | "ready"
    output_vat: str
    input_vat_total: str
    reclaimable_input_vat: str
    net_position: str
    consistency_issues: List[dict] = Field(default_factory=list)
    review_items: List[dict] = Field(default_factory=list)


class ConfirmMappingRequest(BaseModel):
    mapping: Dict[str, str] = Field(..., description='source_id -> "sales" | "purchases" | "ignore"')
    default_rates: Dict[str, str] = Field(
        default_factory=dict,
        description='source_id -> "standard" | "zero_rated" | "exempt", for sources with no tax-rate '
        "column (has_category_column: false). Only used for those sources; if a source needing an "
        "answer has none here, Standard 15% is assumed and a notice is added to consistency_issues.",
    )


@app.post(
    "/api/compliance/period-return/{batch_id}/confirm-mapping",
    response_model=PeriodReturnHeadline,
    tags=["E — period return"],
)
def period_return_confirm_mapping(
    batch_id: str, req: ConfirmMappingRequest, user_id: str = Depends(get_current_user_id_full_access),
):
    """Step 2 — process every row under the user-confirmed roles. Returns
    draft headline figures; reclaimable_input_vat excludes anything still
    pending tier-2 review (see /review)."""
    batch = _get_period_return_batch(batch_id, user_id)
    sources = batch["data"]["sources"]

    result = process_period_return(sources, req.mapping, default_rates=req.default_rates)
    update_batch(batch_id, user_id, status=result["status"], data={**batch["data"], "mapping": req.mapping, "result": result})

    return {
        "status": result["status"], "output_vat": result["output_vat"],
        "input_vat_total": result["input_vat_total"], "reclaimable_input_vat": result["reclaimable_input_vat"],
        "net_position": result["net_position"], "consistency_issues": result["consistency_issues"],
        "review_items": result["review_items"],
    }


class ReviewRequest(BaseModel):
    decisions: Dict[str, bool] = Field(..., description="item_id -> reclaimable (true/false)")


@app.post(
    "/api/compliance/period-return/{batch_id}/review",
    response_model=PeriodReturnHeadline,
    tags=["E — period return"],
)
def period_return_review(batch_id: str, req: ReviewRequest, user_id: str = Depends(get_current_user_id_full_access)):
    """Step 3 — apply the human's collective decision on every tier-2
    (genuinely ambiguous) purchase row, and finalize the figures. Anything
    left undecided defaults to NOT reclaimable — see
    period_return.py's apply_review_decisions() for why."""
    batch = _get_period_return_batch(batch_id, user_id)
    result = batch["data"].get("result")
    if result is None:
        raise HTTPException(status_code=422, detail="Call /confirm-mapping before /review")

    final = apply_review_decisions(result, req.decisions)
    update_batch(batch_id, user_id, status="ready", data={**batch["data"], "result": final})

    return {
        "status": final["status"], "output_vat": final["output_vat"],
        "input_vat_total": final["input_vat_total"], "reclaimable_input_vat": final["reclaimable_input_vat"],
        "net_position": final["net_position"], "consistency_issues": final["consistency_issues"],
        "review_items": final["review_items"],
    }


class PeriodReturnReportRequest(BaseModel):
    format: ReportFormat = ReportFormat.xlsx
    language: Language = Language.en
    period_label: str = Field(..., min_length=1, description='e.g. "July 2026" — shown on both report files')
    business_name: Optional[str] = Field(
        None, description="Whose filing this is — saved to work.user_filing_notes if given, same as filing_notes/qa looks up."
    )


@app.post("/api/compliance/period-return/{batch_id}/report", response_model=None, tags=["E — period return"])
def period_return_report(
    batch_id: str, req: PeriodReturnReportRequest, user_id: str = Depends(get_current_user_id_full_access),
):
    """Step 4 — a ZIP with the two agreed files: filing-ready headline
    figures, and the full workpapers trail. Requires /confirm-mapping (and
    /review, if there were tier-2 items) to have finished first."""
    batch = _get_period_return_batch(batch_id, user_id)
    if batch["status"] != "ready":
        raise HTTPException(status_code=422, detail="Not ready — resolve /review items first" if batch["status"] == "awaiting_review" else "Call /confirm-mapping first")
    result = batch["data"]["result"]
    safe_period = re.sub(r"[^A-Za-z0-9_.-]", "_", req.period_label)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        filing_bytes = generate_period_return_filing(result, req.period_label, format=req.format.value, language=req.language.value)
        zf.writestr(f"vat_return_filing_{safe_period}.{req.format.value}", filing_bytes)
        workpapers_bytes = generate_period_return_workpapers(result, req.period_label, format=req.format.value, language=req.language.value)
        zf.writestr(f"vat_return_workpapers_{safe_period}.{req.format.value}", workpapers_bytes)

    if req.business_name:
        notes_text = (
            f"Period VAT return prepared for {req.period_label}: Output VAT {result['output_vat']}, "
            f"Reclaimable Input VAT {result['reclaimable_input_vat']}, Net position {result['net_position']}."
        )
        try:
            store_filing_note(user_id, req.business_name, "ready", notes_text)
        except Exception as exc:  # best-effort — a note-save failure must never block the actual report
            logger.warning("Failed to store filing note for user %s: %s", user_id, exc)

    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="vat_return_{safe_period}.zip"'},
    )


# ---------------------------------------------------------------------------
# Filing-notes history — unaffected by the job 1/2 rebuild
# ---------------------------------------------------------------------------


class FilingNoteSaveRequest(BaseModel):
    customer_name: Optional[str] = None
    status: str = Field(..., min_length=1)
    notes: str = Field(..., min_length=1)


class FilingNoteSaveResponse(BaseModel):
    saved: bool


@app.post("/api/compliance/filing-notes/save", response_model=FilingNoteSaveResponse, tags=["E — compliance"])
def compliance_filing_notes_save(req: FilingNoteSaveRequest, user_id: str = Depends(get_current_user_id_full_access)):
    """Retry endpoint for a filing note that failed to save. Raises 502
    (not 500) on failure, since that's genuinely retryable — the caller
    can call this again."""
    try:
        store_filing_note(user_id, req.customer_name, req.status, req.notes)
    except Exception as exc:
        logger.warning("Retry save of filing note failed for user %s: %s", user_id, exc)
        raise HTTPException(status_code=502, detail=f"Failed to save filing note: {exc}")
    return {"saved": True}


class FilingNotesQARequest(BaseModel):
    question: str = Field(..., min_length=1)
    customer_name: Optional[str] = None


class FilingNotesQAResponse(BaseModel):
    answer: str


@app.post("/api/compliance/filing-notes/qa", response_model=FilingNotesQAResponse, tags=["E — compliance"])
def compliance_filing_notes_qa(req: FilingNotesQARequest, user_id: str = Depends(get_current_user_id_full_access)):
    """Natural-language Q&A over the caller's own filing history
    (work.user_filing_notes) — e.g. "what did I do last time?" or "what did
    I do for customer Mohammed?". Query layer only; no customer match never
    fabricates an answer. See scripts/seed_filing_notes.py for test data.
    """
    answer = answer_filing_question(user_id=user_id, question=req.question, customer_name=req.customer_name)
    return {"answer": answer}
