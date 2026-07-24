"""
Mizan.ai — FastAPI entrypoint (skeleton).

Endpoint stubs only. No model calls, no vLLM/Modal integration yet.
Each endpoint accepts the right request shape and returns mock data,
so the frontend and agent layers can be built against a stable contract.

Features:
    A - ZATCA/VAT RAG Q&A
    B - EN<->AR document translation        (paid tier)
    C - Document comparison                 (3/month free)
    D - General chat                        (free tier)
    E - ZATCA compliance calculator         (core differentiator, HITL)
    F - Voice interaction (STT/TTS)         (paid tier)
    G - Multi-source document synthesis     (2/month free, max 3 docs)
    H - Format conversion DOCX/XLSX -> PDF  (free, unlimited)
"""

import logging
import os
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from enum import Enum
from typing import List, Optional

import requests
from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from agents.chatbot.filing_notes_qa import answer_filing_question
from agents.chatbot.langgraph_chatbot import ChatbotAgent
from agents.chatbot.memory import (
    create_user,
    get_user_by_email,
    get_user_by_id,
    initialize_auth_schema,
    reset_password,
    set_temp_password,
    verify_password,
)
from agents.doc_format_conversion.convert import ConversionError, convert_to_pdf
from agents.know_vat_n_zatca.online.graph import KnowVatZatcaAgent
from agents.know_vat_n_zatca.qdrant_store import ensure_collection as ensure_know_vat_zatca_collection
from agents.know_vat_n_zatca.sqlite_store import initialize_schema as initialize_know_vat_zatca_schema
from agents.translation.translate import TranslationError, translate_paragraph
from app.auth import create_jwt, generate_temp_password, get_current_user_id, get_current_user_id_full_access
from app.email import send_email

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Ordering matters here: ChatbotAgent() (below) triggers
# initialize_memory_schemas() as a side effect of its own construction, and
# both this line and that one run at *import time* — before FastAPI's
# lifespan hook ever fires. work.user_filing_notes has a foreign key to
# auth.users(id), so auth.users must exist first. Rather than split "some
# schema init runs in lifespan, some at import time" (fragile, order-of-
# definition dependent), both now run at import time, auth schema first.
# Flagging this per docs/mizan_backend_auth_handoff_v3.pdf §5.11, which
# asked for the trade-off to be surfaced rather than silently picked.
initialize_auth_schema()
chatbot_agent = ChatbotAgent()

# Know VAT & ZATCA (Feature A / RAG) — separate SQLite schema + Qdrant
# collection, no FK dependency on anything above, so ordering relative to
# the auth/chatbot init above doesn't matter. ensure_know_vat_zatca_collection()
# so the online pipeline's search() doesn't fail on a fresh Qdrant instance
# before any offline ingestion has run.
initialize_know_vat_zatca_schema()
ensure_know_vat_zatca_collection()
know_vat_zatca_agent = KnowVatZatcaAgent()

