"""
Mizan.ai — LangGraph orchestration for Feature E, acting as the MCP CLIENT
that connects to mcp_server.py and offers its tools to QwenBrain.

QwenBrain is a raw HTTP LLM endpoint — it doesn't speak MCP itself.
Something in our own code has to be the actual MCP client: fetch tool
definitions from the MCP server, offer them to QwenBrain as `tools` in the
/generate call, and when QwenBrain returns a tool_call, invoke the
corresponding tool through the MCP client/server (not as a bare in-process
function call — that's the whole point of using MCP here).

SAFETY INVARIANT, the core design principle of this graph: the
deterministic engine always runs on OUR OWN validated request data, never
on arguments a model extracted from text. Concretely: run_engine (below)
calls the MCP tool using the request's own line_items — not anything
QwenBrain extracted — to get the real tool_result; QwenBrain's tool-calling
turn is used only to produce an optional natural-language narration of
that already-computed result. The one exception is classify_line_items
(handled separately, see classify_async below), where the model's judgment
genuinely IS the answer wanted — see tools.py's module docstring for the
full "our data wins" vs "model's judgment wins" distinction.

Fails open throughout: if QwenBrain or the MCP server is unavailable, the
graph still returns the correct tool_result with explanation=None — same
pattern as features/chatbot/filing_notes_qa.py's "if not model_url: return
context" fallback. Numbers must never depend on model/MCP availability.

Structure follows features/explainer/online/graph.py's StateGraph +
add_conditional_edges style (inline lambda returning the next node name),
adapted to async nodes since the MCP client API is async-only. No
interrupt()/resume and no Postgres checkpointer here, deliberately —
unlike the chatbot's ongoing conversation memory, every calculation is a
stateless, one-shot request: nothing is paused mid-graph waiting for a
later resume call (editing happens client-side, before the confirming API
call — see the HITL design in app/main.py), so there's no state that
needs to survive across separate invocations.
"""

import json
import logging
import sys
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, StateGraph
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from features.common.modal_client import ModalEndpointError, call_modal_json
from features.common.text_cleanup import strip_latex_math

from .config import QWEN_BRAIN_URL
from .tools import (
    CALCULATE_VAT_TOOL_SCHEMA,
    CLASSIFY_LINE_ITEMS_TOOL_SCHEMA,
    VALIDATE_ZATCA_FORM_TOOL_SCHEMA,
)

logger = logging.getLogger(__name__)

_TOOL_SCHEMAS = {
    "calculate_vat": CALCULATE_VAT_TOOL_SCHEMA,
    "validate_zatca_form": VALIDATE_ZATCA_FORM_TOOL_SCHEMA,
}

_NARRATION_PROMPTS = {
    "calculate_vat": (
        "Use the calculate_vat tool on these invoice line items, then briefly explain the result "
        "in plain language for the user."
    ),
    "validate_zatca_form": (
        "Use the validate_zatca_form tool on these invoice line items and document totals, then "
        "briefly explain the result in plain language, calling out any mismatches between the "
        "document's printed values and the recalculated ones."
    ),
}


class ZatcaCalculatorError(RuntimeError):
    """Raised for genuine input errors (e.g. bad line items) — never for
    QwenBrain/MCP unavailability, which fails open instead."""


class ZatcaCalculatorState(TypedDict, total=False):
    mode: str  # "calculate" | "validate"
    line_items: List[dict]
    document_totals: Optional[dict]
    currency: str
    language: str
    tool_name: str
    tool_result: Dict[str, Any]
    brain_tool_calls: List[dict]
    explanation: Optional[str]


