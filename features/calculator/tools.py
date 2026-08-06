"""
Mizan.ai — the four MCP tools behind Feature E's calculator (Step 5, plus
classification and reporting folded in per the same MCP server per user
direction). Plain functions here, registered on mcp_server.py's FastMCP
instance — this module has no MCP-specific code, so it stays independently
unit-testable.

Two different usage patterns across the four tools, deliberately not
uniform:

  calculate_vat / validate_zatca_form / generate_report — "our data wins".
  Called with OUR OWN already-validated request data, never with whatever
  arguments a model extracted from text — the math (and the report
  rendering it feeds) must be trustworthy regardless of model behavior.

  classify_line_items — "model's judgment wins". The model's tool-call
  arguments ARE the answer we want (deciding a line's tax category is a
  genuine judgment call, not something we can compute) — this function's
  job is to validate/normalize that response, not override it.
"""

import base64
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from .config import CalculationFlag, TaxCategory
from .engine import CalculationEngineError, CalculationResult, LineItem, LineItemResult, calculate_invoice
from .report import ReportFormat, ReportLanguage, ReportType, generate_data_report, generate_issues_summary


class ToolInputError(ValueError):
    """Raised for malformed tool input — the MCP layer surfaces this as a tool error."""


# ---------------------------------------------------------------------------
# Shared (de)serialization — JSON-safe dicts <-> Decimal-based engine types
# ---------------------------------------------------------------------------


def _to_decimal(value: Any, field_name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise ToolInputError(f"{field_name} must be a number, got {value!r}")


def _parse_line_items(raw: List[dict]) -> List[LineItem]:
    if not raw:
        raise ToolInputError("line_items must not be empty")

    items = []
    for i, entry in enumerate(raw):
        try:
            tax_category = TaxCategory(entry["tax_category"])
        except (KeyError, ValueError):
            raise ToolInputError(
                f"line_items[{i}] has invalid/missing tax_category "
                f"(expected one of {[c.value for c in TaxCategory]})"
            )
        flag = None
        if entry.get("flag"):
            try:
                flag = CalculationFlag(entry["flag"])
            except ValueError:
                raise ToolInputError(f"line_items[{i}] has invalid flag {entry['flag']!r}")

        items.append(LineItem(
            line_id=str(entry.get("line_id", i)),
            description=entry.get("description", ""),
            taxable_base=_to_decimal(entry.get("taxable_base"), f"line_items[{i}].taxable_base"),
            tax_category=tax_category,
            flag=flag,
        ))
    return items


def _serialize_result(result: CalculationResult) -> Dict[str, Any]:
    return {
        "lines": [
            {
                "line_id": line.line_id,
                "description": line.description,
                "taxable_base": float(line.taxable_base),
                "tax_category": line.tax_category.value,
                "rate": float(line.rate),
                "vat_amount": float(line.vat_amount),
                "flagged_for_manual_review": line.flagged_for_manual_review,
                "flag_reason": line.flag_reason,
            }
            for line in result.lines
        ],
        "subtotal": float(result.subtotal),
        "total_vat": float(result.total_vat),
        "grand_total": float(result.grand_total),
        "currency": result.currency,
        "has_flagged_lines": result.has_flagged_lines,
    }


def _deserialize_result(data: dict) -> CalculationResult:
    """Reconstructs a CalculationResult directly from an already-computed
    dict (the exact shape _serialize_result produces) — trusts the
    vat_amount/rate values as-is rather than recomputing them. Used by
    generate_report(), which renders an already-finalized result and must
    not silently re-run the engine on it."""
    lines = [
        LineItemResult(
            line_id=line["line_id"],
            description=line["description"],
            taxable_base=_to_decimal(line["taxable_base"], "taxable_base"),
            tax_category=TaxCategory(line["tax_category"]),
            rate=_to_decimal(line["rate"], "rate"),
            vat_amount=_to_decimal(line["vat_amount"], "vat_amount"),
            flagged_for_manual_review=line.get("flagged_for_manual_review", False),
            flag_reason=line.get("flag_reason"),
        )
        for line in data.get("lines", [])
    ]
    return CalculationResult(
        lines=lines,
        subtotal=_to_decimal(data.get("subtotal", 0), "subtotal"),
        total_vat=_to_decimal(data.get("total_vat", 0), "total_vat"),
        grand_total=_to_decimal(data.get("grand_total", 0), "grand_total"),
        currency=data.get("currency", "SAR"),
        has_flagged_lines=data.get("has_flagged_lines", False),
    )


# ---------------------------------------------------------------------------
# Tool 1 — calculate_vat ("our data wins")
# ---------------------------------------------------------------------------


def calculate_vat(line_items: List[dict], currency: str = "SAR") -> Dict[str, Any]:
    """Computes VAT from line items. Returns computed numbers only —
    nothing else."""
    try:
        items = _parse_line_items(line_items)
        result = calculate_invoice(items, currency=currency)
    except CalculationEngineError as exc:
        raise ToolInputError(str(exc)) from exc
    return _serialize_result(result)


CALCULATE_VAT_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "calculate_vat",
        "description": "Calculate VAT from a list of invoice line items. Returns the computed numbers only.",
        "parameters": {
            "type": "object",
            "properties": {
                "line_items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "line_id": {"type": "string"},
                            "description": {"type": "string"},
                            "taxable_base": {"type": "number"},
                            "tax_category": {"type": "string", "enum": ["standard", "zero_rated", "exempt"]},
                        },
                        "required": ["line_id", "taxable_base", "tax_category"],
                    },
                },
                "currency": {"type": "string", "default": "SAR"},
            },
            "required": ["line_items"],
        },
    },
}


