"""
Mizan.ai — shared helper for calling internal, docker-compose-local HTTP
services (as opposed to modal_client.py's remote Modal endpoints).

First user: the data_extraction service (Feature E, Phase 1) — a file
upload rather than a JSON payload, so it needs multipart handling
modal_client.py's call_modal_json() doesn't do. Same
request/timeout/error pattern otherwise, deliberately mirrored so callers
don't need to learn a second error-handling shape.
"""

from typing import Optional

import requests


class InternalServiceError(RuntimeError):
    """Raised when an internal service call fails or is misconfigured.

    status_code carries the downstream service's HTTP status when the
    failure was a real response with a 4xx/5xx (e.g. doc-extraction
    rejecting an unsupported/scanned file with a 422 and a clear detail
    message) — None for connection failures/timeouts, where there's no
    downstream status to report. Callers that want to distinguish "your
    input was rejected" (surface the message, e.g. as their own 422) from
    "the service is unreachable" (502) can check this.
    """

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def _error_detail(response: requests.Response) -> Optional[str]:
    """Pulls FastAPI's {"detail": "..."} out of an error response body, if
    present — without this, raise_for_status()'s HTTPError discards the
    original response body entirely, and every downstream 4xx collapses
    into the same generic "internal service call failed" message
    regardless of what the service actually said was wrong."""
    try:
        body = response.json()
    except ValueError:
        return None
    return body.get("detail") if isinstance(body, dict) else None


def post_file(url: str, file_path: str, filename: str, timeout: int = 120) -> dict:
    if not url:
        raise InternalServiceError("Internal service URL is not configured")

    try:
        with open(file_path, "rb") as f:
            response = requests.post(url, files={"file": (filename, f)}, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except requests.Timeout as exc:
        raise InternalServiceError(f"Internal service timed out after {timeout}s: {url}") from exc
    except requests.HTTPError as exc:
        detail = _error_detail(exc.response)
        message = detail or f"Internal service call failed: {url}: {exc}"
        raise InternalServiceError(message, status_code=exc.response.status_code) from exc
    except requests.RequestException as exc:
        raise InternalServiceError(f"Internal service call failed: {url}: {exc}") from exc


def post_json(url: str, payload: dict, timeout: int = 30) -> dict:
    """JSON-payload sibling to post_file() — same error-handling shape.
    First user: features/calculator/report.py calling Translator's
    internal endpoint across the service boundary.
    """
    if not url:
        raise InternalServiceError("Internal service URL is not configured")

    try:
        response = requests.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except requests.Timeout as exc:
        raise InternalServiceError(f"Internal service timed out after {timeout}s: {url}") from exc
    except requests.HTTPError as exc:
        detail = _error_detail(exc.response)
        message = detail or f"Internal service call failed: {url}: {exc}"
        raise InternalServiceError(message, status_code=exc.response.status_code) from exc
    except requests.RequestException as exc:
        raise InternalServiceError(f"Internal service call failed: {url}: {exc}") from exc
