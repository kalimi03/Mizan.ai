"""
Mizan.ai — shared document extraction/format-routing logic.

Used by the data_extraction service (services/data_extraction/) for both
Feature E (features/calculator/) and Feature C (features/comparator/).
Docling/pandas are imported lazily inside functions, not at module level,
matching features/explainer/offline/extract.py's own convention —
keeps this module importable in the lightweight main gateway (e.g. for
detect_format's extension-only fast path) without requiring the heavy
extraction deps to be installed there.

v1 scope: PDF (native, via Docling), ZATCA XML/UBL 2.1, PDF/A-3 with an
embedded XML attachment, XLSX, CSV, plain text. Explicitly NOT handled here:
QR-code decoding, scanned/photocopied/handwritten OCR — both deferred to v2
per product decision. detect_format() actively rejects a scanned/
photographed PDF (no native text layer) with a clear "not supported"
ExtractionError rather than silently routing it into Docling's OCR path —
this module never OCRs, regardless of what the caller uploads.

Returns from every extract_*() function share one shape so downstream code
(tax-category classification, the calculation engine) doesn't need to know
which path produced the data:
    {
        "format_detected": str,
        "seller": {"name": str|None, "vat_number": str|None},
        "buyer": {"name": str|None, "vat_number": str|None},
        "issue_date": str|None,
        "line_items": [{"line_id": str, "description": str,
                         "taxable_base": float|None,
                         "tax_category_code": str|None,   # raw UBL code if present, e.g. "S"/"Z"/"E"
                         "vat_amount": float|None}],
        "totals": {"tax_exclusive_amount": float|None,
                    "tax_amount": float|None,
                    "tax_inclusive_amount": float|None},
        "tables": [...],   # raw Docling/pandas tables, only present for pdf/xlsx/csv paths —
                            # classification works off "line_items" when populated (ubl_xml path),
                            # otherwise the caller (features/calculator) is responsible for
                            # turning "tables"/"markdown" into line_items itself, since that mapping
                            # is exactly the judgment call QwenBrain classification exists for.
        "markdown": str|None,
    }

Classification (tax_category per line, when not already present in the
source) is NOT done here — that's QwenBrain's job, in the main gateway's
features/calculator module.
"""

import os
import re
import zipfile
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

SUPPORTED_EXTENSIONS = {".pdf", ".xml", ".xlsx", ".csv", ".txt"}

# Standard UBL 2.1 namespaces. ZATCA invoices are a profile of this schema.
_UBL_NS = {
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
}

# UNCL5305 tax category codes ZATCA invoices use — S=standard, Z=zero-rated,
# E=exempt. Passed through as the raw code; features/calculator maps
# these onto its own TaxCategory enum, not duplicated here.
_UBL_TAX_CATEGORY_CODES = {"S", "Z", "E", "O"}


class ExtractionError(RuntimeError):
    """Raised for any extraction failure the caller should turn into an HTTP error."""


def _empty_result(format_detected: str) -> Dict[str, Any]:
    return {
        "format_detected": format_detected,
        "seller": {"name": None, "vat_number": None},
        "buyer": {"name": None, "vat_number": None},
        "issue_date": None,
        "line_items": [],
        "totals": {"tax_exclusive_amount": None, "tax_amount": None, "tax_inclusive_amount": None},
        "tables": [],
        "markdown": None,
    }


# ---------------------------------------------------------------------------
# Format detection (Input Router — plain code, not an agent-callable step)
# ---------------------------------------------------------------------------


def _looks_like_ubl_xml(path: str) -> bool:
    """Cheap sniff: read the first ~2KB and check for the UBL Invoice root
    element / namespace, rather than fully parsing here."""
    try:
        with open(path, "rb") as f:
            head = f.read(2048)
    except OSError:
        return False
    return b"Invoice-2" in head or b"<Invoice" in head


def _find_embedded_xml_in_pdf(path: str) -> Optional[bytes]:
    """PDF/A-3 embedded-file attachments are stored as a zip-like structure
    inside the PDF's object tree. Rather than hand-rolling PDF object
    parsing, use PyMuPDF (already a repo dependency, see features/editor)
    to enumerate embedded files and return the first one that looks like
    UBL XML. Returns None if the PDF has no embedded XML attachment."""
    import fitz  # PyMuPDF

    try:
        doc = fitz.open(path)
    except Exception:
        return None
    try:
        for i in range(doc.embfile_count()):
            info = doc.embfile_info(i)
            name = (info.get("filename") or info.get("name") or "").lower()
            if name.endswith(".xml"):
                data = doc.embfile_get(i)
                if b"Invoice-2" in data[:4096] or b"<Invoice" in data[:4096]:
                    return data
        return None
    finally:
        doc.close()


