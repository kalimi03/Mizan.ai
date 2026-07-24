"""
Mizan.ai — "Know VAT & ZATCA" offline chunking.

Structural chunking (article/clause-level), NOT fixed-token windows — legal
meaning lives at the article level, arbitrary splits risk fragments that
read as true but are incomplete. See docs/rag_pipeline_architecture.pdf §4.
"""

import html
import re
from typing import Dict, List, Optional

from ..config import MAX_ARTICLE_WORDS

_ARABIC_INDIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
_DIGIT_TRANSLATION = str.maketrans(_ARABIC_INDIC_DIGITS, "0123456789")

# Arabic articles are chunked straight off the flat markdown text: every
# ZATCA/SOCPA Arabic source seen so far reliably prints "المادة N:" (with a
# trailing colon) only at real article headings, never as a same-line
# citation — confirmed against the real ingestion run (XML Implementation
# Standard, Security Features Standard both chunked cleanly this way), so
# it's left alone.
_ARTICLE_PATTERN_AR = re.compile(r"المادة\s+([0-9٠-٩]+)\s*:")

# English sources are not this well-behaved — two real documents broke the
# old flat-text "Article N:" pattern in different ways (found while
# auditing the 2026-07-22 ingestion run, which produced zero prose chunks
# for both):
#   - VAT Implementing Regulations: most article headings never print
#     "Article N" at all (just an all-caps title); the handful that do
#     spell the number as a word ("ARTICLE FORTY-THREE"), not a digit.
#   - GCC Unified VAT Agreement: headings are "Article (35)" alone on a
#     line with NO trailing colon, title on the next line.
# Boundaries are now taken from Docling's markdown headings (`#...`)
# instead of a flat-text regex — see split_into_articles_en() for the
# heuristic this needs in place of a single pattern.
_HEADING_LINE = re.compile(r"^#{1,6}[ \t]*(.*)$", re.MULTILINE)
_CHAPTER_DIVIDER_EN = re.compile(r"^(chapter|part)\b", re.IGNORECASE)
_ARTICLE_TAG_EN = re.compile(r"\barticle\s*\(?([a-z-]+|[0-9]+)\)?", re.IGNORECASE)

# A THIRD document shape, found auditing SOCPA Accountants Regulations:
# Docling promoted only 2 of its 38 "Article (N) :" markers to real markdown
# headings — the other 36 are plain inline text in flowing paragraphs, so
# heading-only detection swallowed the entire rest of the document into
# "Article 2"'s body. This colon-terminated pattern, scanned over the flat
# text (excluding spans already covered by a heading — see
# split_into_articles_en), recovers those never-promoted-to-heading
# markers without reintroducing the old bug of matching mid-sentence
# citations (which don't carry a trailing colon).
_ARTICLE_TAG_COLON_EN = re.compile(r"\barticle\s*\(?([a-z-]+|[0-9]+)\)?\s*:", re.IGNORECASE)

_ONES = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}


def _english_word_to_number(word: str) -> Optional[int]:
    """"forty-three" / "FORTY THREE" / "seven" -> 43 / 43 / 7. None for
    anything that isn't a recognized number word — not every string after
    "Article" in a heading actually is one (OCR noise, stray characters)."""
    word = word.lower().strip()
    if word in _ONES:
        return _ONES[word]
    if word in _TENS:
        return _TENS[word]
    for sep in ("-", " "):
        if sep in word:
            tens_word, _, ones_word = word.partition(sep)
            if tens_word in _TENS and ones_word in _ONES:
                return _TENS[tens_word] + _ONES[ones_word]
    return None


def _normalize_article_number(raw: str) -> str:
    return raw.translate(_DIGIT_TRANSLATION)


def _normalize_english_tag(token: str) -> Optional[str]:
    """Digit or word-form article number -> canonical digit string, or None
    if the token doesn't actually resolve to a number."""
    if token.isdigit():
        return str(int(token))
    number = _english_word_to_number(token)
    return str(number) if number is not None else None


