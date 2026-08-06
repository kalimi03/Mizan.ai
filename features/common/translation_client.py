"""
Mizan.ai — shared best-effort translation helper for report generation.

Used by any service that renders a bilingual report from data that may not
already be in the target language (Calculator's VAT reports, Comparator's
reconciliation reports). Calls the Translator service's internal,
unauthenticated endpoint (see services/translator/main.py's
POST /internal/translate) rather than importing features/translator/translate.py
directly — Translator is its own service/container, this is a real
cross-service HTTP call, not an in-process import.
"""

from features.common.http_client import post_json
from features.common.language import detect_language


def maybe_translate(text: str, target_language: str, translator_url: str) -> str:
    """Best-effort: translates text if it's not already in target_language.
    Falls back to the original text on any failure — a report with one
    untranslated line beats no report at all.

    Checks detect_language() (a cheap local regex check, no network call)
    first and only calls the network-based Translator endpoint when the
    text actually isn't already in the target language — avoids an
    unnecessary round-trip (and its cold-start latency) for the common case
    of a report generated in the same language as the source data.
    """
    if not text:
        return text
    try:
        if detect_language(text) == target_language:
            return text
    except Exception:
        return text  # can't even tell — leave the text as-is rather than risk a slow call

    try:
        result = post_json(translator_url, {"text": text})
        return result["translated_text"]
    except Exception:
        return text
