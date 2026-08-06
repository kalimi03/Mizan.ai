"""
Mizan.ai — Translator service (Feature B, EN<->AR, MADLAD-400, paid tier).

v1 scope only: paste a paragraph, get it translated — synchronous, no file
upload, no target-language picker (source is auto-detected, target is
always "the other" language).

Exposes two endpoints:
  - POST /api/translate      — the public, end-user endpoint (JWT required).
  - POST /internal/translate — unauthenticated, server-to-server only.
    Same pattern as doc-extraction: never called directly by end users,
    only reachable within the docker-compose/K8s network. This is what
    Calculator's report generation (features/calculator/report.py)
    calls to translate line-item descriptions now that Calculator and
    Translator are separate services and can no longer share an in-process
    function call.
"""

import logging
from enum import Enum

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from features.common.cors import configure_cors
from features.translator.translate import TranslationError, translate_paragraph
from app.auth import get_current_user_id_full_access

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Mizan.ai — Translator",
    description="EN<->AR translation (Feature B).",
    version="0.1.0",
)
configure_cors(app)


class Language(str, Enum):
    ar = "ar"
    en = "en"


@app.get("/health")
def health():
    return {"status": "ok", "version": app.version}


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


@app.post("/internal/translate", response_model=TranslateResponse)
def translate_internal(req: TranslateRequest):
    """No auth dependency — internal, server-to-server only. See module
    docstring for why this mirrors the doc-extraction precedent."""
    try:
        result = translate_paragraph(req.text)
    except TranslationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return result
