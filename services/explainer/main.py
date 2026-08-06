"""
Mizan.ai — RAG online service (Feature A, "Know VAT & ZATCA" Q&A,
Qwen3.5-9B + BGE-M3 retrieval, free tier).

Split out from general chat specifically because of its Qdrant dependency
and different (retrieval-heavy) load profile. Read-only against Qdrant +
SQLite — this pipeline never writes to the knowledge base. The offline
ingestion pipeline (features/explainer/offline/,
services/offline_data_ingestion/) stays a manual/periodic job, not a live
service, unchanged by this split.
"""

import logging
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import Depends, FastAPI
from pydantic import BaseModel, Field

from features.common.cors import configure_cors
from features.explainer.online.graph import KnowVatZatcaAgent
from features.explainer.qdrant_store import ensure_collection as ensure_know_vat_zatca_collection
from features.explainer.sqlite_store import initialize_schema as initialize_know_vat_zatca_schema
from app.auth import get_current_user_id_full_access

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# No FK dependency on the auth schema — SQLite + Qdrant collection init,
# independent of Chatbot+common's Postgres auth-schema startup.
initialize_know_vat_zatca_schema()
ensure_know_vat_zatca_collection()
know_vat_zatca_agent = KnowVatZatcaAgent()

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("RAG online service starting up …")
    state["ready"] = True
    yield
    know_vat_zatca_agent.close()
    state.clear()


app = FastAPI(
    title="Mizan.ai — RAG Online",
    description="Know VAT & ZATCA retrieval-grounded Q&A (Feature A).",
    version="0.1.0",
    lifespan=lifespan,
)
configure_cors(app)


@app.get("/health")
def health():
    return {"status": "ok" if state.get("ready") else "not ok", "version": app.version}


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
    (Chatbot+common's /api/chat). See features/explainer/online/graph.py
    for the retrieval + citation + fail-closed-fallback pipeline behind
    this.
    """
    result = know_vat_zatca_agent.ask(message=req.message, session_id=req.session_id, user_id=user_id)
    return {
        "reply": result["reply"],
        "session_id": result["session_id"],
        "citations": result["citations"],
        "fallback_kind": result["fallback_kind"],
    }
