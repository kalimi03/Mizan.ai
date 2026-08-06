"""
Mizan.ai — "Know VAT & ZATCA" online retrieval logic.

Read-only against Qdrant + SQLite — this pipeline never writes to the
knowledge base. See docs/rag_pipeline_architecture.pdf §3 for the full
step-by-step design this implements.
"""

from typing import Any, Dict, List, Tuple

from features.common.modal_client import call_modal_json

from ..config import CONTEXT_CHUNK_CAP, MODAL_EMBEDDER_URL, MODAL_TIMEOUT_SECONDS, RELEVANCE_THRESHOLD
from ..offline.table_descriptions import format_row
from ..qdrant_store import find_by_composite_key, search
from ..sqlite_store import get_table_rows


def _format_table_rows(rows: List[Dict[str, Any]]) -> str:
    """One row per line, "key: value | key: value" style — reuses the same
    per-row formatting as the offline embedding description (see
    offline/table_descriptions.py), instead of Python's raw list-of-dicts
    repr() this used to produce. Found via a real user report: a query
    ("what is the penalty for late tax payment") retrieved the right table
    (its citation showed up), but the model still failed to surface the
    specific answer — the raw repr() text ({'key': 'val', ...} syntax
    packed across 10 rows) was hard to reliably parse/quote from. One
    clearly delimited fact per line is much easier for the model to
    extract precisely."""
    fragments = [format_row(row) for row in rows]
    return "\n".join(f"- {fragment}" for fragment in fragments if fragment)


def embed_query(text: str) -> List[float]:
    body = call_modal_json(MODAL_EMBEDDER_URL, {"texts": [text]}, timeout=MODAL_TIMEOUT_SECONDS)
    embeddings = body.get("embeddings")
    if not embeddings:
        raise RuntimeError("Embedder endpoint returned no embeddings")
    return embeddings[0]


def _above_threshold(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [c for c in chunks if c["score"] >= RELEVANCE_THRESHOLD]


def search_with_fallback(query_vector: List[float], language: str) -> Tuple[List[Dict[str, Any]], bool]:
    """Same-language-first, retry once without the language filter if
    empty/thin. Returns (relevant_chunks, used_cross_language_fallback)."""
    same_language = _above_threshold(search(query_vector, language=language))
    if same_language:
        return same_language, False

    cross_language = _above_threshold(search(query_vector, language=None))
    return cross_language, bool(cross_language)


def resolve_table_pointer(chunk_payload: Dict[str, Any]) -> Dict[str, Any]:
    table_ref = chunk_payload["table_ref"]
    rows = get_table_rows(table_ref)
    return {
        "content_kind": "table_rows",
        "table_ref": table_ref,
        "document_type": chunk_payload.get("document_type"),
        "jurisdiction": chunk_payload.get("jurisdiction"),
        "version_label": chunk_payload.get("version_label"),
        "is_current": chunk_payload.get("is_current"),
        "source_site": chunk_payload.get("source_site"),
        "source_url": chunk_payload.get("source_url"),
        "text": f"Table data for {table_ref}:\n{_format_table_rows(rows)}",
    }


def resolve_cross_references(chunk_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One hop only — a reference's own further references are not chased."""
    resolved = []
    for ref in chunk_payload.get("references") or []:
        if ref.get("type") == "table":
            table_ref = ref.get("table_ref")
            if table_ref:
                rows = get_table_rows(table_ref)
                resolved.append({
                    "content_kind": "table_rows",
                    "table_ref": table_ref,
                    "text": f"Referenced table data for {table_ref}:\n{_format_table_rows(rows)}",
                })
        elif ref.get("type") == "article":
            # version_label is inherited from the CITING chunk, not stored on the reference itself
            found = find_by_composite_key(
                document_type=chunk_payload.get("document_type"),
                version_label=chunk_payload.get("version_label"),
                article_number=ref.get("article_number"),
            )
            if found:
                resolved.append(found["payload"])
    return resolved


def resolve_and_cap(original_chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Resolves table pointers + one-hop cross-references for every
    retrieved chunk, then caps the total at CONTEXT_CHUNK_CAP.

    Table matches go first, ahead of prose — every chunk here already
    cleared RELEVANCE_THRESHOLD, but a table's placeholder description
    (see table_descriptions.py) still tends to score lower than a real
    prose passage even when it's the more directly useful answer (e.g.
    a penalty-amounts question genuinely needs the penalties TABLE, not
    just prose that happens to mention "penalty"). Putting table matches
    first means they survive the cap instead of a lower-priority prose
    chunk crowding them out — confirmed necessary by testing: a real
    table match at rank 7 was silently dropped by simple score-order
    truncation. Cross-reference resolutions still fill in last, same as
    before."""
    resolved_extra: List[Dict[str, Any]] = []
    table_originals: List[Dict[str, Any]] = []
    prose_originals: List[Dict[str, Any]] = []

    for chunk in original_chunks:
        payload = chunk["payload"]
        if payload.get("content_kind") == "table_pointer":
            table_originals.append(resolve_table_pointer(payload))
        else:
            prose_originals.append(payload)
            resolved_extra.extend(resolve_cross_references(payload))

    combined = table_originals + prose_originals + resolved_extra
    return combined[:CONTEXT_CHUNK_CAP]
