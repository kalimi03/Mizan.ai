"""
Mizan.ai — VAT Compliance Center, job 1: batch invoice compliance check.

Per invoice: extract for real (doc-extraction service), check the fields
extraction can actually produce, recompute VAT independently and compare
against whatever totals were printed, and decide clean vs. flagged. No
human is in the loop per-invoice — this runs the whole batch
automatically, and only flagged invoices ever surface for a person to look
at (see services/calculator/main.py's report endpoint).

Deliberately does NOT reuse structural_rules.py's run_structural_validation
wholesale: that orchestrator's required-field set (seller_id, buyer_id,
identity_number) was built for the old wizard, where a human typed those
in by hand on a form — none of them are ever extracted automatically, so
calling it as-is would flag "missing" on every single invoice regardless
of whether it's actually fine. Instead, this reuses the constituent checks
that genuinely apply to extracted data (VAT number format, date sanity,
seller/buyer VAT numbers not identical) and adds a small essentials check
of its own for what extraction is actually expected to find.
"""

import logging
import os
import shutil
import tempfile
from datetime import date as _date
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from features.calculator.config import DOC_EXTRACTION_URL, MISMATCH_TOLERANCE, QWEN_BRAIN_URL
from features.calculator.graph import ZatcaCalculatorAgent
from features.calculator.structural_rules import (
    validate_cross_field_consistency,
    validate_dates,
    validate_vat_number_format,
)
from features.calculator.tools import ToolInputError
from features.calculator.tools import validate_zatca_form as run_validate_zatca_form
from features.common.http_client import InternalServiceError, post_file
from features.common.modal_client import ModalEndpointError, call_modal_json
from features.common.text_cleanup import strip_latex_math

logger = logging.getLogger(__name__)

# S=standard, Z=zero-rated, E=exempt per the UNCL5305 codes document_extraction.py
# passes through raw (see its _UBL_TAX_CATEGORY_CODES). "O" and anything
# unrecognized has no safe direct mapping — left for classify_async to
# suggest, same as a blank tax_category already works elsewhere in this repo.
_UBL_CODE_TO_TAX_CATEGORY = {"S": "standard", "Z": "zero_rated", "E": "exempt"}

_agent = ZatcaCalculatorAgent()


def _issue(rule_id: str, field: Optional[str], severity: str, message: str) -> Dict[str, Any]:
    return {"rule_id": rule_id, "field": field, "severity": severity, "message": message}


def _essential_fields_issues(extraction: dict) -> List[Dict[str, Any]]:
    """What a real invoice must carry, restricted to what extraction can
    actually find — distinct from structural_rules.py's required-field
    check, which assumes a human filled in fields extraction never
    produces (see module docstring)."""
    issues = []
    seller = extraction.get("seller") or {}
    buyer = extraction.get("buyer") or {}
    if not seller.get("vat_number"):
        issues.append(_issue("missing_seller_vat_number", "seller.vat_number", "error",
                              "Could not find the seller's VAT number on this invoice."))
    if not extraction.get("issue_date"):
        issues.append(_issue("missing_issue_date", "issue_date", "error",
                              "Could not find an issue date on this invoice."))
    if not buyer.get("name") and not buyer.get("vat_number"):
        issues.append(_issue("missing_buyer_identity", "buyer", "warning",
                              "Could not identify who this invoice was issued to."))
    return issues


def _parse_issue_date(raw: Optional[str]) -> Optional[_date]:
    if not raw:
        return None
    try:
        return _date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _structural_issues(extraction: dict) -> List[Dict[str, Any]]:
    seller = extraction.get("seller") or {}
    buyer = extraction.get("buyer") or {}

    issues = _essential_fields_issues(extraction)
    for i in validate_vat_number_format(seller.get("vat_number")):
        issues.append(_issue(i.rule_id, i.field, i.severity, f"Seller — {i.message}"))
    # Buyer VAT number format — checked only when one is present at all.
    # ZATCA's Simplified (B2C) tax invoices legitimately carry no buyer VAT
    # number, so its absence is not itself an issue (see
    # _essential_fields_issues' separate, softer "missing_buyer_identity"
    # warning) — this only fires when a buyer VAT number IS present but
    # malformed, which previously went unchecked entirely (found via live
    # testing: a malformed buyer VAT number sailed through as "clean").
    for i in validate_vat_number_format(buyer.get("vat_number")):
        issues.append(_issue("invalid_buyer_vat_number_format", "buyer.vat_number", i.severity, f"Buyer — {i.message}"))

    issue_date = _parse_issue_date(extraction.get("issue_date"))
    if issue_date is not None:
        for i in validate_dates(issue_date):
            issues.append(_issue(i.rule_id, i.field, i.severity, i.message))

    for i in validate_cross_field_consistency(seller.get("vat_number"), buyer.get("vat_number")):
        issues.append(_issue(i.rule_id, i.field, i.severity, i.message))

    return issues