# ---------------------------------------------------------------------------
# App state & lifespan
# ---------------------------------------------------------------------------

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Warm-up hook — later: init DB pools, Qdrant client, Modal handles."""
    logger.info("Mizan.ai starting up …")
    state["ready"] = True
    logger.info("Service is up.")
    yield
    chatbot_agent.close()
    know_vat_zatca_agent.close()
    state.clear()


app = FastAPI(
    title="Mizan.ai API",
    description="Arabic-first agentic AI platform for ZATCA/VAT compliance.",
    version="0.1.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Shared models
# ---------------------------------------------------------------------------


class Tier(str, Enum):
    free = "free"
    paid = "paid"


class Language(str, Enum):
    ar = "ar"
    en = "en"


class HealthResponse(BaseModel):
    status: str
    version: str


# ---------------------------------------------------------------------------
# Ops
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse, tags=["ops"])
def health():
    """App status check."""
    return {
        "status": "ok" if state.get("ready") else "not ok",
        "version": app.version,
    }


# ---------------------------------------------------------------------------
# Auth — email/password + JWT (stateless, no refresh tokens, no logout
# endpoint in v1 — see docs/mizan_backend_auth_handoff_v3.pdf §5.3)
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    email: str
    password: str
    nickname: Optional[str] = None


class LoginRequest(BaseModel):
    email: str
    password: str


class AuthResponse(BaseModel):
    user_id: str
    email: str
    jwt_token: str
    message: str
    must_reset_password: bool = False


@app.post("/api/auth/register", response_model=AuthResponse, tags=["auth"])
def register(req: RegisterRequest):
    """Create an account and mint a JWT — registration auto-logs-in."""
    if get_user_by_email(req.email):
        raise HTTPException(status_code=409, detail="Email already registered")

    try:
        user_id = create_user(req.email, req.password, req.nickname)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    token = create_jwt(user_id=user_id, email=req.email.strip().lower())
    return {
        "user_id": user_id,
        "email": req.email.strip().lower(),
        "jwt_token": token,
        "message": "Registration successful",
    }


@app.post("/api/auth/login", response_model=AuthResponse, tags=["auth"])
def login(req: LoginRequest):
    """Verify credentials and mint a fresh JWT — every login is a new token,
    the previous one is simply discarded by the frontend.

    If the account has a pending password reset (see /api/auth/forgot-password),
    a still-valid temp password logs in successfully but the token is scoped
    to must_reset_password only — every other protected endpoint rejects it
    until /api/auth/reset-password is called. An expired temp password is
    treated the same as a wrong password.
    """
    user = get_user_by_email(req.email)
    # Generic error for both cases — never reveal which part was wrong
    # (account-enumeration leak).
    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    must_reset = user["must_reset_password"]
    if must_reset:
        expires_at = user["temp_password_expires_at"]
        if not expires_at or expires_at < datetime.utcnow():
            raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_jwt(user_id=user["id"], email=user["email"], must_reset_password=must_reset)
    return {
        "user_id": user["id"],
        "email": user["email"],
        "jwt_token": token,
        "message": "Login successful",
        "must_reset_password": must_reset,
    }


class ForgotPasswordRequest(BaseModel):
    email: str


class ForgotPasswordResponse(BaseModel):
    message: str


class ResetPasswordRequest(BaseModel):
    new_password: str


@app.post("/api/auth/forgot-password", response_model=ForgotPasswordResponse, tags=["auth"])
def forgot_password(req: ForgotPasswordRequest):
    """Issues a 15-minute temp password and emails it via Mailgun, if the
    email is registered. Always returns the same generic message either way
    — same account-enumeration reasoning as the login error contract.
    """
    generic_message = "If that email is registered, a temporary password has been sent."

    temp_password = generate_temp_password()
    user_id = set_temp_password(req.email, temp_password, expires_in_minutes=15)

    if user_id:
        try:
            send_email(
                to=req.email,
                subject="Mizan.ai — Your temporary password",
                text=(
                    f"Your temporary password is: {temp_password}\n\n"
                    "It expires in 15 minutes. Log in with it, then you'll be "
                    "required to set a new password before you can do anything else."
                ),
            )
        except requests.RequestException as exc:
            logger.warning("Failed to send password-reset email to %s: %s", req.email, exc)

    return {"message": generic_message}


@app.post("/api/auth/reset-password", response_model=AuthResponse, tags=["auth"])
def reset_password_endpoint(req: ResetPasswordRequest, user_id: str = Depends(get_current_user_id)):
    """Sets a real password and clears must_reset_password. Accepts a
    must-reset token (unlike full-access endpoints) since that's exactly
    the case this exists to resolve.
    """
    try:
        reset_password(user_id, req.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    user = get_user_by_id(user_id)
    token = create_jwt(user_id=user["id"], email=user["email"], must_reset_password=False)
    return {
        "user_id": user["id"],
        "email": user["email"],
        "jwt_token": token,
        "message": "Password reset successful",
        "must_reset_password": False,
    }


# ---------------------------------------------------------------------------
# A — ZATCA/VAT RAG Q&A  (Qwen3.5-9B + BGE-M3 retrieval, free tier)
# ---------------------------------------------------------------------------


class KnowVatZatcaChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    session_id: Optional[str] = None


class KnowVatZatcaCitation(BaseModel):
    document_type: Optional[str] = None
    jurisdiction: Optional[str] = None
    version_label: Optional[str] = None
    source_site: Optional[str] = None
    source_url: Optional[str] = None


class KnowVatZatcaChatResponse(BaseModel):
    reply: str
    session_id: str
    citations: List[KnowVatZatcaCitation] = []
    fallback_kind: Optional[str] = None


@app.post("/api/know-vat-zatca/chat", response_model=KnowVatZatcaChatResponse, tags=["A — know vat & zatca"])
def know_vat_zatca_chat(req: KnowVatZatcaChatRequest, user_id: str = Depends(get_current_user_id_full_access)):
    """The 'Know VAT & ZATCA' page's own chat — grounded in the RAG
    knowledge base, entirely separate from the general homepage chatbot
    (/api/chat). See agents/know_vat_n_zatca/online/graph.py for the
    retrieval + citation + fail-closed-fallback pipeline behind this.
    """
    result = know_vat_zatca_agent.ask(message=req.message, session_id=req.session_id, user_id=user_id)
    return {
        "reply": result["reply"],
        "session_id": result["session_id"],
        "citations": result["citations"],
        "fallback_kind": result["fallback_kind"],
    }


# ---------------------------------------------------------------------------
# B — Translation EN<->AR  (MADLAD-400, paid tier)
#
# v1 scope only: paste a paragraph, get it translated — synchronous,
# no file upload, no target-language picker (source is auto-detected, target
# is always "the other" language). Document upload (DOCX/PDF/text),
# formatting-preserving reassembly, and freeform translation instructions
# are a later phase — see agents/translation/config.py. The previous mock
# here modeled this as an async job (job_id + poll), which fit document
# translation; a single paragraph translates fast enough that a plain
# request/response (same shape as /api/know-vat-zatca/chat) fits better.
# ---------------------------------------------------------------------------


class TranslateRequest(BaseModel):
    text: str = Field(..., min_length=1)


class TranslateResponse(BaseModel):
    translated_text: str
    source_language: Language
    target_language: Language


@app.post("/api/translate", response_model=TranslateResponse, tags=["B — translation"])
def translate(req: TranslateRequest, user_id: str = Depends(get_current_user_id_full_access)):
    """Detects English/Arabic and translates to the other language."""
    try:
        result = translate_paragraph(req.text)
    except TranslationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return result


# ---------------------------------------------------------------------------
# C — Document comparison  (3/month free)
# ---------------------------------------------------------------------------


class ComparisonDifference(BaseModel):
    location: str
    doc_a_value: Optional[str]
    doc_b_value: Optional[str]
    change_type: str  # added | removed | modified


class CompareResponse(BaseModel):
    comparison_id: str
    summary: str
    differences: List[ComparisonDifference]
    remaining_free_quota: Optional[int] = None


@app.post("/api/compare", response_model=CompareResponse, tags=["C — comparison"])
async def compare_documents(
    file_a: UploadFile = File(...),
    file_b: UploadFile = File(...),
):
    """Compare two related documents and highlight differences."""
    return {
        "comparison_id": "cmp_mock_001",
        "summary": "[mock] 3 differences found between the two documents.",
        "differences": [
            {
                "location": "field: total_amount",
                "doc_a_value": "1500.00",
                "doc_b_value": "1750.00",
                "change_type": "modified",
            }
        ],
        "remaining_free_quota": 2,
    }


# ---------------------------------------------------------------------------
# D — General chat  (Qwen3.5-9B free tier / 35B tool-calling paid)
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    reply: str
    session_id: str
    remaining_daily_quota: Optional[int] = None
    status: Optional[str] = None


@app.post("/api/chat", response_model=ChatResponse, tags=["D — chat"])
def chat(req: ChatRequest, user_id: str = Depends(get_current_user_id_full_access)):
    """General-purpose chat for the homepage assistant. Requires a valid JWT
    (Authorization: Bearer <token>) from /api/auth/login or /api/auth/register.

    session_id identifies one conversation thread (LangGraph's thread_id) and
    is independent of the caller's account: it's temporary and per-conversation,
    while user_id (from the JWT) is permanent and keys profile memory across
    conversations. If the caller doesn't supply a session_id — e.g. a brand-new
    conversation — the server mints one and hands it back; the caller must
    resend that exact value on every subsequent turn to keep this thread's
    checkpointed history.
    """
    session_id = req.session_id or str(uuid.uuid4())
    reply = chatbot_agent.generate_reply(
        message=req.message,
        language=None,
        session_id=session_id,
        user_id=user_id,
    )
    status = "warming_up" if chatbot_agent.is_warming_up() else "ready"
    if status == "warming_up":
        reply = "Please wait — the assistant is still loading and warming up."
    return {
        "reply": reply,
        "session_id": session_id,
        "remaining_daily_quota": 29,
        "status": status,
    }


# ---------------------------------------------------------------------------
# E — ZATCA compliance calculator  (Qwen3.5-35B-A3B brain, HITL, core feature)
#
# Multi-step flow:
#   1. POST /api/compliance/upload      -> upload docs, Docling extraction
#   2. GET  /api/compliance/{id}/extraction -> review extracted fields
#   3. POST /api/compliance/{id}/validate   -> human confirms/corrects (HITL)
#   4. POST /api/compliance/{id}/calculate  -> run compliance calculation
#   5. POST /api/compliance/{id}/report     -> generate output report
# ---------------------------------------------------------------------------


class ComplianceUploadResponse(BaseModel):
    filing_id: str
    documents_received: int
    status: str  # extracting | awaiting_validation | calculating | done


class ExtractedField(BaseModel):
    field_name: str
    value: str
    confidence: float
    source_document: str


class ExtractionResponse(BaseModel):
    filing_id: str
    status: str
    fields: List[ExtractedField]


class ValidationRequest(BaseModel):
    corrections: dict = Field(
        default_factory=dict,
        description="field_name -> corrected value; empty dict = approve as-is",
    )
    approved: bool


class CalculationResponse(BaseModel):
    filing_id: str
    vat_due: float
    currency: str
    compliance_status: str  # compliant | issues_found
    issues: List[str]


class ReportFormat(str, Enum):
    pdf = "pdf"
    docx = "docx"
    xlsx = "xlsx"


class ReportRequest(BaseModel):
    format: ReportFormat = ReportFormat.pdf
    language: Language = Language.ar


class ReportResponse(BaseModel):
    filing_id: str
    report_url: str
    format: ReportFormat


@app.post("/api/compliance/upload", response_model=ComplianceUploadResponse, tags=["E — compliance"])
async def compliance_upload(files: List[UploadFile] = File(...)):
    """Step 1 — upload invoices/POs/payment docs for a ZATCA filing."""
    return {
        "filing_id": "fil_mock_001",
        "documents_received": len(files),
        "status": "extracting",
    }


@app.get("/api/compliance/{filing_id}/extraction", response_model=ExtractionResponse, tags=["E — compliance"])
def compliance_extraction(filing_id: str):
    """Step 2 — fetch extracted fields for human review."""
    return {
        "filing_id": filing_id,
        "status": "awaiting_validation",
        "fields": [
            {
                "field_name": "invoice_total",
                "value": "5750.00",
                "confidence": 0.94,
                "source_document": "invoice_march.pdf",
            }
        ],
    }


@app.post("/api/compliance/{filing_id}/validate", response_model=ComplianceUploadResponse, tags=["E — compliance"])
def compliance_validate(filing_id: str, req: ValidationRequest):
    """Step 3 — human-in-the-loop validation before calculation."""
    if not req.approved:
        raise HTTPException(status_code=400, detail="Validation rejected — corrections required.")
    return {
        "filing_id": filing_id,
        "documents_received": 0,
        "status": "calculating",
    }


@app.post("/api/compliance/{filing_id}/calculate", response_model=CalculationResponse, tags=["E — compliance"])
def compliance_calculate(filing_id: str):
    """Step 4 — run the ZATCA/VAT compliance calculation."""
    return {
        "filing_id": filing_id,
        "vat_due": 862.50,
        "currency": "SAR",
        "compliance_status": "compliant",
        "issues": [],
    }


@app.post("/api/compliance/{filing_id}/report", response_model=ReportResponse, tags=["E — compliance"])
def compliance_report(filing_id: str, req: ReportRequest):
    """Step 5 — generate the output report via generate_report MCP tool."""
    return {
        "filing_id": filing_id,
        "report_url": f"/downloads/{filing_id}.{req.format.value}",
        "format": req.format,
    }


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
    fabricates an answer. See scripts/seed_filing_notes.py for test data —
    Feature E's real extraction pipeline doesn't write to this table yet.
    """
    answer = answer_filing_question(user_id=user_id, question=req.question, customer_name=req.customer_name)
    return {"answer": answer}


# ---------------------------------------------------------------------------
# G — Multi-source document synthesis  (max 3 docs, 2/month free, no HITL v1)
# ---------------------------------------------------------------------------


class SynthesisResponse(BaseModel):
    synthesis_id: str
    status: str  # processing | done | failed
    report_url: Optional[str] = None
    remaining_free_quota: Optional[int] = None


@app.post("/api/synthesis", response_model=SynthesisResponse, tags=["G — synthesis"])
async def synthesize_documents(files: List[UploadFile] = File(...)):
    """Synthesize up to 3 documents into a single report."""
    if len(files) > 3:
        raise HTTPException(status_code=400, detail="Maximum 3 input documents per synthesis.")
    return {
        "synthesis_id": "syn_mock_001",
        "status": "processing",
        "report_url": None,
        "remaining_free_quota": 1,
    }


@app.get("/api/synthesis/{synthesis_id}", response_model=SynthesisResponse, tags=["G — synthesis"])
def get_synthesis(synthesis_id: str):
    """Poll a synthesis job."""
    return {
        "synthesis_id": synthesis_id,
        "status": "done",
        "report_url": f"/downloads/{synthesis_id}.pdf",
        "remaining_free_quota": 1,
    }


# ---------------------------------------------------------------------------
# H — Format conversion DOCX/XLSX -> PDF  (LibreOffice headless, free)
#
# Plain function, no MCP wrapping (see agents/doc_format_conversion/convert.py)
# — same "dedicated page, no Modal call" shape as Translate v1, just with a
# local subprocess instead of a hosted model. PDF -> DOCX is explicitly out
# of scope (LibreOffice can't do it — see convert.py's module docstring),
# rejected with a clear error rather than attempted.
# ---------------------------------------------------------------------------


@app.post(
    "/api/convert",
    tags=["H — conversion"],
    responses={200: {"content": {"application/pdf": {}}}},
)
def convert_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user_id_full_access),
):
    """Converts an uploaded DOCX or XLSX file to PDF and returns the PDF
    directly as the response body. Synchronous (not async def) on purpose
    — the conversion itself is blocking subprocess/file I/O, and a plain
    def endpoint lets FastAPI run it in its thread pool instead of stalling
    the event loop for every other in-flight request."""
    filename = file.filename or "upload"
    source_extension = os.path.splitext(filename)[1].lower()

    upload_dir = tempfile.mkdtemp(prefix="mizan_upload_")
    input_path = os.path.join(upload_dir, filename)
    with open(input_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        output_path = convert_to_pdf(input_path, source_extension)
    except ConversionError as exc:
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(exc))

    background_tasks.add_task(shutil.rmtree, upload_dir, ignore_errors=True)
    background_tasks.add_task(shutil.rmtree, os.path.dirname(output_path), ignore_errors=True)

    output_filename = os.path.splitext(filename)[0] + ".pdf"
    return FileResponse(
        output_path,
        media_type="application/pdf",
        filename=output_filename,
        background=background_tasks,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
