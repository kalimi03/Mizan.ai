"""
Mizan.ai — shared helper for calling deployed Modal endpoints over HTTP.

Extracted so any agent module (chatbot, know_vat_n_zatca, future features)
can reuse the same request/timeout/error pattern instead of each
reimplementing it. features/chatbot/langgraph_chatbot.py predates this and
calls requests.post directly inline — left as-is per the standing rule not
to touch working chatbot code; new features should use this instead.
"""

import requests


class ModalEndpointError(RuntimeError):
    """Raised when a Modal endpoint call fails or is misconfigured."""


def call_modal_json(url: str, payload: dict, timeout: int = 600) -> dict:
    if not url:
        raise ModalEndpointError("Modal endpoint URL is not configured")

    try:
        response = requests.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except requests.Timeout as exc:
        raise ModalEndpointError(f"Modal endpoint timed out after {timeout}s: {url}") from exc
    except requests.RequestException as exc:
        raise ModalEndpointError(f"Modal endpoint call failed: {url}: {exc}") from exc
