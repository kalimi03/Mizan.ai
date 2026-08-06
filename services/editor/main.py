"""
Mizan.ai — PDF Editor service (companion to Converter, see
docs/mizan_pdf_editor_scoping_handoff.pdf). Overlay-only operations via
features/editor/edit.py (PyMuPDF) — no MCP wrapping, same "plain
function, synchronous endpoint" shape as Converter/Translator. Every
endpoint here is a self-contained multipart request: the PDF file plus
operation parameters as Form fields (JSON-encoded for structured/list
parameters, since multipart requests can't carry a nested JSON body the
way a pure-JSON endpoint could).

Kept as its own service, separate from Converter, because v2 is planned to
add heavier features here — isolating now avoids a disruptive split later
once it's grown.
"""

import json
import logging
import os
import shutil
import tempfile
from typing import List, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from features.editor.edit import (
    PDFEditError,
    add_annotations,
    add_password,
    add_signature,
    add_text_boxes,
    add_watermark,
    compress_pdf,
    fill_form,
    merge_pdfs,
    redact,
    render_page_preview,
    reorder_pages,
    rotate_pages,
    split_pdf,
)
from app.auth import get_current_user_id_full_access
from features.common.cors import configure_cors

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Mizan.ai — PDF Editor",
    description="PDF annotation/editing overlay operations.",
    version="0.1.0",
)
configure_cors(app)


@app.get("/health")
def health():
    return {"status": "ok", "version": app.version}