def _is_scanned_pdf(path: str) -> bool:
    """A native/digitally-generated PDF has a real text layer; a scanned or
    photographed PDF is just page images with no extractable text. Checks
    the first few pages' native text layer via PyMuPDF (get_text(),
    without OCR) — near-empty across every sampled page means "no native
    text", i.e. scanned. Returns False (not scanned) on any error reading
    the file, so the real error surfaces from the normal extraction path
    instead of being masked here."""
    import fitz  # PyMuPDF

    try:
        doc = fitz.open(path)
    except Exception:
        return False
    try:
        if doc.page_count == 0:
            return False
        sample_pages = min(doc.page_count, 3)
        total_text = "".join(doc[i].get_text().strip() for i in range(sample_pages))
        return len(total_text) < 20
    finally:
        doc.close()


def detect_format(path: str, filename: str) -> str:
    """Returns one of: "ubl_xml", "pdf_embedded_xml", "pdf", "xlsx", "csv",
    "text". Raises ExtractionError for anything else, including a scanned
    or photographed PDF (same "not supported in this version" policy as
    an unsupported extension — see _is_scanned_pdf())."""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ExtractionError(
            f"Unsupported file format {ext!r} — accepted file formats are: "
            f"{', '.join(sorted(SUPPORTED_EXTENSIONS))} "
            "(scanned images and QR-code decoding are not supported in this version)"
        )

    if ext == ".xml":
        return "ubl_xml"
    if ext == ".pdf":
        if _find_embedded_xml_in_pdf(path) is not None:
            return "pdf_embedded_xml"
        if _is_scanned_pdf(path):
            raise ExtractionError(
                "This PDF appears to be a scanned or photographed image with no extractable "
                f"text — accepted file formats are: {', '.join(sorted(SUPPORTED_EXTENSIONS))} "
                "(scanned images and QR-code decoding are not supported in this version)"
            )
        return "pdf"
    if ext == ".xlsx":
        return "xlsx"
    if ext == ".csv":
        return "csv"
    return "text"


# ---------------------------------------------------------------------------
# ZATCA XML / UBL 2.1
# ---------------------------------------------------------------------------


def _text(el: Optional[ET.Element]) -> Optional[str]:
    return el.text.strip() if el is not None and el.text else None


def _float(el: Optional[ET.Element]) -> Optional[float]:
    t = _text(el)
    if t is None:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def extract_ubl_xml(path: str) -> Dict[str, Any]:
    """Parses a ZATCA-compliant UBL 2.1 Invoice XML file directly — no OCR,
    no Docling. Field mapping follows standard UBL 2.1 Invoice semantics
    (cac:AccountingSupplierParty/AccountingCustomerParty,
    cac:InvoiceLine, cac:LegalMonetaryTotal). NOTE: this has not yet been
    validated against a real ZATCA-issued sample invoice — the general UBL
    2.1 shape is well-established, but exact field presence/naming should
    be confirmed against a real sample during testing, not trusted purely
    on this implementation.
    """
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise ExtractionError(f"Could not parse XML: {exc}") from exc
    root = tree.getroot()
    ns = _UBL_NS

    result = _empty_result("ubl_xml")
    result["issue_date"] = _text(root.find("cbc:IssueDate", ns))

    supplier_party = root.find("cac:AccountingSupplierParty/cac:Party", ns)
    if supplier_party is not None:
        result["seller"]["name"] = _text(
            supplier_party.find("cac:PartyLegalEntity/cbc:RegistrationName", ns)
        )
        result["seller"]["vat_number"] = _text(
            supplier_party.find("cac:PartyTaxScheme/cbc:CompanyID", ns)
        )

    customer_party = root.find("cac:AccountingCustomerParty/cac:Party", ns)
    if customer_party is not None:
        result["buyer"]["name"] = _text(
            customer_party.find("cac:PartyLegalEntity/cbc:RegistrationName", ns)
        )
        result["buyer"]["vat_number"] = _text(
            customer_party.find("cac:PartyTaxScheme/cbc:CompanyID", ns)
        )

    for line in root.findall("cac:InvoiceLine", ns):
        item = line.find("cac:Item", ns)
        tax_category_el = None
        if item is not None:
            tax_category_el = item.find("cac:ClassifiedTaxCategory/cbc:ID", ns)
        tax_category_code = _text(tax_category_el)
        if tax_category_code is not None and tax_category_code not in _UBL_TAX_CATEGORY_CODES:
            tax_category_code = None  # unrecognized code — leave for classification, don't guess

        result["line_items"].append({
            "line_id": _text(line.find("cbc:ID", ns)) or "",
            "description": _text(item.find("cbc:Name", ns)) if item is not None else None,
            "taxable_base": _float(line.find("cbc:LineExtensionAmount", ns)),
            "tax_category_code": tax_category_code,
            "vat_amount": _float(line.find("cac:TaxTotal/cbc:TaxAmount", ns)),
        })

    monetary_total = root.find("cac:LegalMonetaryTotal", ns)
    if monetary_total is not None:
        result["totals"]["tax_exclusive_amount"] = _float(
            monetary_total.find("cbc:TaxExclusiveAmount", ns)
        )
        result["totals"]["tax_inclusive_amount"] = _float(
            monetary_total.find("cbc:TaxInclusiveAmount", ns)
        )
    result["totals"]["tax_amount"] = _float(root.find("cac:TaxTotal/cbc:TaxAmount", ns))

    return result


