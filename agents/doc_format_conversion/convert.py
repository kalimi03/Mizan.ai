"""
Mizan.ai — "Format conversion" (Feature H): DOCX/XLSX -> PDF via LibreOffice
headless.

Plain, framework-free function — no MCP wrapping, no async job (matches how
Translate v1 turned out: this is fast enough to be a normal synchronous
request/response, just without any Modal model call since LibreOffice runs
as a local subprocess, not a hosted model). Both gotchas fixed here don't
show up in casual single-request manual testing, which is exactly what
makes them dangerous — see docs/mizan_format_conversion_findings.md for the
full investigation.
"""

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import CONVERT_TIMEOUT_SECONDS, SOFFICE_BINARY, SUPPORTED_SOURCE_EXTENSIONS

logger = logging.getLogger(__name__)


class ConversionError(RuntimeError):
    """Raised for any conversion failure the caller should turn into an HTTP error."""


def _autofit_xlsx_columns(input_path: str, output_path: str) -> None:
    """LibreOffice's PDF export clips every cell strictly to its own column
    width — Excel/Calc's on-screen "overflow into the empty neighbor cell"
    display trick doesn't survive PDF export, so any text that only "fit"
    by overflowing gets silently cut off with no error or warning
    (confirmed: "Amount (SAR)" -> "Amount (S"). Widening columns to fit
    their longest value before conversion fixes that — confirmed via
    pdfplumber text extraction on the output.

    That alone isn't sufficient by itself, though (found testing beyond the
    findings doc's own narrower sample data): widening several columns for
    genuinely long content -- e.g. a real vendor name like "International
    Electronics Distribution Est." -- can push the total table width past
    the printable page width. LibreOffice doesn't wrap or shrink to fit in
    that case, it silently crops the rightmost columns off the page
    entirely, which is a worse failure than the original per-cell clipping
    since whole columns vanish with no error. fitToWidth=1 (this sheet's
    print output is scaled to fit one page wide, however many pages tall it
    needs) fixes that on top of the column widening -- confirmed together
    they keep every column visible on real multi-column, long-text data
    that reproduces the crop with fitToWidth alone left unset."""
    import openpyxl

    wb = openpyxl.load_workbook(input_path)
    for ws in wb.worksheets:
        for col_cells in ws.columns:
            values = [str(c.value) for c in col_cells if c.value is not None]
            if not values:
                continue
            length = max(len(v) for v in values)
            col_letter = col_cells[0].column_letter
            ws.column_dimensions[col_letter].width = length + 2

        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
    wb.save(output_path)


def convert_to_pdf(input_path: str, source_extension: str) -> str:
    """Converts a DOCX or XLSX file to PDF. Returns the path to the
    produced PDF, sitting in its own fresh temp directory — the caller owns
    cleaning that directory up once it's done reading the file (e.g. via a
    FastAPI BackgroundTask scheduled after the response is sent)."""
    source_extension = source_extension.lower()
    if source_extension not in SUPPORTED_SOURCE_EXTENSIONS:
        raise ConversionError(
            f"Unsupported source format {source_extension!r} — only "
            f"{sorted(SUPPORTED_SOURCE_EXTENSIONS)} are supported."
        )

    work_dir = tempfile.mkdtemp(prefix="mizan_convert_")
    # A LibreOffice headless instance's default user profile has a lock
    # shared across invocations — confirmed via a 3-way concurrent
    # ThreadPoolExecutor test that without an isolated profile per call,
    # 1 of 3 concurrent conversions fails SILENTLY: return code 1, empty
    # stdout, empty stderr, no output file, no error message at all. Every
    # request MUST get its own fresh profile dir; never reuse one across
    # concurrent calls.
    profile_dir = tempfile.mkdtemp(prefix="mizan_lo_profile_")
    try:
        convert_input = input_path
        if source_extension == ".xlsx":
            # Auto-fit on a COPY, not the original — the original is the
            # caller's uploaded file, not ours to modify.
            autofit_path = os.path.join(work_dir, "input" + source_extension)
            _autofit_xlsx_columns(input_path, autofit_path)
            convert_input = autofit_path

        try:
            result = subprocess.run(
                [
                    SOFFICE_BINARY, "--headless",
                    f"-env:UserInstallation=file://{Path(profile_dir).as_posix()}",
                    "--convert-to", "pdf",
                    "--outdir", work_dir,
                    convert_input,
                ],
                capture_output=True,
                timeout=CONVERT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise ConversionError(f"Conversion timed out after {CONVERT_TIMEOUT_SECONDS}s") from exc

        output_path = os.path.join(work_dir, Path(convert_input).stem + ".pdf")

        # Return code alone isn't trustworthy here (see the silent-failure
        # gotcha above) — check the output file actually exists too.
        if result.returncode != 0 or not os.path.exists(output_path):
            stderr = result.stderr.decode(errors="replace").strip()
            stdout = result.stdout.decode(errors="replace").strip()
            raise ConversionError(
                f"Conversion failed (return code {result.returncode}): "
                f"{stderr or stdout or 'no output produced, no error message'}"
            )

        return output_path
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)
