"""
Mizan.ai — Feature E's MCP server. One server, four tools: calculate_vat,
validate_zatca_form, classify_line_items, generate_report — all thin
wrappers around tools.py, which does the actual work and stays
independently unit-testable without any MCP machinery involved.

Runs over stdio transport (the default) — spawned as a subprocess by
graph.py's MCP client, no separate network service needed. See
tools.py's module docstring for the "our data wins" vs "model's judgment
wins" distinction between the four tools.

Run standalone for manual testing: python -m features.calculator.mcp_server
"""

from typing import Optional

# Eager imports (deliberately not the lazy-inside-function style used
# elsewhere in this package) — confirmed via direct testing that
# generate_report would otherwise hang indefinitely (no exception, no
# response, server just stops responding) the first time it ran inside the
# MCP server specifically, while the exact same call in a plain synchronous
# script completed in under half a second. Root cause: FastMCP runs
# synchronous tool handlers via anyio's worker-thread pool, and
# openpyxl/python-docx/reportlab being imported for the very first time
# from a non-main thread deadlocked. Importing them here, on the main
# thread, before the server starts accepting requests, resolved it —
# confirmed with a raw JSON-RPC probe bypassing the MCP client library.
import openpyxl  # noqa: F401
import docx  # noqa: F401
import reportlab  # noqa: F401

from mcp.server.fastmcp import FastMCP

from .tools import (
    calculate_vat,
    classify_line_items,
    generate_report,
    validate_zatca_form,
)

mcp = FastMCP("zatca-calculator")


@mcp.tool(name="calculate_vat", description=(
    "Calculate VAT from a list of invoice line items. Returns the computed numbers only."
))
def _calculate_vat_tool(line_items: list, currency: str = "SAR") -> dict:
    return calculate_vat(line_items, currency)


@mcp.tool(name="validate_zatca_form", description=(
    "Recompute VAT independently from line items, then compare against the totals already "
    "printed on the document. Returns match/mismatch with both values shown."
))
def _validate_zatca_form_tool(line_items: list, document_totals: dict, currency: str = "SAR") -> dict:
    return validate_zatca_form(line_items, document_totals, currency)


@mcp.tool(name="classify_line_items", description=(
    "Record your tax-category judgment for each invoice line item. For each line, decide "
    "whether it is standard-rated (15%), zero-rated (0%, e.g. exports), or exempt (0%, e.g. "
    "certain financial/real-estate services) based on its description, and give a confidence "
    "score from 0 to 1."
))
def _classify_line_items_tool(classifications: list) -> dict:
    return classify_line_items(classifications)


@mcp.tool(name="generate_report", description=(
    "Generate the final output report (data export or issues summary) as a downloadable file."
))
def _generate_report_tool(
    calculation_result: dict,
    format: str = "xlsx",
    language: str = "en",
    report_type: str = "data",
    structural_issues: Optional[list] = None,
    mismatches: Optional[list] = None,
    explanation: Optional[str] = None,
) -> dict:
    return generate_report(
        calculation_result, format=format, language=language, report_type=report_type,
        structural_issues=structural_issues, mismatches=mismatches, explanation=explanation,
    )


if __name__ == "__main__":
    mcp.run()
