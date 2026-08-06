"""
Mizan.ai — "Format conversion" (Feature H) config.

v1 scope only: DOCX -> PDF and XLSX -> PDF, via LibreOffice headless. PDF ->
DOCX is explicitly out of scope — LibreOffice imports a PDF as a Draw
document (positioned shapes/text boxes), which has no Writer-format export
filter at all, so it's a hard failure, not a quality problem. If PDF -> DOCX
is ever needed, it needs a different tool entirely (pdf2docx or Docling),
not this pipeline. See docs/mizan_format_conversion_findings.md for the
full investigation this module is built from.
"""

import os

SUPPORTED_SOURCE_EXTENSIONS = {".docx", ".xlsx"}

# LibreOffice can hang on a malformed or huge input -- confirmed single-file
# conversions complete in ~3.5s cold, so 120s is generous headroom, not a
# tight budget.
CONVERT_TIMEOUT_SECONDS = int(os.getenv("MIZAN_CONVERT_TIMEOUT_SECONDS", "120"))

SOFFICE_BINARY = os.getenv("MIZAN_SOFFICE_BINARY", "soffice")
