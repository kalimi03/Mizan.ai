"""
Mizan.ai — "PDF editor" (Tier 1): overlay-only PDF operations via PyMuPDF.

Every operation here only overlays or manipulates the PDF at the page/object
level — none of them touch or reconstruct existing embedded text content
(true in-place text editing is explicitly out of scope, see config.py).
Plain functions, no MCP wrapping, no async job — matches the shape of
convert_to_pdf() in features/converter/convert.py: takes an input
path, returns an output path sitting in its own fresh temp directory that
the caller owns cleaning up.

PyMuPDF (fitz) bundles MuPDF as a compiled wheel — no LibreOffice-style
system package/subprocess concerns, no concurrency lock-contention gotcha
like the format converter had.
"""

import logging
import os
import shutil
import tempfile
from typing import List, Optional

import fitz  # PyMuPDF
import numpy as np
from PIL import Image, ImageEnhance

from .config import SUPPORTED_SOURCE_EXTENSIONS

logger = logging.getLogger(__name__)


class PDFEditError(RuntimeError):
    """Raised for any edit failure the caller should turn into an HTTP error."""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _validate_pdf(input_path: str) -> None:
    ext = os.path.splitext(input_path)[1].lower()
    if ext not in SUPPORTED_SOURCE_EXTENSIONS:
        raise PDFEditError(f"Unsupported source format {ext!r} — only .pdf is supported.")


def _open_pdf(input_path: str) -> fitz.Document:
    _validate_pdf(input_path)
    try:
        return fitz.open(input_path)
    except Exception as exc:
        raise PDFEditError(f"Could not open PDF: {exc}") from exc


def _new_output_path(input_path: str, suffix: str = "") -> str:
    """Fresh temp dir per call, same ownership contract as convert_to_pdf —
    caller cleans this directory up once done reading the file."""
    work_dir = tempfile.mkdtemp(prefix="mizan_pdf_edit_")
    stem = os.path.splitext(os.path.basename(input_path))[0]
    return os.path.join(work_dir, f"{stem}{suffix}.pdf")


def _save(doc: fitz.Document, output_path: str, **save_kwargs) -> str:
    try:
        doc.save(output_path, **save_kwargs)
    except Exception as exc:
        shutil.rmtree(os.path.dirname(output_path), ignore_errors=True)
        raise PDFEditError(f"Could not save edited PDF: {exc}") from exc
    finally:
        doc.close()
    return output_path


def _resolve_pages(doc: fitz.Document, pages) -> List[int]:
    """pages is either the literal string "all" or a list of 0-indexed page
    numbers. Validates bounds either way."""
    if pages == "all":
        return list(range(doc.page_count))
    if not isinstance(pages, list) or not pages:
        raise PDFEditError('pages must be "all" or a non-empty list of 0-indexed page numbers')
    for p in pages:
        if not isinstance(p, int) or p < 0 or p >= doc.page_count:
            raise PDFEditError(f"Page index {p} out of range (document has {doc.page_count} pages)")
    return pages


# ---------------------------------------------------------------------------
# Highlighting / annotations / sticky notes / shapes / freehand
# ---------------------------------------------------------------------------


def _apply_annotation(page: fitz.Page, ann: dict) -> None:
    ann_type = ann.get("type")
    color = tuple(ann.get("color", [1, 1, 0]))  # default yellow

    if ann_type == "highlight":
        rect = fitz.Rect(ann["rect"])
        annot = page.add_highlight_annot(rect)
        annot.set_colors(stroke=color)
        annot.update()

    elif ann_type == "note":
        point = fitz.Point(ann["point"])
        annot = page.add_text_annot(point, ann.get("text", ""))
        annot.set_colors(stroke=color)
        annot.update()

    elif ann_type in ("rect", "circle"):
        rect = fitz.Rect(ann["rect"])
        annot = page.add_rect_annot(rect) if ann_type == "rect" else page.add_circle_annot(rect)
        fill = ann.get("fill")
        annot.set_colors(stroke=color, fill=tuple(fill) if fill else None)
        annot.set_opacity(ann.get("opacity", 1.0))
        annot.update()

    elif ann_type == "line":
        p1, p2 = ann["points"]
        annot = page.add_line_annot(fitz.Point(p1), fitz.Point(p2))
        annot.set_colors(stroke=color)
        annot.update()

    elif ann_type == "freehand":
        strokes = [[tuple(pt) for pt in stroke] for stroke in ann["strokes"]]
        annot = page.add_ink_annot(strokes)
        annot.set_colors(stroke=color)
        annot.update()

    else:
        raise PDFEditError(
            f"Unknown annotation type {ann_type!r} — expected one of: "
            "highlight, note, rect, circle, line, freehand"
        )


