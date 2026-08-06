"""
Mizan.ai — document extraction service (Feature E, Phase 1).

Internal-only service, reachable over the docker-compose network by the
main `chatbot` gateway (see MIZAN_DOC_EXTRACTION_URL) — not exposed to end
users directly, so no auth here (matches how the gateway itself already
authenticates the end user before ever calling this service).

Thin FastAPI wrapper around features/common/document_extraction.py's
route_and_extract() — all the actual format-detection/extraction logic
lives there, shared with (eventually) Feature C.
"""

import logging
import os
import shutil
import tempfile

from fastapi import FastAPI, File, HTTPException, UploadFile

from features.common.cors import configure_cors
from features.common.document_extraction import ExtractionError, route_and_extract

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Mizan.ai — Document Extraction")
configure_cors(app)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/extract")
def extract(file: UploadFile = File(...)):
    filename = file.filename or "upload"
    upload_dir = tempfile.mkdtemp(prefix="mizan_extract_upload_")
    input_path = os.path.join(upload_dir, filename)
    try:
        with open(input_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        try:
            return route_and_extract(input_path, filename)
        except ExtractionError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception as exc:
            logger.exception("Extraction failed for %s", filename)
            raise HTTPException(status_code=500, detail=f"Extraction failed: {exc}")
    finally:
        shutil.rmtree(upload_dir, ignore_errors=True)
