"""
Mizan.ai — "PDF editor" config.

Companion to the format converter (Feature H): basic PDF editing (annotate,
fill forms, reorder/rotate pages, watermark, password-protect) so users
don't have to leave Mizan.ai for simple edits. Deliberately narrow scope —
see docs/mizan_pdf_editor_scoping_handoff.pdf for the full Tier 1/Tier 2/
out-of-scope breakdown this module is built from. True in-place editing of
existing PDF text content is explicitly out of scope (PDFs store positioned
glyphs, not structured paragraphs).
"""

SUPPORTED_SOURCE_EXTENSIONS = {".pdf"}
