"""
Mizan.ai — "Know VAT & ZATCA" cross-reference detection.

Regex-only first pass, implementable without Claude — finds explicit
"Article N" / "المادة N" citations within a chunk's own text. The Claude
review pass that also catches indirect/spelled-out references and table
citations is NOT implemented here — see CLAUDE_STEPS_SPEC.md for that
piece's spec. This regex pass alone still catches the large majority of
citations, since ZATCA/SOCPA legal text almost always cites articles by
their explicit number.
"""

import re
from typing import Dict, List

from .chunker import _normalize_article_number, _normalize_english_tag

# Deliberately more permissive than chunker.py's boundary patterns (which
# require a heading, not just any mention in flat text). Here, operating on
# an already-delimited chunk's body text, any "Article N" mention — digit
# or word-form, with or without a colon/parens — is a citation to another
# article, e.g. "...subject to Article 25." or "...pursuant to Article
# forty-six..." or "...Article (35)..." are all real citations. Word-form
# support was added after finding the VAT Implementing Regulations PDF
# cites articles almost exclusively by spelled-out number ("Article
# seventy-seven"), which the old digit-only pattern silently caught none
# of.
_MENTION_PATTERN_EN = re.compile(r"Articles?\s*\(?([A-Za-z-]+|[0-9]+)\)?", re.IGNORECASE)
_MENTION_PATTERN_AR = re.compile(r"المادة\s+([0-9٠-٩]+)")


def _mention_pattern_for_language(language: str) -> re.Pattern:
    return _MENTION_PATTERN_AR if language == "ar" else _MENTION_PATTERN_EN


def detect_article_references(text: str, own_article_number: str, language: str) -> List[Dict[str, str]]:
    """Finds mentions of a DIFFERENT article within this chunk's own text
    (self-references to the article's own number are dropped, as are
    duplicate mentions of the same target)."""
    pattern = _mention_pattern_for_language(language)
    references: List[Dict[str, str]] = []
    seen = set()

    for match in pattern.finditer(text):
        if language == "ar":
            article_number = _normalize_article_number(match.group(1))
        else:
            article_number = _normalize_english_tag(match.group(1))
            if article_number is None:
                continue
        if article_number == own_article_number or article_number in seen:
            continue
        seen.add(article_number)
        references.append({"type": "article", "article_number": article_number})

    return references