def _resolve_line_items(line_items: List[dict]) -> List[dict]:
    """Maps each line's raw UBL tax_category_code onto our TaxCategory
    enum; anything missing/unrecognized gets a QwenBrain suggestion (same
    "model's judgment wins" classify_line_items path the old wizard used),
    falling back to "standard" — this app's own established default for a
    genuinely uncertain case (see graph.py's _CLASSIFICATION_GUIDANCE) —
    only if classification itself is unavailable, so a check never crashes
    for lack of a category."""
    resolved = [dict(li) for li in line_items]
    unresolved = [li for li in resolved if _UBL_CODE_TO_TAX_CATEGORY.get(li.get("tax_category_code")) is None]

    suggestions: Dict[str, str] = {}
    if unresolved:
        try:
            result = _agent.classify(
                [{"line_id": li.get("line_id"), "description": li.get("description")} for li in unresolved],
            )
            suggestions = {c["line_id"]: c["tax_category"] for c in result.get("classifications", [])}
        except Exception as exc:  # classification is best-effort — never block the check on it
            logger.warning("classify_async failed during invoice check: %s", exc)

    for li in resolved:
        mapped = _UBL_CODE_TO_TAX_CATEGORY.get(li.get("tax_category_code"))
        li["tax_category"] = mapped or suggestions.get(li.get("line_id")) or "standard"
    return resolved


def _narrate_flagged_invoice(structural_issues: List[dict], mismatches: List[dict]) -> Optional[str]:
    """A single direct QwenBrain call (no MCP, no graph.py) that narrates
    the COMPLETE set of findings for a flagged invoice together —
    structural issues AND numeric mismatches in one prompt. Deliberately
    NOT graph.py's ZatcaCalculatorAgent.run(): that narrates only the
    validate_zatca_form tool result, with zero visibility into structural
    issues computed separately in this module — confirmed live that this
    produced a genuinely misleading explanation ("no mismatches found...
    correct") on an invoice that was flagged purely for a malformed VAT
    number, since the narration had no idea that issue existed. Explicitly
    instructing the model not to call anything "correct" while findings
    are listed is the fix, not just adding more context and hoping."""
    if not QWEN_BRAIN_URL:
        return None

    findings = [f"- [{i['severity']}] {i['message']}" for i in structural_issues]
    findings += [
        f"- {m['field']}: document shows {m['document_value']}, recalculated value is {m['recalculated_value']}"
        for m in mismatches
    ]
    if not findings:
        return None

    prompt = (
        "An invoice was flagged by an automated VAT compliance check. Below is the COMPLETE list of "
        "everything found wrong with it. Write a brief, plain-language explanation covering every item "
        "below. Do not say the invoice is correct, compliant, or that there are no issues — every line "
        "below is a real problem that needs the reader's attention.\n\n" + "\n".join(findings)
    )
    try:
        response = call_modal_json(QWEN_BRAIN_URL, {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 256,
            "temperature": 0.1,
        }, timeout=120)
    except ModalEndpointError as exc:
        logger.warning("Narration failed: %s", exc)
        return None
    return strip_latex_math(response.get("content")) or None


def _is_significant_mismatch(m: dict, tolerance: Decimal) -> bool:
    """A mismatch within tolerance is harmless rounding-methodology noise,
    not a real discrepancy — see config.py's MISMATCH_TOLERANCE (the
    default, used when the caller doesn't set their own). Unparseable
    values fail toward "significant" rather than silently disappearing."""
    try:
        diff = abs(Decimal(str(m["document_value"])) - Decimal(str(m["recalculated_value"])))
    except (InvalidOperation, TypeError, KeyError):
        return True
    return diff > tolerance


def _extract_one(filename: str, path: str) -> dict:
    if not DOC_EXTRACTION_URL:
        raise InternalServiceError("MIZAN_DOC_EXTRACTION_URL is not configured")
    return post_file(f"{DOC_EXTRACTION_URL}/extract", path, filename, timeout=120)