def add_annotations(input_path: str, annotations: List[dict]) -> str:
    """annotations: list of dicts, each shaped per _apply_annotation()'s
    supported types above. "page" is required and 0-indexed on every item."""
    if not annotations:
        raise PDFEditError("annotations list must not be empty")

    doc = _open_pdf(input_path)
    try:
        for ann in annotations:
            page_num = ann.get("page")
            if not isinstance(page_num, int) or page_num < 0 or page_num >= doc.page_count:
                raise PDFEditError(f"Annotation has invalid/missing page index: {ann}")
            _apply_annotation(doc[page_num], ann)
    except PDFEditError:
        doc.close()
        raise
    except Exception as exc:
        doc.close()
        raise PDFEditError(f"Failed applying annotation: {exc}") from exc

    return _save(doc, _new_output_path(input_path, "_annotated"))


# ---------------------------------------------------------------------------
# Text boxes (overlay, not an edit of existing text)
# ---------------------------------------------------------------------------


def add_text_boxes(input_path: str, text_boxes: List[dict]) -> str:
    """text_boxes: list of {"page": int, "rect": [x0,y0,x1,y1], "text": str,
    "font_size": float=11, "color": [r,g,b]=[0,0,0]}"""
    if not text_boxes:
        raise PDFEditError("text_boxes list must not be empty")

    doc = _open_pdf(input_path)
    try:
        for box in text_boxes:
            page_num = box.get("page")
            if not isinstance(page_num, int) or page_num < 0 or page_num >= doc.page_count:
                raise PDFEditError(f"Text box has invalid/missing page index: {box}")
            rect = fitz.Rect(box["rect"])
            color = tuple(box.get("color", [0, 0, 0]))
            doc[page_num].insert_textbox(
                rect, box["text"], fontsize=box.get("font_size", 11), color=color
            )
    except PDFEditError:
        doc.close()
        raise
    except Exception as exc:
        doc.close()
        raise PDFEditError(f"Failed inserting text box: {exc}") from exc

    return _save(doc, _new_output_path(input_path, "_textboxes"))


# ---------------------------------------------------------------------------
# Form filling (existing AcroForm fields only)
# ---------------------------------------------------------------------------


def fill_form(input_path: str, fields: dict) -> str:
    """fields: {"field_name": value}. Only fills fields that already exist
    on the PDF (AcroForm templates) — does not create new fields. Raises if
    any named field isn't found, so a typo'd field name fails loudly rather
    than silently doing nothing."""
    if not fields:
        raise PDFEditError("fields dict must not be empty")

    doc = _open_pdf(input_path)
    try:
        found_names = set()
        for page in doc:
            for widget in page.widgets():
                if widget.field_name in fields:
                    widget.field_value = fields[widget.field_name]
                    widget.update()
                    found_names.add(widget.field_name)

        missing = set(fields) - found_names
        if missing:
            raise PDFEditError(f"Form field(s) not found in this PDF: {sorted(missing)}")
    except PDFEditError:
        doc.close()
        raise
    except Exception as exc:
        doc.close()
        raise PDFEditError(f"Failed filling form: {exc}") from exc

    return _save(doc, _new_output_path(input_path, "_filled"))


# ---------------------------------------------------------------------------
# Page preview (rasterized to PNG) — lets the frontend show the actual page
# and let the user draw a rect on it, instead of typing point coordinates
# by hand. Returns the page's real size in points alongside the image
# bytes so the caller can convert on-screen pixel positions back to PDF
# points without needing any PDF-rendering capability of its own.
# ---------------------------------------------------------------------------


def render_page_preview(input_path: str, page: int, zoom: float = 1.5) -> tuple:
    doc = _open_pdf(input_path)
    try:
        if page < 0 or page >= doc.page_count:
            raise PDFEditError(f"Page index {page} out of range (document has {doc.page_count} pages)")
        pg = doc[page]
        pixmap = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        return pixmap.tobytes("png"), pg.rect.width, pg.rect.height
    except PDFEditError:
        raise
    except Exception as exc:
        raise PDFEditError(f"Failed rendering page preview: {exc}") from exc
    finally:
        doc.close()


