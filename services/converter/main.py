"""
Mizan.ai — Converter service (Feature H, DOCX/XLSX -> PDF, LibreOffice
headless, free tier).

Plain function, no MCP wrapping (see features/converter/convert.py)
— a local LibreOffice subprocess, not a hosted model call. PDF -> DOCX is
explicitly out of scope (LibreOffice can't do it), rejected with a clear
error rather than attempted.

Kept as its own service, separate from Translator, despite both being
small: different resource profile (this one runs a real CPU/memory-cost
subprocess with its own concurrency-safety requirements; Translator is a
thin proxy to a Modal endpoint) — bundling them would force them to scale
together despite unrelated load patterns.
"""

import logging
import os
import shutil
import tempfile

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from features.common.cors import configure_cors
from features.converter.convert import ConversionError, convert_to_pdf
from app.auth import get_current_user_id_full_access

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Mizan.ai — Converter",
    description="DOCX/XLSX -> PDF conversion (Feature H).",
    version="0.1.0",
)
configure_cors(app)


@app.get("/health")
def health():
    return {"status": "ok", "version": app.version}


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
