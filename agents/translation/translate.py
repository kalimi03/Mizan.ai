"""
Mizan.ai — "Translate" (Feature B) v1: paragraph-only EN<->AR translation.

No target-language parameter — the whole v1 UX is "paste English or Arabic,
see the other one back," not a language picker, so the source language is
detected and the target is always the other one.
"""

from typing import Dict

from agents.chatbot.langgraph_chatbot import detect_language
from agents.common.modal_client import ModalEndpointError, call_modal_json

from .config import MAX_TEXT_LENGTH, MODAL_TIMEOUT_SECONDS, MODAL_TRANSLATE_URL

_OPPOSITE_LANGUAGE = {"en": "ar", "ar": "en"}


class TranslationError(RuntimeError):
    """Raised for any translation failure the caller should turn into an HTTP error."""


def translate_paragraph(text: str) -> Dict[str, str]:
    stripped = text.strip()
    if not stripped:
        raise TranslationError("Text is empty")
    if len(stripped) > MAX_TEXT_LENGTH:
        raise TranslationError(f"Text exceeds the {MAX_TEXT_LENGTH}-character paragraph limit")

    source_language = detect_language(stripped)
    target_language = _OPPOSITE_LANGUAGE[source_language]

    try:
        body = call_modal_json(
            MODAL_TRANSLATE_URL,
            {"text": stripped, "target_language": target_language},
            timeout=MODAL_TIMEOUT_SECONDS,
        )
    except ModalEndpointError as exc:
        raise TranslationError(str(exc)) from exc

    translated_text = body.get("translated_text")
    if not translated_text:
        raise TranslationError("Translation endpoint returned no text")

    return {
        "translated_text": translated_text,
        "source_language": source_language,
        "target_language": target_language,
    }