async def _call_mcp_tool(tool_name: str, arguments: dict) -> Dict[str, Any]:
    """Connects to mcp_server.py over stdio, calls one tool, closes the
    connection. A fresh subprocess per call is simpler and safer than
    holding a persistent MCP session open across unrelated HTTP requests —
    acceptable for v1; revisit if per-request subprocess spawn latency
    becomes a real problem."""
    params = StdioServerParameters(command=sys.executable, args=["-m", "features.calculator.mcp_server"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            if result.isError:
                message = result.content[0].text if result.content else "unknown MCP tool error"
                raise ZatcaCalculatorError(message)
            return json.loads(result.content[0].text)


def _build_user_message(state: ZatcaCalculatorState) -> str:
    """Includes the actual line items (and document totals, for validate)
    in the prompt — without concrete data to reason about, the model has
    nothing to decide to call the tool with, and tends to just ask for
    details instead of invoking it."""
    tool_name = state["tool_name"]
    prompt = _NARRATION_PROMPTS[tool_name]
    if state.get("language") == "ar":
        prompt += " Respond in Arabic."

    payload: Dict[str, Any] = {"line_items": state["line_items"], "currency": state.get("currency", "SAR")}
    if tool_name == "validate_zatca_form":
        payload["document_totals"] = state.get("document_totals") or {}

    return f"{prompt}\n\n{json.dumps(payload)}"


def _reconstruct_tool_call_message(tool_name: str, arguments: dict) -> str:
    """Matches Qwen's own <tool_call> tag format exactly, per
    modal-serving/test_qwen_brain.py's test_tool_call_followup() —
    GenerateRequest's ChatMessage only has {role, content}, no structured
    tool_calls field for the assistant turn, so this has to be
    reconstructed manually."""
    return f'<tool_call>\n{json.dumps({"name": tool_name, "arguments": arguments})}\n</tool_call>'


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------


async def _run_engine_node(state: ZatcaCalculatorState) -> dict:
    """Always runs, unconditionally of the model — this is the safety
    invariant in code: the real number comes from here, using our own
    already-validated data."""
    mode = state["mode"]
    tool_name = "calculate_vat" if mode == "calculate" else "validate_zatca_form"

    arguments: Dict[str, Any] = {"line_items": state["line_items"], "currency": state.get("currency", "SAR")}
    if mode == "validate":
        arguments["document_totals"] = state.get("document_totals") or {}

    tool_result = await _call_mcp_tool(tool_name, arguments)
    return {"tool_name": tool_name, "tool_result": tool_result}


async def _offer_to_brain_node(state: ZatcaCalculatorState) -> dict:
    """First /generate call: offers the tool schema so QwenBrain's turn is
    structurally consistent with the two-turn tool-calling pattern. We do
    NOT use whatever arguments it extracts — tool_result already came from
    _run_engine_node using our own data."""
    if not QWEN_BRAIN_URL:
        return {"brain_tool_calls": [], "explanation": None}

    tool_name = state["tool_name"]
    schema = _TOOL_SCHEMAS[tool_name]

    try:
        response = call_modal_json(QWEN_BRAIN_URL, {
            "messages": [{"role": "user", "content": _build_user_message(state)}],
            "max_tokens": 256,
            "temperature": 0.1,
            "tools": [schema],
        }, timeout=120)
    except ModalEndpointError as exc:
        logger.warning("QwenBrain unavailable for narration (%s): %s", tool_name, exc)
        return {"brain_tool_calls": [], "explanation": None}

    tool_calls = response.get("tool_calls") or []
    # No tool call from the model — use whatever direct content it gave as
    # a fallback explanation rather than discarding it.
    return {"brain_tool_calls": tool_calls, "explanation": strip_latex_math(response.get("content")) or None}


async def _execute_and_confirm_node(state: ZatcaCalculatorState) -> dict:
    """Second /generate call — feeds OUR tool_result (not the model's
    extracted arguments) back as the tool turn, to get a final
    natural-language explanation grounded in the real numbers."""
    tool_name = state["tool_name"]
    schema = _TOOL_SCHEMAS[tool_name]
    user_message = _build_user_message(state)
    assistant_tool_call = _reconstruct_tool_call_message(tool_name, {})

    try:
        response = call_modal_json(QWEN_BRAIN_URL, {
            "messages": [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": assistant_tool_call},
                {"role": "tool", "content": json.dumps(state["tool_result"])},
            ],
            "max_tokens": 256,
            "temperature": 0.1,
            "tools": [schema],
        }, timeout=120)
    except ModalEndpointError as exc:
        logger.warning("QwenBrain unavailable for narration follow-up (%s): %s", tool_name, exc)
        return {"explanation": None}

    return {"explanation": strip_latex_math(response.get("content")) or None}


def _route_after_offer(state: ZatcaCalculatorState) -> str:
    return "execute_and_confirm" if state.get("brain_tool_calls") else "__end__"


def _build_graph():
    workflow = StateGraph(ZatcaCalculatorState)
    workflow.add_node("run_engine", _run_engine_node)
    workflow.add_node("offer_to_brain", _offer_to_brain_node)
    workflow.add_node("execute_and_confirm", _execute_and_confirm_node)

    workflow.set_entry_point("run_engine")
    workflow.add_edge("run_engine", "offer_to_brain")
    workflow.add_conditional_edges("offer_to_brain", _route_after_offer, {
        "execute_and_confirm": "execute_and_confirm",
        "__end__": END,
    })
    workflow.add_edge("execute_and_confirm", END)

    return workflow.compile()


# ---------------------------------------------------------------------------
# classify_line_items flow — separate from the graph above: a single async
# round trip (ask QwenBrain, validate its judgment via the MCP tool), not
# the two-turn "confirm our own data, then narrate" pattern the calculate/
# validate flow needs, so folding it into the same StateGraph would add
# structure without benefit.
# ---------------------------------------------------------------------------


# Real category definitions, not just the enum names — confirmed via live
# testing that the model will otherwise guess wrong on common cases (e.g.
# classified general "legal consulting fees" as exempt, when it should be
# standard-rated). "If uncertain, prefer standard" is a deliberate
# calibration instruction, not padding: standard is VAT law's actual
# default/baseline, with zero-rated and exempt being narrow, enumerated
# exceptions — an uncertain guess should lean toward the default, not an
# exception, which is the safer failure mode either way (the classification
# is always surfaced for human review regardless, per the "model's
# judgment wins but only as a suggestion" design — see tools.py — so this
# improves the suggestion's hit rate, it doesn't change what gets trusted).
_CLASSIFICATION_GUIDANCE = (
    "Saudi VAT tax category definitions:\n"
    "- standard (15%): the default rate for most goods and services — including general "
    "professional/consulting/legal/business services, most merchandise and commercial transactions.\n"
    "- zero_rated (0%, input VAT still reclaimable): a narrow set of specific exceptions — exports "
    "outside the GCC, international transport, qualifying medicines/medical goods, qualifying "
    "investment-grade precious metals.\n"
    "- exempt (0%, input VAT NOT reclaimable): another narrow, specific set — margin-based financial "
    "services (not fee-based ones), life insurance, residential real estate leasing.\n"
    "If a line item doesn't clearly match one of the zero_rated or exempt cases above, classify it as "
    "standard — that is the default, not a fallback to avoid."
)


async def classify_async(line_items: List[dict], language: str = "en") -> Dict[str, Any]:
    """Offers classify_line_items to QwenBrain and lets it propose
    categories (the "model's judgment wins" tool — see tools.py),
    validated/normalized via the MCP tool. Fails open: returns an empty
    classification list (never raises) if QwenBrain/MCP are unavailable,
    so the caller can still show the review screen for manual
    classification."""
    if not QWEN_BRAIN_URL:
        return {"classifications": [], "invalid": []}

    prompt = (
        "For each of the following invoice line items, decide its tax category "
        "(standard, zero_rated, or exempt) and call classify_line_items with your judgment.\n\n"
        + _CLASSIFICATION_GUIDANCE
        + "\n\nLine items:\n"
        + json.dumps([{"line_id": i.get("line_id"), "description": i.get("description")} for i in line_items])
    )

    try:
        response = call_modal_json(QWEN_BRAIN_URL, {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 512,
            "temperature": 0.1,
            "tools": [CLASSIFY_LINE_ITEMS_TOOL_SCHEMA],
        }, timeout=120)
    except ModalEndpointError as exc:
        logger.warning("QwenBrain unavailable for classification: %s", exc)
        return {"classifications": [], "invalid": []}

    tool_calls = response.get("tool_calls") or []
    if not tool_calls or tool_calls[0].get("name") != "classify_line_items":
        return {"classifications": [], "invalid": []}

    # The model's own judgment IS the input here — pass it through the MCP
    # tool for validation/normalization, not recomputed by us.
    arguments = tool_calls[0].get("arguments", {})
    try:
        return await _call_mcp_tool("classify_line_items", arguments)
    except ZatcaCalculatorError as exc:
        logger.warning("classify_line_items MCP call failed: %s", exc)
        return {"classifications": [], "invalid": []}


class ZatcaCalculatorAgent:
    """Public entrypoint app/main.py calls. Wraps the async
    StateGraph/MCP-client machinery behind a synchronous interface,
    matching this repo's established sync-endpoint convention
    (features/explainer/online/graph.py's ask(),
    features/chatbot/langgraph_chatbot.py's generate_reply())."""

    def __init__(self) -> None:
        self.graph = _build_graph()

    def run(
        self,
        mode: str,
        line_items: List[dict],
        currency: str = "SAR",
        language: str = "en",
        document_totals: Optional[dict] = None,
    ) -> Dict[str, Any]:
        import asyncio

        state: ZatcaCalculatorState = {
            "mode": mode,
            "line_items": line_items,
            "document_totals": document_totals,
            "currency": currency,
            "language": language,
        }
        result_state = asyncio.run(self.graph.ainvoke(state))
        return {
            "tool_result": result_state["tool_result"],
            "explanation": result_state.get("explanation"),
        }

    def classify(self, line_items: List[dict], language: str = "en") -> Dict[str, Any]:
        import asyncio

        return asyncio.run(classify_async(line_items, language))
