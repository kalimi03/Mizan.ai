"""
Mizan.ai — Comparator (Feature C) optional exception summary.

Not part of the handoff doc's explicit spec — a small, low-risk addition
mirroring Feature E's optional narration UX, using the SAME simple
single-completion-call shape as features/chatbot/filing_notes_qa.py's
answer_filing_question() (no tool-calling, no MCP): there's no tool result
to narrate here, just an already-finalized list of exceptions to summarize
in plain language. Fails open — returns None on any failure or if
QWEN_BRAIN_URL isn't configured, same as every other QwenBrain call site in
this repo. Never blocks POST /confirm.
"""

import logging
from typing import List, Optional

from features.common.modal_client import ModalEndpointError, call_modal_json
from features.common.text_cleanup import strip_latex_math

from .config import QWEN_BRAIN_URL
from .models import ReviewItem

logger = logging.getLogger(__name__)

_SUMMARY_SYSTEM_PROMPT = (
    "You summarize a monthly reconciliation's exceptions in plain, concise language for a "
    "business owner. Use ONLY the exceptions listed below — never invent an item, amount, or "
    "reason that isn't there. Every line ends with 'status: confirmed', 'status: dismissed', or "
    "'status: unresolved' — this status is the actual, final, human-reviewed outcome and always "
    "wins over anything the description text might otherwise suggest. A line marked 'status: "
    "confirmed' is SETTLED — describe it as resolved/explained (using its 'explanation' field if "
    "one is given), never as missing, unmatched, or lacking a corresponding entry, no matter what "
    "kind it is or what its row descriptions look like. Only describe something as missing/not "
    "appearing if it's a genuine 'unmatched' line that is NOT confirmed. Each line already states "
    "what kind of exception it is — an "
    "'ambiguous'/contested line means a candidate WAS found on the second document but it's "
    "unclear which of our own transactions it belongs to (an internal records question, not "
    "something missing from the other side); a 'group_match' line means several transactions on "
    "one side plausibly consolidate into one entry on the other (e.g. a vendor billing multiple "
    "purchase orders as one invoice); only a genuine 'unmatched' line with nothing on the other "
    "side should be described as missing/not appearing. Do not describe a contested or grouped "
    "item as missing just because it isn't a clean one-to-one pairing. An 'amount_mismatch' line "
    "marked '(possible VAT gap)' means the two amounts differ by close to 15% — likely one side "
    "recorded the figure VAT-inclusive and the other VAT-exclusive for the same transaction, not "
    "necessarily a data error; mention that possibility rather than just calling it a discrepancy. "
    "A 'sum_match' line is the SAME kind of consolidation as 'group_match' (several transactions "
    "summing to one), but found without any shared date, description, or reference — weaker "
    "evidence, found by amount alone. Describe it as a possible grouping worth double-checking, "
    "not with the same confidence as a group_match. Group similar cases together (e.g. timing "
    "differences vs. genuinely missing items) rather than listing every line individually. Keep it "
    "to a short paragraph."
)


def _format_row_side(description: Optional[str], amount) -> str:
    return f"{description or '-'} ({amount if amount is not None else '-'})"


def _format_rows(rows: List["ReconciliationRow"]) -> str:
    if not rows:
        return "-"
    return "; ".join(_format_row_side(r.description, r.amount) for r in rows)


def _format_reviewed(reviewed: List[ReviewItem]) -> str:
    lines = []
    for item in reviewed:
        status = item.decision or "unresolved"

        if item.kind == "ambiguous":
            # row_b is never set here -- the real second-document data lives
            # in `candidates` (see matching.py's Pass 3). Reading row_b
            # directly (as this used to) told the model "second doc: - (-)",
            # i.e. nothing there at all, which is why it wrongly summarized
            # these as missing from the second document instead of
            # contested between candidates.
            note = (
                "one candidate found, but also claimed by a different one of our transactions"
                if len(item.candidates) == 1
                else "several of our transactions could plausibly be this one candidate"
            )
            lines.append(
                f"- kind: ambiguous ({note}) | sap/odoo: {_format_row_side(item.row_a.description if item.row_a else None, item.row_a.amount if item.row_a else None)} "
                f"| second doc candidate(s): {_format_rows(item.candidates)} "
                f"| status: {status} | explanation: {item.explanation or '-'}"
            )
            continue

        if item.kind in ("group_match", "sum_match"):
            # Same underlying gap as above -- the multi-item side lives in
            # `group`, not row_a/row_b, whichever of those is None here.
            # sum_match reuses group_match's exact shape, just weaker
            # evidence (see the system prompt's own distinction).
            kind_note = (
                "several of our transactions plausibly consolidate into one second-document entry"
                if item.kind == "group_match"
                else "several of our transactions might sum to one second-document entry, found by amount alone with no other signal"
            )
            kind_note_reverse = (
                "several second-document entries plausibly consolidate into one of our transactions"
                if item.kind == "group_match"
                else "several second-document entries might sum to one of our transactions, found by amount alone with no other signal"
            )
            if item.row_b is not None:
                lines.append(
                    f"- kind: {item.kind} ({kind_note}) "
                    f"| sap/odoo group: {_format_rows(item.group)} "
                    f"| second doc: {_format_row_side(item.row_b.description, item.row_b.amount)} "
                    f"| status: {status} | explanation: {item.explanation or '-'}"
                )
            else:
                lines.append(
                    f"- kind: {item.kind} ({kind_note_reverse}) "
                    f"| sap/odoo: {_format_row_side(item.row_a.description, item.row_a.amount)} "
                    f"| second doc group: {_format_rows(item.group)} "
                    f"| status: {status} | explanation: {item.explanation or '-'}"
                )
            continue

        a_desc = item.row_a.description if item.row_a else None
        a_amount = item.row_a.amount if item.row_a else None
        b_desc = item.row_b.description if item.row_b else None
        b_amount = item.row_b.amount if item.row_b else None
        kind_text = item.kind
        if item.kind == "amount_mismatch" and item.possible_vat_gap:
            kind_text += " (possible VAT gap)"
        lines.append(
            f"- kind: {kind_text} | sap/odoo: {a_desc or '-'} ({a_amount if a_amount is not None else '-'}) "
            f"| second doc: {b_desc or '-'} ({b_amount if b_amount is not None else '-'}) "
            f"| status: {status} | explanation: {item.explanation or '-'}"
        )
    return "\n".join(lines)


def summarize_exceptions(reviewed: List[ReviewItem], language: str = "en", timeout: int = 60) -> Optional[str]:
    if not QWEN_BRAIN_URL or not reviewed:
        return None

    context = _format_reviewed(reviewed)
    prompt = f"Reconciliation exceptions:\n{context}\n\nSummarize these for the business owner."
    if language == "ar":
        prompt += " Respond in Arabic."

    try:
        response = call_modal_json(QWEN_BRAIN_URL, {
            "messages": [
                {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 300,
            "temperature": 0.2,
        }, timeout=timeout)
    except ModalEndpointError as exc:
        logger.warning("QwenBrain unavailable for exception summary: %s", exc)
        return None

    return strip_latex_math(response.get("content")) or None
