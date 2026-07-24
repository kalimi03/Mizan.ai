"""
Mizan.ai — "Know VAT & ZATCA" fail-closed fallback messages.

Exact, finalized bilingual text — not to be paraphrased or regenerated.
See docs/rag_agent_handoff.pdf §7.3 (wrong-jurisdiction, nothing-found —
locked from the start) and the cross-language wrapper (drafted and signed
off by Mohammed, 2026-07-20).
"""

from typing import Optional

from .config import NON_KSA_GCC_COUNTRIES

WRONG_JURISDICTION_EN = (
    "Mizan.ai currently supports Saudi Arabia (ZATCA/KSA) tax and compliance "
    "guidance only. We don't yet cover other GCC countries. If you need "
    "support for {country}, please reach out to our team — we're always "
    "looking to understand where to expand next."
)

WRONG_JURISDICTION_AR = (
    "يدعم Mizan.ai حاليا الإرشادات الضريبية والامتثال الخاصة بالمملكة العربية "
    "السعودية هيئة الزكاة والضريبة والجمارك فقط، ولا يغطي دول مجلس التعاون "
    "الخليجي الأخرى بعد. إذا كنت بحاجة إلى دعم لـ {country}، يرجى التواصل مع "
    "فريقنا - نحن نتطلع دائما إلى معرفة أين نوسع خدماتنا."
)

NOTHING_FOUND_EN = (
    "I wasn't able to find a clear answer to this in Mizan.ai's current "
    "knowledge base. Rather than guess, I'd rather be upfront that this "
    "isn't covered with enough detail yet. For help with this specific "
    "question, please reach out to our team."
)

NOTHING_FOUND_AR = (
    "لم أتمكن من العثور على إجابة واضحة لهذا السؤال ضمن قاعدة معارف Mizan.ai "
    "الحالية. بدلا من التخمين، أفضل أن أكون واضحا بأن هذا الموضوع غير مغطى "
    "بتفصيل كاف حتى الآن. للحصول على المساعدة بخصوص هذا السؤال تحديدا، يرجى "
    "التواصل مع فريقنا."
)

CROSS_LANGUAGE_WRAPPER_EN = (
    "I found relevant information for this, but currently only in "
    "{content_language} — not yet in {query_language} within Mizan.ai's "
    "knowledge base. To avoid changing the meaning of official tax and "
    "compliance text, I'm showing it below exactly as published, without "
    "translating it. If you'd like a translation, you're welcome to use "
    "Mizan.ai's translation feature."
)

CROSS_LANGUAGE_WRAPPER_AR = (
    "وجدت معلومات ذات صلة بهذا السؤال، لكنها متوفرة حاليا باللغة "
    "{content_language} فقط ضمن قاعدة معارف Mizan.ai، وليست بعد باللغة "
    "{query_language}. تجنبا لتغيير معنى النصوص الضريبية والامتثالية "
    "الرسمية، أعرضها أدناه كما نُشرت تماما دون ترجمة. إذا رغبت بالحصول على "
    "ترجمة، يمكنك استخدام ميزة الترجمة في Mizan.ai."
)

_LANGUAGE_DISPLAY_NAME = {
    "ar": {"ar": "العربية", "en": "Arabic"},
    "en": {"ar": "الإنجليزية", "en": "English"},
}


def detect_non_ksa_country(text: str) -> Optional[str]:
    """Cheap keyword check — the only pre-search jurisdiction filter.
    Returns the canonical (English) country name if an explicit non-KSA GCC
    country is named, else None.
    """
    lowered = text.lower()
    for keyword, canonical_name in NON_KSA_GCC_COUNTRIES.items():
        if keyword in lowered:
            return canonical_name
    return None


def wrong_jurisdiction_message(country: str, language: str) -> str:
    template = WRONG_JURISDICTION_AR if language == "ar" else WRONG_JURISDICTION_EN
    return template.format(country=country)


def nothing_found_message(language: str) -> str:
    return NOTHING_FOUND_AR if language == "ar" else NOTHING_FOUND_EN


def cross_language_wrapper_message(query_language: str, content_language: str) -> str:
    template = CROSS_LANGUAGE_WRAPPER_AR if query_language == "ar" else CROSS_LANGUAGE_WRAPPER_EN
    return template.format(
        content_language=_LANGUAGE_DISPLAY_NAME[content_language][query_language],
        query_language=_LANGUAGE_DISPLAY_NAME[query_language][query_language],
    )