def _check_one_invoice(filename: str, path: str, tolerance: Decimal = MISMATCH_TOLERANCE) -> Dict[str, Any]:
    try:
        extraction = _extract_one(filename, path)
    except InternalServiceError as exc:
        return {
            "filename": filename,
            "status": "flagged",
            "extracted": {},
            "structural_issues": [_issue("extraction_failed", None, "error", str(exc))],
            "mismatches": [],
            "explanation": None,
        }

    seller = extraction.get("seller") or {}
    buyer = extraction.get("buyer") or {}
    totals = extraction.get("totals") or {}
    extracted_summary = {
        "seller_name": seller.get("name"), "seller_vat_number": seller.get("vat_number"),
        "buyer_name": buyer.get("name"), "issue_date": extraction.get("issue_date"),
        "tax_exclusive_amount": totals.get("tax_exclusive_amount"),
        "tax_amount": totals.get("tax_amount"), "tax_inclusive_amount": totals.get("tax_inclusive_amount"),
    }

    structural_issues = _structural_issues(extraction)
    line_items = extraction.get("line_items") or []
    mismatches: List[dict] = []
    resolved_line_items: List[dict] = []
    document_totals = {"total_vat": totals.get("tax_amount"), "grand_total": totals.get("tax_inclusive_amount")}

    if not line_items:
        structural_issues.append(_issue(
            "not_readable_as_invoice", None, "error",
            "Couldn't read this as a structured invoice (no line items found) — only a ZATCA "
            "e-invoice XML, or a PDF with an embedded XML attachment, are supported here.",
        ))
    else:
        if document_totals["total_vat"] is None and document_totals["grand_total"] is None:
            structural_issues.append(_issue(
                "no_printed_totals", None, "warning",
                "No printed VAT/total found on the document to verify the calculation against.",
            ))
        resolved_line_items = _resolve_line_items(line_items)
        # Calls tools.validate_zatca_form() directly — in-process, no MCP
        # subprocess, no QwenBrain call — just to determine match/
        # mismatches cheaply for every invoice in the batch. Narration
        # (which does spin up an MCP subprocess + two QwenBrain round
        # trips, see graph.py's ZatcaCalculatorAgent.run()) is requested
        # separately below, ONLY for invoices that end up flagged — the
        # whole reason for the two-step split.
        try:
            tool_result = run_validate_zatca_form(resolved_line_items, document_totals)
        except ToolInputError as exc:
            structural_issues.append(_issue("calculation_failed", None, "error", str(exc)))
        else:
            # Every mismatch is tagged with whether it's within tolerance —
            # nothing is dropped from the data, but a caller (report/
            # summary/narration) can now tell "this is just rounding
            # noise" apart from "this is a real discrepancy" instead of
            # seeing two identically-shaped, unlabeled entries.
            mismatches = [{**m, "within_tolerance": not _is_significant_mismatch(m, tolerance)} for m in tool_result.get("mismatches", [])]

    significant_mismatches = [m for m in mismatches if not m["within_tolerance"]]
    has_error = any(i["severity"] == "error" for i in structural_issues) or bool(significant_mismatches)
    status = "flagged" if has_error else "clean"

    explanation = None
    if status == "flagged":
        try:
            # Only significant mismatches are passed as findings to narrate
            # — a within-tolerance rounding difference isn't being treated
            # as a problem, so it shouldn't be narrated as one either.
            explanation = _narrate_flagged_invoice(structural_issues, significant_mismatches)
        except Exception as exc:  # narration is best-effort — never re-flag or crash the check over it
            logger.warning("Narration failed for flagged invoice %s: %s", filename, exc)

    return {
        "filename": filename,
        "status": status,
        "extracted": extracted_summary,
        "structural_issues": structural_issues,
        "mismatches": mismatches,
        "explanation": explanation,
    }


def run_invoice_check(files: List[Tuple[str, bytes]], tolerance: Optional[Decimal] = None) -> Dict[str, Any]:
    """files: [(filename, content_bytes), ...]. Runs every file through
    the check automatically, no human input mid-batch — returns the full
    per-invoice results plus counts. tolerance: user-supplied SAR
    tolerance for the printed-vs-recalculated VAT/total comparison — the
    app's own default (config.MISMATCH_TOLERANCE) is used when not set."""
    effective_tolerance = tolerance if tolerance is not None else MISMATCH_TOLERANCE
    results = []
    for filename, content in files:
        upload_dir = tempfile.mkdtemp(prefix="mizan_invoice_check_")
        try:
            path = os.path.join(upload_dir, filename)
            with open(path, "wb") as f:
                f.write(content)
            results.append(_check_one_invoice(filename, path, effective_tolerance))
        finally:
            shutil.rmtree(upload_dir, ignore_errors=True)

    clean = sum(1 for r in results if r["status"] == "clean")
    return {
        "invoices": results,
        "total": len(results),
        "clean": clean,
        "flagged": len(results) - clean,
    }
