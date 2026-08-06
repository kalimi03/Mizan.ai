"""
Mizan.ai — "Know VAT & ZATCA" vector store (Qdrant).

Used by both the offline ingestion pipeline (writes) and the online
retrieval pipeline (read-only search). Single collection, payload-filtered
— see docs/rag_pipeline_architecture.pdf §6 for the full payload schema.

Direct writes to the live collection (no staging/swap pattern) — agreed
with Mohammed 2026-07-20.
"""

from typing import Any, Dict, List, Optional

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from .config import EMBEDDING_DIM, QDRANT_COLLECTION, QDRANT_URL

_client: Optional[QdrantClient] = None


def get_client() -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(url=QDRANT_URL)
    return _client


def ensure_collection() -> None:
    client = get_client()
    existing = {c.name for c in client.get_collections().collections}
    if QDRANT_COLLECTION in existing:
        return
    client.create_collection(
        collection_name=QDRANT_COLLECTION,
        vectors_config=qmodels.VectorParams(size=EMBEDDING_DIM, distance=qmodels.Distance.COSINE),
    )


def upsert_chunks(points: List[Dict[str, Any]]) -> None:
    """Each point: {id, vector, payload} — payload carries the full schema
    (content_kind, document_type, article_number|table_ref, jurisdiction,
    language, version_label, is_current, effective_start_date,
    effective_end_date, references, source_site, source_url, text)."""
    client = get_client()
    ensure_collection()
    client.upsert(
        collection_name=QDRANT_COLLECTION,
        points=[
            qmodels.PointStruct(id=point["id"], vector=point["vector"], payload=point["payload"])
            for point in points
        ],
    )


def search(
    query_vector: List[float],
    language: Optional[str] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Same-language-first retrieval: pass language to filter, or None for
    the cross-language fallback retry."""
    client = get_client()
    query_filter = None
    if language:
        query_filter = qmodels.Filter(
            must=[qmodels.FieldCondition(key="language", match=qmodels.MatchValue(value=language))]
        )
    results = client.query_points(
        collection_name=QDRANT_COLLECTION,
        query=query_vector,
        query_filter=query_filter,
        limit=limit,
        with_payload=True,
    ).points
    return [{"id": r.id, "score": r.score, "payload": r.payload} for r in results]


def find_by_composite_key(
    document_type: str,
    version_label: str,
    article_number: str,
) -> Optional[Dict[str, Any]]:
    """One-hop cross-reference resolution for type:article references."""
    client = get_client()
    results, _ = client.scroll(
        collection_name=QDRANT_COLLECTION,
        scroll_filter=qmodels.Filter(
            must=[
                qmodels.FieldCondition(key="document_type", match=qmodels.MatchValue(value=document_type)),
                qmodels.FieldCondition(key="version_label", match=qmodels.MatchValue(value=version_label)),
                qmodels.FieldCondition(key="article_number", match=qmodels.MatchValue(value=article_number)),
            ]
        ),
        limit=1,
        with_payload=True,
    )
    if not results:
        return None
    point = results[0]
    return {"id": point.id, "payload": point.payload}