def _parse_json_form(raw: str, field_name: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON in '{field_name}': {exc}")


def _parse_pages_param(raw: str):
    """'pages' Form fields accept either the literal string "all" or a
    JSON-encoded list of 0-indexed page numbers, e.g. "[0,2]"."""
    if raw == "all":
        return "all"
    return _parse_json_form(raw, "pages")


def _run_pdf_edit(background_tasks: BackgroundTasks, file: UploadFile, op_fn, *args, **kwargs):
    filename = file.filename or "document.pdf"
    upload_dir = tempfile.mkdtemp(prefix="mizan_pdf_upload_")
    input_path = os.path.join(upload_dir, filename)
    with open(input_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        output_path = op_fn(input_path, *args, **kwargs)
    except PDFEditError as exc:
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(exc))

    background_tasks.add_task(shutil.rmtree, upload_dir, ignore_errors=True)
    background_tasks.add_task(shutil.rmtree, os.path.dirname(output_path), ignore_errors=True)

    return FileResponse(
        output_path,
        media_type="application/pdf",
        filename=os.path.splitext(filename)[0] + "_edited.pdf",
        background=background_tasks,
    )


@app.post(
    "/api/pdf-editor/annotate",
    tags=["PDF Editor"],
    responses={200: {"content": {"application/pdf": {}}}},
)
def pdf_annotate(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    annotations: str = Form(...),
    user_id: str = Depends(get_current_user_id_full_access),
):
    """annotations: JSON array, e.g.
    [{"type":"highlight","page":0,"rect":[x0,y0,x1,y1],"color":[r,g,b]}] —
    supported types: highlight, note, rect, circle, line, freehand."""
    parsed = _parse_json_form(annotations, "annotations")
    return _run_pdf_edit(background_tasks, file, add_annotations, parsed)


@app.post(
    "/api/pdf-editor/text-boxes",
    tags=["PDF Editor"],
    responses={200: {"content": {"application/pdf": {}}}},
)
def pdf_text_boxes(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    text_boxes: str = Form(...),
    user_id: str = Depends(get_current_user_id_full_access),
):
    """text_boxes: JSON array, e.g.
    [{"page":0,"rect":[x0,y0,x1,y1],"text":"...","font_size":11,"color":[r,g,b]}]."""
    parsed = _parse_json_form(text_boxes, "text_boxes")
    return _run_pdf_edit(background_tasks, file, add_text_boxes, parsed)


@app.post(
    "/api/pdf-editor/fill-form",
    tags=["PDF Editor"],
    responses={200: {"content": {"application/pdf": {}}}},
)
def pdf_fill_form(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    fields: str = Form(...),
    user_id: str = Depends(get_current_user_id_full_access),
):
    """fields: JSON object of {"field_name": value}, matched against the
    PDF's existing AcroForm field names — only fills fields that already
    exist on the template."""
    parsed = _parse_json_form(fields, "fields")
    return _run_pdf_edit(background_tasks, file, fill_form, parsed)


@app.post(
    "/api/pdf-editor/page-preview",
    tags=["PDF Editor"],
    responses={200: {"content": {"image/png": {}}}},
)
def pdf_page_preview(
    file: UploadFile = File(...),
    page: int = Form(0),
    user_id: str = Depends(get_current_user_id_full_access),
):
    """Rasterizes one page to a PNG so the frontend can show the real page
    and let the user draw the signature rect on it directly, instead of
    typing point coordinates by hand. The page's actual size in points is
    returned as response headers so the caller can convert on-screen pixel
    positions back into the point coordinates /signature expects."""
    filename = file.filename or "document.pdf"
    upload_dir = tempfile.mkdtemp(prefix="mizan_pdf_upload_")
    input_path = os.path.join(upload_dir, filename)
    try:
        with open(input_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        try:
            png_bytes, width_points, height_points = render_page_preview(input_path, page)
        except PDFEditError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return Response(
            content=png_bytes,
            media_type="image/png",
            headers={
                "X-Page-Width-Points": str(width_points),
                "X-Page-Height-Points": str(height_points),
                "Access-Control-Expose-Headers": "X-Page-Width-Points, X-Page-Height-Points",
            },
        )
    finally:
        shutil.rmtree(upload_dir, ignore_errors=True)


@app.post(
    "/api/pdf-editor/signature",
    tags=["PDF Editor"],
    responses={200: {"content": {"application/pdf": {}}}},
)
def pdf_signature(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    signature_image: UploadFile = File(...),
    page: int = Form(...),
    rect: str = Form(...),
    darkness: float = Form(1.0, description="1.0 = as photographed. >1 darkens a too-light signature; <1 lightens a too-dark one."),
    user_id: str = Depends(get_current_user_id_full_access),
):
    """rect: JSON array [x0,y0,x1,y1] — where on the page to place the
    uploaded signature image."""
    parsed_rect = _parse_json_form(rect, "rect")
    if darkness <= 0:
        raise HTTPException(status_code=422, detail="darkness must be > 0")

    filename = file.filename or "document.pdf"
    upload_dir = tempfile.mkdtemp(prefix="mizan_pdf_upload_")
    input_path = os.path.join(upload_dir, filename)
    with open(input_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    image_path = os.path.join(upload_dir, signature_image.filename or "signature.png")
    with open(image_path, "wb") as f:
        shutil.copyfileobj(signature_image.file, f)

    try:
        output_path = add_signature(input_path, page, parsed_rect, image_path, darkness=darkness)
    except PDFEditError as exc:
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(exc))

    background_tasks.add_task(shutil.rmtree, upload_dir, ignore_errors=True)
    background_tasks.add_task(shutil.rmtree, os.path.dirname(output_path), ignore_errors=True)
    return FileResponse(
        output_path,
        media_type="application/pdf",
        filename=os.path.splitext(filename)[0] + "_signed.pdf",
        background=background_tasks,
    )


@app.post(
    "/api/pdf-editor/reorder-pages",
    tags=["PDF Editor"],
    responses={200: {"content": {"application/pdf": {}}}},
)
def pdf_reorder_pages(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    page_order: str = Form(...),
    user_id: str = Depends(get_current_user_id_full_access),
):
    """page_order: JSON array of 0-indexed page numbers in the desired
    final order, e.g. "[2,0,1]". Omitting a page number deletes it."""
    parsed = _parse_json_form(page_order, "page_order")
    return _run_pdf_edit(background_tasks, file, reorder_pages, parsed)


@app.post(
    "/api/pdf-editor/watermark",
    tags=["PDF Editor"],
    responses={200: {"content": {"application/pdf": {}}}},
)
def pdf_watermark(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    text: str = Form(...),
    pages: str = Form("all"),
    font_size: float = Form(40),
    rotation: int = Form(45),
    opacity: float = Form(0.3),
    color: Optional[str] = Form(None),
    user_id: str = Depends(get_current_user_id_full_access),
):
    """pages: "all" or a JSON array of 0-indexed page numbers. color: JSON
    array [r,g,b], each 0-1, defaults to gray."""
    parsed_pages = _parse_pages_param(pages)
    parsed_color = _parse_json_form(color, "color") if color else None
    return _run_pdf_edit(
        background_tasks, file, add_watermark, text,
        pages=parsed_pages, font_size=font_size, rotation=rotation,
        opacity=opacity, color=parsed_color,
    )


@app.post(
    "/api/pdf-editor/rotate",
    tags=["PDF Editor"],
    responses={200: {"content": {"application/pdf": {}}}},
)
def pdf_rotate(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    pages: str = Form("all"),
    angle: int = Form(90),
    user_id: str = Depends(get_current_user_id_full_access),
):
    """pages: "all" or a JSON array of 0-indexed page numbers. angle: must
    be a multiple of 90, relative to each page's current rotation."""
    parsed_pages = _parse_pages_param(pages)
    return _run_pdf_edit(background_tasks, file, rotate_pages, pages=parsed_pages, angle=angle)


@app.post(
    "/api/pdf-editor/password",
    tags=["PDF Editor"],
    responses={200: {"content": {"application/pdf": {}}}},
)
def pdf_password(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    user_password: Optional[str] = Form(None),
    owner_password: Optional[str] = Form(None),
    restrict_printing: bool = Form(False),
    restrict_copying: bool = Form(False),
    user_id: str = Depends(get_current_user_id_full_access),
):
    """user_password: required to open the file at all. owner_password:
    required to change permissions (defaults to user_password if not set).
    At least one of the two must be provided."""
    return _run_pdf_edit(
        background_tasks, file, add_password,
        user_password=user_password, owner_password=owner_password,
        restrict_printing=restrict_printing, restrict_copying=restrict_copying,
    )


@app.post(
    "/api/pdf-editor/merge",
    tags=["PDF Editor"],
    responses={200: {"content": {"application/pdf": {}}}},
)
def pdf_merge(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    user_id: str = Depends(get_current_user_id_full_access),
):
    """Combines 2+ uploaded PDFs into one, in upload order."""
    if len(files) < 2:
        raise HTTPException(status_code=422, detail="merge needs at least 2 files")

    upload_dir = tempfile.mkdtemp(prefix="mizan_pdf_upload_")
    input_paths = []
    for i, f in enumerate(files):
        p = os.path.join(upload_dir, f"{i}_{f.filename or 'file.pdf'}")
        with open(p, "wb") as out:
            shutil.copyfileobj(f.file, out)
        input_paths.append(p)

    try:
        output_path = merge_pdfs(input_paths)
    except PDFEditError as exc:
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(exc))

    background_tasks.add_task(shutil.rmtree, upload_dir, ignore_errors=True)
    background_tasks.add_task(shutil.rmtree, os.path.dirname(output_path), ignore_errors=True)
    return FileResponse(
        output_path, media_type="application/pdf", filename="merged.pdf", background=background_tasks
    )


@app.post(
    "/api/pdf-editor/split",
    tags=["PDF Editor"],
    responses={200: {"content": {"application/pdf": {}}}},
)
def pdf_split(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    start_page: int = Form(...),
    end_page: int = Form(...),
    user_id: str = Depends(get_current_user_id_full_access),
):
    """Extracts the inclusive 0-indexed page range [start_page, end_page]
    into a new file."""
    return _run_pdf_edit(background_tasks, file, split_pdf, start_page, end_page)


@app.post(
    "/api/pdf-editor/redact",
    tags=["PDF Editor"],
    responses={200: {"content": {"application/pdf": {}}}},
)
def pdf_redact(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    redactions: str = Form(...),
    user_id: str = Depends(get_current_user_id_full_access),
):
    """redactions: JSON array, e.g.
    [{"page":0,"rect":[x0,y0,x1,y1],"fill":[0,0,0]}] — genuinely deletes
    the underlying text/content in each rect, not just a visual cover."""
    parsed = _parse_json_form(redactions, "redactions")
    return _run_pdf_edit(background_tasks, file, redact, parsed)


@app.post(
    "/api/pdf-editor/compress",
    tags=["PDF Editor"],
    responses={200: {"content": {"application/pdf": {}}}},
)
def pdf_compress(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user_id_full_access),
):
    """Reduces file size via redundant-object cleanup and stream/font/image
    recompression — no visual quality loss, no image downsampling."""
    return _run_pdf_edit(background_tasks, file, compress_pdf)
