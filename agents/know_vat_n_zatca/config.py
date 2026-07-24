"""
Mizan.ai — "Know VAT & ZATCA" (Feature A / RAG) shared config.

Single source of truth for the tunables both the offline ingestion
pipeline and the online retrieval pipeline need to agree on. See
docs/rag_agent_handoff.pdf and docs/rag_pipeline_architecture.pdf for the
reasoning behind each value.
"""

import os
from pathlib import Path

# --- Vector store -----------------------------------------------------------

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_COLLECTION = "know_vat_n_zatca"
EMBEDDING_DIM = 1024  # BGE-M3 dense vectors

# --- Structured store ---------------------------------------------------

# Plain file, no Docker/server involved (SQLite isn't a client-server DB) —
# both the offline ingestion pipeline and the online gateway process read
# from this same path.
SQLITE_DB_PATH = os.getenv(
    "KNOW_VAT_ZATCA_SQLITE_PATH",
    str(Path(__file__).resolve().parent / "data" / "know_vat_n_zatca.db"),
)

# --- Retrieval tuning ---------------------------------------------------

# Cosine-similarity cutoff below which a match is treated as "not actually
# relevant" -> nothing-found fallback, even though Qdrant always returns
# *something*. Starting value, agreed with Mohammed 2026-07-20 — recalibrate
# once real queries are being tested.
RELEVANCE_THRESHOLD = 0.5

# Total chunks (original retrieval + resolved table pointers/cross-refs),
# post-expansion. Originals are prioritized over resolved references if the
# cap is hit.
CONTEXT_CHUNK_CAP = 6

# Small table -> one whole-table pointer; large table (Data Dictionary, code
# lists) -> sharded per-row/group pointers. Decided at ingestion by row count.
TABLE_SHARD_ROW_THRESHOLD = 20

# --- Chunking ---------------------------------------------------------------

# Sub-split an article only if it exceeds this word count.
MAX_ARTICLE_WORDS = 500

# --- Conversation history -----------------------------------------------

# (query, response) pairs only, same shape as langgraph_chatbot.py's
# dual-window memory — not a new memory system.
MAX_HISTORY_TURNS = 8

# --- Jurisdiction ------------------------------------------------------

JURISDICTION = "KSA"

# Cheap, editable keyword check for an explicit non-KSA GCC country — the
# ONLY pre-search scope check (everything else is a post-search relevance
# judgment, since reliable pre-search out-of-scope classification isn't
# possible — VAT and customs vocabulary overlap too much).
NON_KSA_GCC_COUNTRIES = {
    "bahrain": "Bahrain",
    "البحرين": "Bahrain",
    "uae": "UAE",
    "united arab emirates": "UAE",
    "الإمارات": "UAE",
    "الامارات": "UAE",
    "oman": "Oman",
    "عمان": "Oman",
    "qatar": "Qatar",
    "قطر": "Qatar",
    "kuwait": "Kuwait",
    "الكويت": "Kuwait",
}

# --- Modal endpoints (reuse the existing deployed models, no new deploys) --

MODAL_EMBEDDER_URL = (
    os.getenv("MIZAN_EMBEDDER_URL")
    or os.getenv("MODAL_EMBEDDER_URL")
)

MODAL_QWEN_LITE_URL = (
    os.getenv("MIZAN_QWEN_LITE_URL")
    or os.getenv("QWEN_LITE_URL")
    or os.getenv("MODAL_QWEN_LITE_URL")
)

MODAL_TIMEOUT_SECONDS = int(os.getenv("MIZAN_CHATBOT_TIMEOUT_SECONDS", "600"))