def _make_signature_transparent(image_path: str, darkness: float = 1.0) -> str:
    """Turns a photo/scan of a signature on plain paper into a transparent
    PNG, so only the ink is placed on the page rather than a solid
    rectangle of paper background.

    Background color is sampled from the image's own corners (not assumed
    to be pure white) — this is a color-distance threshold, not a plain
    grayscale/brightness one, so it still works on off-white or tinted
    paper. Pixels are faded smoothly between two distance thresholds
    (rather than a hard cutoff) so stroke edges/anti-aliasing don't come
    out jagged. Finally crops to the signature's own bounding box, trimming
    the blank paper margin. Assumes a plain, evenly lit background — see
    the frontend's own note to the user about this constraint.

    darkness: 1.0 = as photographed. Actually a contrast adjustment, not a
    plain brightness one — uniformly darkening/lightening an image scales
    the ink-vs-paper color gap down just as much as it scales the whole
    image down, so it doesn't change what gets detected. Contrast instead
    pushes ink and paper apart (or together) relative to the image's own
    mean tone: >1 widens that gap (helps a too-light/faint signature
    register as ink instead of getting thresholded away as background);
    <1 narrows it (helps a too-dark/heavy source stop swallowing fine
    stroke detail into a blob).
    """
    img = Image.open(image_path).convert("RGB")
    if darkness != 1.0:
        img = ImageEnhance.Contrast(img).enhance(darkness)
    arr = np.asarray(img, dtype=np.float64)

    corner = max(1, min(arr.shape[0], arr.shape[1]) // 20)
    corners = np.concatenate([
        arr[:corner, :corner].reshape(-1, 3),
        arr[:corner, -corner:].reshape(-1, 3),
        arr[-corner:, :corner].reshape(-1, 3),
        arr[-corner:, -corner:].reshape(-1, 3),
    ])
    background_color = corners.mean(axis=0)

    distance = np.linalg.norm(arr - background_color, axis=2)
    low, high = 25.0, 70.0  # below low = background, above high = ink, between = soft fade
    alpha = np.clip((distance - low) / (high - low), 0.0, 1.0) * 255

    rgba = np.dstack([arr, alpha]).astype(np.uint8)
    out = Image.fromarray(rgba, mode="RGBA")

    visible_rows = np.where(alpha.max(axis=1) > 0)[0]
    visible_cols = np.where(alpha.max(axis=0) > 0)[0]
    if len(visible_rows) and len(visible_cols):
        pad = 4
        top = max(0, int(visible_rows[0]) - pad)
        bottom = min(arr.shape[0], int(visible_rows[-1]) + pad + 1)
        left = max(0, int(visible_cols[0]) - pad)
        right = min(arr.shape[1], int(visible_cols[-1]) + pad + 1)
        out = out.crop((left, top, right, bottom))

    output_path = os.path.join(tempfile.mkdtemp(prefix="mizan_signature_"), "signature_transparent.png")
    out.save(output_path)
    return output_path


def add_signature(input_path: str, page: int, rect: List[float], image_path: str, darkness: float = 1.0) -> str:
    """Places an uploaded signature image (drawn or scanned) on one page at
    the given rect [x0,y0,x1,y1] — background removed first (see
    _make_signature_transparent) so only the ink shows over the page
    content underneath, not a solid rectangle of paper."""
    doc = _open_pdf(input_path)
    try:
        if page < 0 or page >= doc.page_count:
            raise PDFEditError(f"Page index {page} out of range (document has {doc.page_count} pages)")
        transparent_image_path = _make_signature_transparent(image_path, darkness=darkness)
        doc[page].insert_image(fitz.Rect(rect), filename=transparent_image_path)
    except PDFEditError:
        doc.close()
        raise
    except Exception as exc:
        doc.close()
        raise PDFEditError(f"Failed placing signature: {exc}") from exc

    return _save(doc, _new_output_path(input_path, "_signed"))


# ---------------------------------------------------------------------------
# Page reordering / deletion
# ---------------------------------------------------------------------------


def reorder_pages(input_path: str, page_order: List[int]) -> str:
    """page_order: 0-indexed page numbers in the desired final order.
    Omitting a page number deletes it — this covers both "reorder" and
    "delete individual pages" from the same operation, since PyMuPDF's
    Document.select() already works that way."""
    if not page_order:
        raise PDFEditError("page_order must not be empty")

    doc = _open_pdf(input_path)
    try:
        for p in page_order:
            if not isinstance(p, int) or p < 0 or p >= doc.page_count:
                raise PDFEditError(f"Page index {p} out of range (document has {doc.page_count} pages)")
        doc.select(page_order)
    except PDFEditError:
        doc.close()
        raise
    except Exception as exc:
        doc.close()
        raise PDFEditError(f"Failed reordering pages: {exc}") from exc

    return _save(doc, _new_output_path(input_path, "_reordered"), garbage=4, deflate=True)


# ---------------------------------------------------------------------------
# Watermarking / stamping
# ---------------------------------------------------------------------------


def add_watermark(
    input_path: str,
    text: str,
    pages="all",
    font_size: float = 40,
    rotation: int = 45,
    opacity: float = 0.3,
    color: Optional[List[float]] = None,
) -> str:
    """Diagonal text watermark (e.g. "DRAFT", a logo-text stand-in) stamped
    across the given pages. Pure overlay via insert_textbox — same effort
    class as add_text_boxes(), just applied page-wide with opacity/rotation.
    insert_textbox's own `rotate` kwarg only accepts multiples of 90 — an
    arbitrary angle (the default here is 45, a typical diagonal watermark)
    needs the `morph` kwarg instead (center point + rotation matrix).

    The box passed to insert_textbox is deliberately sized to the text
    itself (via get_text_length), not the full page rect — confirmed via
    direct testing that centering a single line inside a much larger box
    (e.g. the whole page) is unreliable on a PDF that's been saved and
    reopened (as every real upload here is): the computed line height
    needed came out different enough between a fresh in-memory document and
    a reopened one that the same box height which fit fine in-memory
    reported negative "spare" (insert_textbox's own fit-failure signal) —
    and the width-only version of that same gap silently clipped the
    watermark's leading character instead of failing loudly. A generously
    padded, text-sized box (2.2x fontsize tall) tested clean across a range
    of rotations/fontsizes on reopened documents and leaves comfortable
    positive spare in every case."""
    doc = _open_pdf(input_path)
    try:
        target_pages = _resolve_pages(doc, pages)
        fill = tuple(color or [0.5, 0.5, 0.5])
        text_width = fitz.get_text_length(text, fontname="helv", fontsize=font_size)
        box_w = text_width + 20
        box_h = font_size * 2.2
        for page_num in target_pages:
            page = doc[page_num]
            cx = (page.rect.x0 + page.rect.x1) / 2
            cy = (page.rect.y0 + page.rect.y1) / 2
            box = fitz.Rect(cx - box_w / 2, cy - box_h / 2, cx + box_w / 2, cy + box_h / 2)
            center = fitz.Point(cx, cy)
            page.insert_textbox(
                box,
                text,
                fontsize=font_size,
                color=fill,
                fill_opacity=opacity,
                morph=(center, fitz.Matrix(rotation)),
                align=fitz.TEXT_ALIGN_CENTER,
            )
    except PDFEditError:
        doc.close()
        raise
    except Exception as exc:
        doc.close()
        raise PDFEditError(f"Failed adding watermark: {exc}") from exc

    return _save(doc, _new_output_path(input_path, "_watermarked"))


# ---------------------------------------------------------------------------
# Page rotation
# ---------------------------------------------------------------------------


def rotate_pages(input_path: str, pages="all", angle: int = 90) -> str:
    """angle: relative rotation in degrees, added to each page's current
    rotation (e.g. 90, 180, 270, -90). Must be a multiple of 90 — PDF page
    rotation is always axis-aligned."""
    if angle % 90 != 0:
        raise PDFEditError("angle must be a multiple of 90")

    doc = _open_pdf(input_path)
    try:
        target_pages = _resolve_pages(doc, pages)
        for page_num in target_pages:
            page = doc[page_num]
            page.set_rotation((page.rotation + angle) % 360)
    except PDFEditError:
        doc.close()
        raise
    except Exception as exc:
        doc.close()
        raise PDFEditError(f"Failed rotating pages: {exc}") from exc

    return _save(doc, _new_output_path(input_path, "_rotated"))


# ---------------------------------------------------------------------------
# Password protection (Tier 1 quick-win, pulled forward from Tier 2)
# ---------------------------------------------------------------------------


def add_password(
    input_path: str,
    user_password: Optional[str] = None,
    owner_password: Optional[str] = None,
    restrict_printing: bool = False,
    restrict_copying: bool = False,
) -> str:
    """user_password: required to open the file at all (None/empty = anyone
    can open). owner_password: required to change permissions (defaults to
    user_password if not given, so at least SOME password gates
    permissions whenever an open password is set). restrict_printing/
    restrict_copying: permission flags, only meaningful once an
    owner_password is actually set — an unprotected owner password makes
    permission flags trivially bypassable."""
    if not user_password and not owner_password:
        raise PDFEditError("At least one of user_password or owner_password must be set")

    doc = _open_pdf(input_path)
    try:
        perm = fitz.PDF_PERM_ANNOTATE | fitz.PDF_PERM_FORM | fitz.PDF_PERM_ACCESSIBILITY
        if not restrict_printing:
            perm |= fitz.PDF_PERM_PRINT
        if not restrict_copying:
            perm |= fitz.PDF_PERM_COPY
    except Exception as exc:
        doc.close()
        raise PDFEditError(f"Failed computing permissions: {exc}") from exc

    return _save(
        doc,
        _new_output_path(input_path, "_protected"),
        encryption=fitz.PDF_ENCRYPT_AES_256,
        user_pw=user_password or "",
        owner_pw=owner_password or user_password,
        permissions=perm,
    )


# ---------------------------------------------------------------------------
# Tier 2 — merge, split, redaction, compress. Parked-for-later per the
# scoping doc, picked up once Tier 1 shipped and tested. Delete-invoice-
# line-item is deliberately NOT here — the scoping doc itself flags it as
# touching document structure/logic (table reflow, total recalculation),
# not just visuals, and needs a design conversation before implementation,
# not a guess.
# ---------------------------------------------------------------------------


def merge_pdfs(input_paths: List[str]) -> str:
    """Combines multiple PDFs into one, in the given order."""
    if not input_paths or len(input_paths) < 2:
        raise PDFEditError("merge_pdfs needs at least 2 input files")
    for p in input_paths:
        _validate_pdf(p)

    merged = fitz.open()
    try:
        for p in input_paths:
            src = fitz.open(p)
            merged.insert_pdf(src)
            src.close()
    except Exception as exc:
        merged.close()
        raise PDFEditError(f"Failed merging PDFs: {exc}") from exc

    return _save(merged, _new_output_path(input_paths[0], "_merged"))


def split_pdf(input_path: str, start_page: int, end_page: int) -> str:
    """Extracts the inclusive 0-indexed page range [start_page, end_page]
    into a new file."""
    doc = _open_pdf(input_path)
    try:
        if not (0 <= start_page <= end_page < doc.page_count):
            raise PDFEditError(
                f"Invalid page range [{start_page}, {end_page}] for a "
                f"{doc.page_count}-page document"
            )
        extracted = fitz.open()
        extracted.insert_pdf(doc, from_page=start_page, to_page=end_page)
    except PDFEditError:
        doc.close()
        raise
    except Exception as exc:
        doc.close()
        raise PDFEditError(f"Failed splitting PDF: {exc}") from exc
    doc.close()

    return _save(extracted, _new_output_path(input_path, f"_pages_{start_page}-{end_page}"))


def redact(input_path: str, redactions: List[dict]) -> str:
    """redactions: list of {"page": int, "rect": [x0,y0,x1,y1], "fill":
    [r,g,b]=[0,0,0]}. Genuinely deletes the underlying text/content stream
    within each rect (confirmed via direct testing: redacted text does not
    survive re-extraction) — not just a black box drawn on top, which is
    what makes this Tier 2 rather than a trivial overlay like a rect
    annotation."""
    if not redactions:
        raise PDFEditError("redactions list must not be empty")

    doc = _open_pdf(input_path)
    try:
        pages_touched = set()
        for r in redactions:
            page_num = r.get("page")
            if not isinstance(page_num, int) or page_num < 0 or page_num >= doc.page_count:
                raise PDFEditError(f"Redaction has invalid/missing page index: {r}")
            rect = fitz.Rect(r["rect"])
            fill = tuple(r.get("fill", [0, 0, 0]))
            doc[page_num].add_redact_annot(rect, fill=fill)
            pages_touched.add(page_num)

        # apply_redactions() must run per-page, after all annotations on
        # that page are added — running it immediately per-annotation would
        # still work here but doing it once per touched page is cheaper for
        # documents with several redactions on the same page.
        for page_num in pages_touched:
            doc[page_num].apply_redactions()
    except PDFEditError:
        doc.close()
        raise
    except Exception as exc:
        doc.close()
        raise PDFEditError(f"Failed applying redaction: {exc}") from exc

    return _save(doc, _new_output_path(input_path, "_redacted"), garbage=4, deflate=True)


def compress_pdf(input_path: str) -> str:
    """Reduces file size via redundant-object garbage collection and
    stream/font/image recompression — safe, always-beneficial, no visual
    quality loss. Does NOT downsample embedded images to a lower DPI (that
    would need a real quality/size tradeoff decision and adds a PIL
    dependency); per the scoping doc this wasn't something users had asked
    for directly — revisit with image downsampling if scanned-file-size
    complaints come up."""
    doc = _open_pdf(input_path)
    return _save(
        doc,
        _new_output_path(input_path, "_compressed"),
        garbage=4,
        clean=True,
        deflate=True,
        deflate_images=True,
        deflate_fonts=True,
        compression_effort=100,
    )
