"""
Mizan.ai — Chatbot + common service.

Owns auth (email/password + JWT — stateless, shared JWT_SECRET verified by
every other service), general chat (Feature D), and — since it has no
real backing logic or a better home — the still-mock /api/synthesis
(Feature G, out of scope entirely).

Features A (RAG), B (translation), C (comparator), E (compliance
calculator), H (conversion), and PDF Editor have each moved out to their
own service — see services/explainer/, services/translator/,
services/comparator/, services/calculator/, services/converter/,
services/editor/.
"""

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from enum import Enum
from typing import List, Optional

import requests
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from features.chatbot.langgraph_chatbot import ChatbotAgent
from features.common.cors import configure_cors
from features.common.db import store_feedback
from features.chatbot.memory import (
    create_user,
    get_user_by_email,
    get_user_by_id,
    initialize_auth_schema,
    reset_password,
    set_temp_password,
    verify_password,
)
from app.auth import create_jwt, generate_temp_password, get_current_user_id, get_current_user_id_full_access
from app.email import send_email

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Idempotent (all IF NOT EXISTS DDL) — safe to call at this service's own
# startup regardless of start order relative to Calculator+MCP, which also
# calls this. ChatbotAgent() triggers initialize_memory_schemas() as a side
# effect of its own construction; work.user_filing_notes has a foreign key
# to auth.users(id), so auth.users must exist first — hence this call comes
# before ChatbotAgent() below. See features/common/db.py's docstring.
initialize_auth_schema()
chatbot_agent = ChatbotAgent()

# ---------------------------------------------------------------------------
# App state & lifespan
# ---------------------------------------------------------------------------

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Warm-up hook — later: init DB pools, Modal handles."""
    logger.info("Mizan.ai starting up …")
    state["ready"] = True
    logger.info("Service is up.")
    yield
    chatbot_agent.close()
    state.clear()


app = FastAPI(
    title="Mizan.ai API — Chatbot + Common",
    description="Arabic-first agentic AI platform — auth, general chat.",
    version="0.1.0",
    lifespan=lifespan,
)
configure_cors(app)

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
# Feedback — the frontend's "Contact us" page. Deliberately just a DB row,
# not an email send: no dependency on Mailgun being configured/working,
# and it's checked manually later rather than needing a read/admin
# endpoint here.
# ---------------------------------------------------------------------------


class FeedbackRequest(BaseModel):
    message: str = Field(..., min_length=1)


class FeedbackResponse(BaseModel):
    received: bool


@app.post("/api/feedback", response_model=FeedbackResponse, tags=["feedback"])
def submit_feedback(req: FeedbackRequest, user_id: str = Depends(get_current_user_id_full_access)):
    store_feedback(user_id, req.message)
    return {"received": True}


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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
