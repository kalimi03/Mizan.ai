"""
Mizan.ai — "Know VAT & ZATCA" offline ingestion CLI.

Usage: python -m agents.know_vat_n_zatca.offline.ingest \
    --manifest path/to/manifest.json --embedding-model-path path/to/bge-m3

manifest.json: a list of {"path", "document_type", "language",
"version_label", "is_current", "source_site", "source_url"} entries — one
per source file. See offline/sample_manifest.json for the shape.

NOT implemented here: the "Fetch" step (pulling source PDFs/XLSX from the
cataloged ZATCA/SOCPA URLs) — that requires the URL catalog from the "RAG
Data Source Strategy" companion document, which wasn't provided as part of
this handoff. This CLI starts from files already present locally (e.g. in
a raw/ folder), tagged via the manifest above. Add a fetch step once the
URL catalog is available.

Also not implemented: the quality-gate table CORRECTION step (requires
human/Claude judgment on merged-cell extraction errors) and the Claude
cross-reference review pass — see CLAUDE_STEPS_SPEC.md. Table registration
happens per-document here (not as a separate whole-corpus phase before
reference detection) because the regex-only reference pass only *tags*
article-number mentions for later online resolution — it doesn't need to
resolve table_refs at ingest time the way the Claude review pass would. If
that pass is added later, this needs restructuring into two phases:
extraction+chunking+table-registration for the whole corpus first, then a
second corpus-wide pass for Claude-reviewed references.
"""

import argparse
import json
import logging
import uuid
from typing import Any, Dict, List

from . import extract, references, table_descriptions
from .chunker import chunk_document, chunk_fallback
from .embed import load_embedding_model, run_embedding, test_embedding_consistency
from ..qdrant_store import upsert_chunks
from ..sqlite_store import initialize_schema, insert_table_rows, register_table, set_table_description

logger = logging.getLogger(__name__)


def load_manifest(path: str) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


_ID_NAMESPACE = uuid.UUID("6f7e9a2e-9c1e-4b3a-8f0a-1c2d3e4f5a6b")


def _stable_id(*parts: str) -> str:
    """Deterministic across process restarts, unlike Python's built-in
    hash() (randomized per-process via PYTHONHASHSEED by default) — using
    that for point IDs meant re-running ingestion never actually overwrote
    a previous run's points, silently accumulating stale duplicates."""
    return str(uuid.uuid5(_ID_NAMESPACE, ":".join(parts)))


def _table_ref(document_type: str, index: int) -> str:
    slug = document_type.lower().replace(" ", "_")
    return f"{slug}_table_{index}"


def ingest_document(entry: Dict[str, Any], embedding_model) -> Dict[str, int]:
    path = entry["path"]
    document_type = entry["document_type"]
    language = entry["language"]
    version_label = entry.get("version_label", "current")
    is_current = entry.get("is_current", True)
    source_site = entry.get("source_site")
    source_url = entry.get("source_url")

    if path.lower().endswith(".pdf"):
        extracted = extract.extract_pdf(path)
        raw_text = extracted["markdown"]
        raw_tables = extracted["tables"]
    elif path.lower().endswith((".xlsx", ".csv")):
        raw_text = ""
        raw_tables = extract.extract_xlsx_or_csv(path)
    else:
        raise ValueError(f"Unsupported file type: {path}")

    prose_chunks = chunk_document(raw_text, document_type, language) if raw_text else []
    if raw_text and not prose_chunks:
        # No "Article N" structure found at all -- a hub overview page or a
        # decimal-numbered guideline, not a numbered regulation. Without
        # this fallback the document's entire prose content is silently
        # dropped (confirmed happening for VAT Hub Overview and
        # E-Invoicing Hub Overview in the 2026-07-22 run: zero chunks,
        # zero errors logged).
        prose_chunks = chunk_fallback(raw_text, document_type, language)
    for chunk in prose_chunks:
        chunk["references"] = references.detect_article_references(
            chunk["text"], chunk["article_number"], language
        )

    table_points = []
    for i, table in enumerate(raw_tables):
        table_ref = _table_ref(document_type, i)
        register_table(
            table_ref=table_ref, document_type=document_type, version_label=version_label,
            language=language, is_current=is_current, source_site=source_site, source_url=source_url,
        )
        insert_table_rows(table_ref, table["rows"])
        description = table_descriptions.generate_placeholder_description(
            document_type=document_type, column_names=table["column_names"],
            rows=table["rows"], language=language,
        )
        set_table_description(table_ref, description)
        table_points.append({"table_ref": table_ref, "text": description})

    all_texts = [c["text"] for c in prose_chunks] + [t["text"] for t in table_points]
    vectors = run_embedding(embedding_model, all_texts) if all_texts else []

    points = []
    for chunk, vector in zip(prose_chunks, vectors[:len(prose_chunks)]):
        points.append({
            "id": _stable_id(
                document_type, version_label, "article", chunk["article_number"],
                str(chunk.get("part_index", 0)),
            ),
            "vector": vector,
            "payload": {
                "content_kind": "prose", "document_type": document_type,
                "article_number": chunk["article_number"], "jurisdiction": "KSA",
                "language": language, "version_label": version_label, "is_current": is_current,
                "references": chunk["references"], "source_site": source_site,
                "source_url": source_url, "text": chunk["text"],
            },
        })

    for table_point, vector in zip(table_points, vectors[len(prose_chunks):]):
        points.append({
            "id": _stable_id(document_type, version_label, "table", table_point["table_ref"]),
            "vector": vector,
            "payload": {
                "content_kind": "table_pointer", "document_type": document_type,
                "table_ref": table_point["table_ref"], "jurisdiction": "KSA",
                "language": language, "version_label": version_label, "is_current": is_current,
                "source_site": source_site, "source_url": source_url, "text": table_point["text"],
            },
        })

    if points:
        upsert_chunks(points)

    return {"prose_chunks": len(prose_chunks), "tables": len(raw_tables)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Know VAT & ZATCA offline ingestion")
    parser.add_argument("--manifest", required=True, help="Path to manifest.json listing source files")
    parser.add_argument("--embedding-model-path", required=True, help="Local path to the BGE-M3 checkpoint")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    initialize_schema()

    manifest = load_manifest(args.manifest)
    logger.info("Loading BGE-M3 embedding model from %s", args.embedding_model_path)
    model = load_embedding_model(args.embedding_model_path)
    test_embedding_consistency(model)
    logger.info("Embedding consistency smoke test passed")

    for entry in manifest:
        try:
            stats = ingest_document(entry, model)
            logger.info("Ingested %s: %s", entry["path"], stats)
        except Exception:
            logger.exception("Failed to ingest %s -- skipping, moving to next document", entry["path"])


if __name__ == "__main__":
    main()
