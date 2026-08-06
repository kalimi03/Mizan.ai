"""
Mizan.ai — natural-language Q&A over a user's filing history.

Query layer only, no LangGraph graph — Feature E (the ZATCA compliance
calculator this data belongs to) doesn't have a graph yet, and inventing
one here would mean designing Feature E's architecture as a side effect of
the auth task. See docs/mizan_backend_auth_handoff_v3.pdf §5.8.
"""

import os
from typing import Optional

import requests

from features.common.db import get_filing_notes_for_customer, get_last_filing_note
from features.common.text_cleanup import strip_latex_math

_MODEL_URL = (
    os.getenv("QWEN_LITE_URL")
    or os.getenv("MIZAN_QWEN_LITE_URL")
    or os.getenv("MODAL_QWEN_LITE_URL")
)

_QA_SYSTEM_PROMPT = (
    "You answer questions about a user's filing history using ONLY the "
    "filing records provided below. Never invent a record, customer, or "
    "detail that isn't in the provided data. Answer in the same language "
    "as the question."
)


def _format_rows(rows: list) -> str:
    return "\n".join(
        f"- customer: {row.get('customer_name')} | status: {row.get('status')} | "
        f"notes: {row.get('notes')} | filed: {row.get('created_at')}"
        for row in rows
    )


def answer_filing_question(
    user_id: str,
    question: str,
    customer_name: Optional[str] = None,
    model_url: Optional[str] = None,
    timeout: int = 60,
) -> str:
    """No customer named -> most recent filing note for the user. Customer
    named -> filtered to that customer; if there's no match, say so rather
    than fabricating an answer (never calls the model in that case).
    """
    if customer_name:
        rows = get_filing_notes_for_customer(user_id, customer_name)
        if not rows:
            return f"I don't have any filing records for {customer_name}."
    else:
        last = get_last_filing_note(user_id)
        rows = [last] if last else []
        if not rows:
            return "I don't have any filing records for you yet."

    model_url = model_url or _MODEL_URL
    context = _format_rows(rows)

    if not model_url:
        return context

    payload = {
        "messages": [
            {"role": "system", "content": _QA_SYSTEM_PROMPT},
            {"role": "user", "content": f"Filing records:\n{context}\n\nQuestion: {question}"},
        ],
        "max_tokens": 256,
        "temperature": 0.2,
    }
    try:
        response = requests.post(model_url, json=payload, timeout=timeout)
        response.raise_for_status()
        body = response.json()
        content = body.get("content") or body.get("reply") or body.get("message")
        if content:
            return strip_latex_math(str(content))
    except requests.RequestException:
        pass

    return context