# ---------------------------------------------------------------------------
# Tool 2 — validate_zatca_form ("our data wins")
# ---------------------------------------------------------------------------


def validate_zatca_form(line_items: List[dict], document_totals: dict, currency: str = "SAR") -> Dict[str, Any]:
    """Recomputes VAT independently via calculate_vat's same engine, then
    compares the result against whatever numbers are already printed on
    the document. The engine never looks at document_totals — this
    comparison happens only here, so it's a genuine check, not circular."""
    try:
        items = _parse_line_items(line_items)
        result = calculate_invoice(items, currency=currency)
    except CalculationEngineError as exc:
        raise ToolInputError(str(exc)) from exc

    calculation = _serialize_result(result)
    mismatches = []
    for field_name, recalculated in (("total_vat", calculation["total_vat"]), ("grand_total", calculation["grand_total"])):
        document_value = document_totals.get(field_name)
        if document_value is None:
            continue
        if round(float(document_value), 2) != round(recalculated, 2):
            mismatches.append({
                "field": field_name,
                "document_value": document_value,
                "recalculated_value": recalculated,
            })

    return {
        "match": len(mismatches) == 0,
        "calculation": calculation,
        "mismatches": mismatches,
    }


VALIDATE_ZATCA_FORM_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "validate_zatca_form",
        "description": (
            "Recompute VAT independently from line items, then compare against the totals "
            "already printed on the document. Returns match/mismatch with both values shown."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "line_items": CALCULATE_VAT_TOOL_SCHEMA["function"]["parameters"]["properties"]["line_items"],
                "document_totals": {
                    "type": "object",
                    "properties": {
                        "total_vat": {"type": "number"},
                        "grand_total": {"type": "number"},
                    },
                },
                "currency": {"type": "string", "default": "SAR"},
            },
            "required": ["line_items", "document_totals"],
        },
    },
}


# ---------------------------------------------------------------------------
# Tool 3 — classify_line_items ("model's judgment wins")
# ---------------------------------------------------------------------------


def classify_line_items(classifications: List[dict]) -> Dict[str, Any]:
    """Takes the MODEL'S OWN classification judgments (not extracted from
    our data — this is the one tool where the caller's arguments ARE the
    answer) and validates/normalizes them: confirms each tax_category is
    one of the three valid values, carries through confidence, flags
    anything malformed rather than silently dropping it."""
    valid: List[Dict[str, Any]] = []
    invalid: List[Dict[str, Any]] = []

    for entry in classifications or []:
        line_id = entry.get("line_id")
        category_raw = entry.get("tax_category")
        try:
            category = TaxCategory(category_raw)
        except ValueError:
            invalid.append({"line_id": line_id, "reason": f"invalid tax_category {category_raw!r}"})
            continue
        valid.append({
            "line_id": line_id,
            "tax_category": category.value,
            "confidence": entry.get("confidence"),
        })

    return {"classifications": valid, "invalid": invalid}


CLASSIFY_LINE_ITEMS_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "classify_line_items",
        "description": (
            "Record your tax-category judgment for each invoice line item. For each line, decide "
            "whether it is standard-rated (15%), zero-rated (0%, e.g. exports), or exempt (0%, e.g. "
            "certain financial/real-estate services) based on its description, and give a confidence "
            "score from 0 to 1."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "classifications": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "line_id": {"type": "string"},
                            "tax_category": {"type": "string", "enum": ["standard", "zero_rated", "exempt"]},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        },
                        "required": ["line_id", "tax_category"],
                    },
                },
            },
            "required": ["classifications"],
        },
    },
}


# ---------------------------------------------------------------------------
# Tool 4 — generate_report ("our data wins", deterministic rendering)
# ---------------------------------------------------------------------------


def generate_report(
    calculation_result: dict,
    format: ReportFormat = "xlsx",
    language: ReportLanguage = "en",
    report_type: ReportType = "data",
    structural_issues: Optional[List[dict]] = None,
    mismatches: Optional[List[dict]] = None,
    explanation: Optional[str] = None,
) -> Dict[str, Any]:
    """Renders the already-finalized calculation_result (the exact dict
    shape calculate_vat/validate_zatca_form return) into a downloadable
    file. report_type="issues_summary" ignores `format` (always PDF) and
    renders a narrative instead of a data table."""
    if report_type == "issues_summary":
        content = generate_issues_summary(
            structural_issues or [], mismatches or [], explanation, language,
        )
        filename = f"issues_summary.pdf"
        mimetype = "application/pdf"
    else:
        result = _deserialize_result(calculation_result)
        content = generate_data_report(result, format=format, language=language)
        filename = f"vat_report.{format}"
        mimetype = {
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "pdf": "application/pdf",
        }[format]

    return {
        "filename": filename,
        "mimetype": mimetype,
        "content_base64": base64.b64encode(content).decode("ascii"),
    }


GENERATE_REPORT_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "generate_report",
        "description": "Generate the final output report (data export or issues summary) as a downloadable file.",
        "parameters": {
            "type": "object",
            "properties": {
                "calculation_result": {"type": "object"},
                "format": {"type": "string", "enum": ["pdf", "docx", "xlsx"], "default": "xlsx"},
                "language": {"type": "string", "enum": ["ar", "en"], "default": "en"},
                "report_type": {"type": "string", "enum": ["data", "issues_summary"], "default": "data"},
            },
            "required": ["calculation_result"],
        },
    },
}