def _looks_like_heading_title(text: str) -> bool:
    """Distinguishes a real (untagged) article title from a stray fragment
    Docling occasionally promotes to a heading — e.g. a two-line fraction
    label ("Initial Input Tax deduction" / "adjustment period") split out
    of a formula in the VAT Implementing Regulations PDF. Real titles in
    that source are consistently printed in ALL CAPS; spurious ones are
    not — checked on letters only (via html.unescape first) so an
    HTML-escaped "&amp;" doesn't cause a false rejection on an otherwise
    all-caps title."""
    letters = re.sub(r"[^A-Za-z]", "", html.unescape(text))
    if not letters:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return upper / len(letters) >= 0.7


def split_into_articles_ar(text: str) -> List[Dict[str, str]]:
    matches = list(_ARTICLE_PATTERN_AR.finditer(text))
    if not matches:
        return []
    articles = []
    for i, match in enumerate(matches):
        article_number = _normalize_article_number(match.group(1))
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip().lstrip(":-،  ").strip()
        articles.append({"article_number": article_number, "body": body})
    return articles


def split_into_articles_en(text: str) -> List[Dict[str, str]]:
    """Boundaries come from two merged sources, in document order (see the
    module comments above for why each is needed):
      A. Markdown headings — a boundary if the heading either explicitly
         tags a number ("Article 12", "ARTICLE FORTY-THREE", "Article
         (35)" — digit or word, with or without a colon/parens), or is an
         ALL-CAPS title with no tag once inside the document body
         (untagged, non-caps headings — e.g. GCC's "Scope of Tax"
         sub-titles — merge into the preceding article's body instead).
      B. Colon-terminated "Article N:" mentions anywhere OUTSIDE a heading
         line — catches documents (e.g. SOCPA) where most article markers
         were never promoted to real markdown headings by Docling at all.
    Content before the first chapter/tagged/colon-tagged marker (title
    page, table of contents) is dropped, same as before."""
    headings = list(_HEADING_LINE.finditer(text))
    heading_spans = [(m.start(), m.end()) for m in headings]

    def _inside_a_heading(pos: int) -> bool:
        return any(s <= pos < e for s, e in heading_spans)

    # candidate: (position, body_start_pos, tag_number_or_None, is_chapter_divider, heading_text_or_None)
    candidates = []
    for m in headings:
        heading_text = m.group(1).strip()
        if not heading_text:
            continue
        tag_match = _ARTICLE_TAG_EN.search(heading_text)
        tag_number = _normalize_english_tag(tag_match.group(1)) if tag_match else None
        is_chapter_divider = _CHAPTER_DIVIDER_EN.match(heading_text) is not None
        candidates.append((m.start(), m.end(), tag_number, is_chapter_divider, heading_text))

    for m in _ARTICLE_TAG_COLON_EN.finditer(text):
        if _inside_a_heading(m.start()):
            continue  # already covered by a heading candidate at (almost) this position
        tag_number = _normalize_english_tag(m.group(1))
        if tag_number is not None:
            candidates.append((m.start(), m.end(), tag_number, False, None))

    candidates.sort(key=lambda c: c[0])

    boundaries = []  # (body_start_pos, article_number)
    started = False
    next_number = 1
    for _, end, tag_number, is_chapter_divider, heading_text in candidates:
        if not started:
            if tag_number is not None or is_chapter_divider:
                started = True
            else:
                continue

        if tag_number is not None:
            boundaries.append((end, tag_number))
            next_number = int(tag_number) + 1
        elif is_chapter_divider:
            continue
        elif heading_text is not None and _looks_like_heading_title(heading_text):
            boundaries.append((end, str(next_number)))
            next_number += 1
        # else: spurious heading (e.g. a formula fragment) — merges into
        # the current article's body, not a boundary.

    if not boundaries:
        return []

    articles = []
    for i, (start, article_number) in enumerate(boundaries):
        end = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(text)
        body = text[start:end].strip()
        articles.append({"article_number": article_number, "body": body})
    return articles


