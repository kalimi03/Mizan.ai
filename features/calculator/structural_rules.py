"""
Mizan.ai — structural validation rules (Feature E, Step 6). Deliberately
separate from engine.py — this module is used only by the validator, never
by calculate_vat, so it can later reference Feature A's RAG knowledge base
to cite the actual regulation behind a failure without touching the
calculation engine at all.

Every check returns a list of StructuralIssue — never raises for a "the
document is invalid" finding (that's the whole point: surface it for human
review). Only genuinely malformed *input to this function* (missing dict
keys entirely) is a caller bug, not a document-quality finding.

Expected `document` dict shape (matches what the /validate endpoint, Phase
3, passes through):
    {
        "seller_id": str, "buyer_id": str, "vat_number": str,
        "issue_date": date, "supply_date": Optional[date],
        "identity_number": str,   # CR number (company) or national ID/Iqama (individual)
    }
"""

from dataclasses import dataclass
from datetime import date
from typing import List, Optional

from .config import VAT_NUMBER_LENGTH, TaxpayerType


@dataclass
class StructuralIssue:
    rule_id: str
    field: Optional[str]
    severity: str  # "error" | "warning"
    message: str


def validate_required_fields(document: dict, taxpayer_type: TaxpayerType) -> List[StructuralIssue]:
    issues: List[StructuralIssue] = []

    required = ["seller_id", "buyer_id", "vat_number", "issue_date"]
    for field_name in required:
        if not document.get(field_name):
            issues.append(StructuralIssue(
                rule_id="missing_required_field",
                field=field_name,
                severity="error",
                message=f"Required field {field_name!r} is missing.",
            ))

    identity_label = "CR number" if taxpayer_type == TaxpayerType.company else "national ID / Iqama"
    if not document.get("identity_number"):
        issues.append(StructuralIssue(
            rule_id="missing_identity_field",
            field="identity_number",
            severity="error",
            message=f"Missing {identity_label} — required for taxpayer_type={taxpayer_type.value}.",
        ))

    return issues


def validate_vat_number_format(vat_number: Optional[str]) -> List[StructuralIssue]:
    if not vat_number:
        return []  # already covered by validate_required_fields — don't double-report

    issues: List[StructuralIssue] = []
    if len(vat_number) != VAT_NUMBER_LENGTH or not vat_number.isdigit():
        issues.append(StructuralIssue(
            rule_id="invalid_vat_number_format",
            field="vat_number",
            severity="error",
            message=f"VAT number must be {VAT_NUMBER_LENGTH} digits (got {vat_number!r}).",
        ))
    elif not (vat_number.startswith("3") and vat_number.endswith("3")):
        issues.append(StructuralIssue(
            rule_id="invalid_vat_number_format",
            field="vat_number",
            severity="error",
            message=f"ZATCA VAT numbers must start and end with '3' (got {vat_number!r}).",
        ))
    return issues


def validate_dates(issue_date: Optional[date], supply_date: Optional[date] = None) -> List[StructuralIssue]:
    if issue_date is None:
        return []  # already covered by validate_required_fields

    issues: List[StructuralIssue] = []
    today = date.today()
    if issue_date > today:
        issues.append(StructuralIssue(
            rule_id="future_issue_date",
            field="issue_date",
            severity="error",
            message=f"Issue date {issue_date} is in the future.",
        ))
    if supply_date is not None and supply_date > issue_date:
        issues.append(StructuralIssue(
            rule_id="supply_date_after_issue_date",
            field="supply_date",
            severity="warning",
            message=f"Supply date {supply_date} is after issue date {issue_date} — verify.",
        ))
    return issues


def validate_cross_field_consistency(seller_id: Optional[str], buyer_id: Optional[str]) -> List[StructuralIssue]:
    if not seller_id or not buyer_id:
        return []  # already covered by validate_required_fields

    if seller_id == buyer_id:
        return [StructuralIssue(
            rule_id="seller_equals_buyer",
            field="buyer_id",
            severity="error",
            message="Buyer and seller identifiers are identical.",
        )]
    return []


def run_structural_validation(document: dict, taxpayer_type: TaxpayerType) -> List[StructuralIssue]:
    """Orchestrator — the single entry point the validator calls."""
    issues: List[StructuralIssue] = []
    issues += validate_required_fields(document, taxpayer_type)
    issues += validate_vat_number_format(document.get("vat_number"))
    issues += validate_dates(document.get("issue_date"), document.get("supply_date"))
    issues += validate_cross_field_consistency(document.get("seller_id"), document.get("buyer_id"))
    return issues