def extract_pdf_embedded_xml(path: str) -> Optional[Dict[str, Any]]:
    """PDF/A-3 case: extracts the embedded UBL XML attachment and parses it
    exactly like extract_ubl_xml. Returns None if no embedded XML is found
    (caller should fall back to plain extract_pdf in that case)."""
    xml_bytes = _find_embedded_xml_in_pdf(path)
    if xml_bytes is None:
        return None
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ExtractionError(f"Embedded PDF XML attachment could not be parsed: {exc}") from exc

    # Reuse extract_ubl_xml's field-mapping logic by writing the bytes to a
    # temp file rather than duplicating the ElementTree walk — keeps one
    # source of truth for the UBL field mapping.
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tmp:
        tmp.write(xml_bytes)
        tmp_path = tmp.name
    try:
        result = extract_ubl_xml(tmp_path)
        result["format_detected"] = "pdf_embedded_xml"
        return result
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# PDF (native, no embedded XML) / XLSX / CSV — reuse Feature A's extraction
# ---------------------------------------------------------------------------


def extract_pdf(path: str) -> Dict[str, Any]:
    """Native PDF extraction via Docling. Reuses
    features/explainer/offline/extract.py's extract_pdf() rather than
    reimplementing it — imported lazily so this module stays importable
    without Docling installed."""
    from features.explainer.offline.extract import extract_pdf as _extract_pdf

    raw = _extract_pdf(path)
    result = _empty_result("pdf")
    result["markdown"] = raw["markdown"]
    result["tables"] = raw["tables"]
    return result


# ZATCA VAT registration numbers: 15 digits, starting and ending with "3"
# (see features/calculator/config.py's VAT_NUMBER_LENGTH — duplicated as a
# literal here rather than imported, since this module stays importable
# without the calculator feature installed). Scoped to \b...\b so it only
# matches a standalone 15-digit run, not a substring of a longer number.
_VAT_NUMBER_RE = re.compile(r"\b3\d{13}3\b")


def _sniff_vat_number(tables: List[Dict[str, Any]]) -> Optional[str]:
    """xlsx/csv extraction never produces a structured "seller" block (that
    field only comes from parsing an actual UBL/PDF invoice) — but
    real-world spreadsheet exports (ledgers, VAT registers) often print the
    seller's VAT number in a title/metadata line above the real header,
    e.g. "Period: ... | Seller VAT No: 310123456700003". Those lines
    survive as each table's "preamble" (see extract.py's _preamble_lines())
    specifically so a caller can recover facts like this one — a VAT
    number's format is distinctive enough (15 digits, starts/ends with
    '3') that a plain regex is safe here, unlike guessing at buyer name or
    totals from free text, which would be too easy to get wrong."""
    for table in tables:
        for line in table.get("preamble") or []:
            match = _VAT_NUMBER_RE.search(line)
            if match:
                return match.group(0)
    return None


def extract_xlsx_or_csv(path: str, format_detected: str) -> Dict[str, Any]:
    """Reuses features/explainer/offline/extract.py's
    extract_xlsx_or_csv() — already branches internally on .csv vs .xlsx."""
    from features.explainer.offline.extract import extract_xlsx_or_csv as _extract_xlsx_or_csv

    tables = _extract_xlsx_or_csv(path)
    result = _empty_result(format_detected)
    result["tables"] = tables
    result["seller"]["vat_number"] = _sniff_vat_number(tables)
    return result


def extract_text(path: str) -> Dict[str, Any]:
    """Plain text file — read directly, no parsing library needed."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as exc:
        raise ExtractionError(f"Could not read text file: {exc}") from exc
    result = _empty_result("text")
    result["markdown"] = content
    return result


# ---------------------------------------------------------------------------
# Combined Input Router + Extraction entry point
# ---------------------------------------------------------------------------


def route_and_extract(path: str, filename: str) -> Dict[str, Any]:
    """The single entry point services/data_extraction/main.py's endpoint
    calls. Detects format, dispatches to the right extractor, returns the
    unified shape documented at the top of this module."""
    format_detected = detect_format(path, filename)

    if format_detected == "ubl_xml":
        return extract_ubl_xml(path)
    if format_detected == "pdf_embedded_xml":
        result = extract_pdf_embedded_xml(path)
        if result is not None:
            return result
        # Attachment vanished between detect and extract (shouldn't happen
        # in practice) — fall back to plain PDF rather than failing.
        return extract_pdf(path)
    if format_detected == "pdf":
        return extract_pdf(path)
    if format_detected in ("xlsx", "csv"):
        return extract_xlsx_or_csv(path, format_detected)
    if format_detected == "text":
        return extract_text(path)

    raise ExtractionError(f"Unhandled format_detected value: {format_detected!r}")