def split_into_articles(text: str, language: str) -> List[Dict[str, str]]:
    """Splits raw extracted text into one segment per article, in document
    order. Text before the first real article marker (e.g. a preamble) is
    dropped — not article content."""
    if language == "ar":
        return split_into_articles_ar(text)
    return split_into_articles_en(text)


def _sub_split_if_long(body: str, max_words: int) -> List[str]:
    words = body.split()
    if len(words) <= max_words:
        return [body]
    return [" ".join(words[i:i + max_words]) for i in range(0, len(words), max_words)]


def build_context_header(document_type: str, article_number: str, language: str) -> str:
    """In-language context header, embedded into the chunk TEXT itself (not
    just metadata) so retrieval matches on chapter/topic context, not just
    bare clause wording."""
    if language == "ar":
        return f"{document_type}، المادة {article_number}:"
    return f"{document_type}, Article {article_number}:"


def chunk_document(
    text: str,
    document_type: str,
    language: str,
    max_words: int = MAX_ARTICLE_WORDS,
) -> List[Dict]:
    """One chunk per article; sub-split only if an article exceeds
    max_words. No overlap between chunks — chunking on natural article
    boundaries makes overlap unnecessary (it would only create
    near-duplicate chunks in Qdrant)."""
    chunks = []
    for article in split_into_articles(text, language):
        header = build_context_header(document_type, article["article_number"], language)
        pieces = _sub_split_if_long(article["body"], max_words)
        for i, piece in enumerate(pieces):
            suffix = f" (part {i + 1}/{len(pieces)})" if len(pieces) > 1 else ""
            chunks.append({
                "article_number": article["article_number"],
                "part_index": i,
                "text": f"{header}{suffix} {piece}",
                "word_count": len(piece.split()),
            })
    return chunks


def _fallback_header(document_type: str, index: int, language: str) -> str:
    if language == "ar":
        return f"{document_type}، القسم {index}:"
    return f"{document_type}, Section {index}:"


def chunk_fallback(
    text: str,
    document_type: str,
    language: str,
    max_words: int = MAX_ARTICLE_WORDS,
) -> List[Dict]:
    """For documents with no article-level structure at all — e.g. a ZATCA
    "hub overview" info page, or a guideline organized by decimal section
    numbers ("2.1.1") rather than "Article N". chunk_document() finds no
    boundaries on these and returns [], which would otherwise silently
    drop 100% of the document's content (confirmed happening for real —
    VAT Hub Overview and E-Invoicing Hub Overview both ingested as zero
    chunks in the 2026-07-22 run). Chunks by Docling's own markdown
    headings when the document has any (keeps real section boundaries,
    e.g. the Amendments guideline's "2.1.1 Requirements for..." sections);
    falls back further to blank-line-separated paragraph groups for
    documents with no heading structure either (the hub overview pages).
    Identifiers are synthetic ("fallback-1", "fallback-2"...), distinct
    from real digit article numbers — content chunked this way was never
    going to be cited by article number."""
    headings = list(_HEADING_LINE.finditer(text))
    sections: List[str] = []
    if headings:
        for i, m in enumerate(headings):
            start = m.start()
            end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            section = text[start:end].strip()
            if section:
                sections.append(section)
    else:
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        current: List[str] = []
        current_words = 0
        for para in paragraphs:
            para_words = len(para.split())
            if current and current_words + para_words > max_words:
                sections.append("\n\n".join(current))
                current, current_words = [], 0
            current.append(para)
            current_words += para_words
        if current:
            sections.append("\n\n".join(current))

    chunks = []
    for i, section in enumerate(sections, start=1):
        header = _fallback_header(document_type, i, language)
        pieces = _sub_split_if_long(section, max_words)
        for j, piece in enumerate(pieces):
            suffix = f" (part {j + 1}/{len(pieces)})" if len(pieces) > 1 else ""
            chunks.append({
                "article_number": f"fallback-{i}",
                "part_index": j,
                "text": f"{header}{suffix} {piece}",
                "word_count": len(piece.split()),
            })
    return chunks
