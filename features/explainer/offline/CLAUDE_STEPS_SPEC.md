# Deferred steps — Anthropic/Claude API (spec only, no code)

Per Mohammed's instruction (2026-07-20): this repo does not call the
Anthropic API anywhere. The two pipeline steps that the handoff doc
(`docs/rag_agent_handoff.pdf` §3.1, offline tasks 7-8) specifies as
Claude-driven are documented here as a spec instead of being implemented.
Everything else in the offline pipeline (extraction, chunking, regex-only
cross-reference detection, embedding, Qdrant/SQLite writes) is real,
working code — only these two pieces are placeholder-only.

## 1. Table description generation

**Where it plugs in:** after a table is extracted and its rows are stored
via `sqlite_store.insert_table_rows()`, before it's embedded and written to
Qdrant as a `table_pointer` chunk. Currently handled by
`table_descriptions.generate_placeholder_description()` — a deterministic,
non-AI string (document type + column names + row count), just detailed
enough to keep the table-pointer pattern functionally testable end-to-end.

**What the real version should do:**
- **Input:** the table's `document_type`, `version_label`, jurisdiction,
  column names, and either all rows (small tables, <20 rows per
  `TABLE_SHARD_ROW_THRESHOLD`) or one row-group (sharded large tables like
  the Data Dictionary).
- **Call:** one Claude API call per table (or per row-group for sharded
  tables) — authored once at ingestion time, never at query time.
- **Output:** a short (1-3 sentence) natural-language description of what
  the table is about and what kind of question it would answer — e.g. "This
  table lists administrative penalty amounts (in SAR) for specific VAT
  compliance violations, including late registration, late filing, and
  incorrect invoicing." This description text is what actually gets
  embedded into Qdrant, not the raw table data — the embedding's only job
  is "recognize what this table is about" (semantic search), while the
  factual retrieval is a precise SQLite lookup by `table_ref` once matched.
- **Cost/rate awareness:** track a simple running call-counter, logged to
  console. No unbounded retry loops — on a single table's API failure, log
  it and move to the next table/document rather than blocking the whole
  ingestion run.

## 2. Cross-reference Claude review pass

**Where it plugs in:** after the regex-only pass
(`references.detect_article_references()`, which IS implemented and
catches explicit "Article N" / "المادة N" mentions), as an enhancement
layer over the same chunk's text.

**What the real version should do:**
- **Input:** a chunk's full text (already regex-scanned).
- **Call:** one Claude API call per chunk, asking it to identify any
  reference the regex pass would miss — indirect phrasing ("the Schedule",
  "the aforementioned article", spelled-out article names) and **table**
  references (regex alone can't reliably distinguish "per the penalty
  matrix" as a table citation vs. prose).
- **Output:** additional `{type: "article"|"table", ...}` entries merged
  into the same `references` list the regex pass produced — `type:table`
  entries need a `table_ref` (matched against `sqlite_store.list_tables()`
  for that document), `type:article` entries need an `article_number` (the
  citing chunk's own `document_type`/`version_label` is inherited at
  resolution time, per `docs/rag_pipeline_architecture.pdf` §7 — not stored
  on the reference itself).
- **Ordering constraint (already respected by the pipeline design):** this
  pass must run only after ALL documents' tables are registered
  (`sqlite_store.register_table()` calls complete across the whole corpus),
  so a reference to a table defined in a different document can resolve.

## Why this matters for retrieval quality today

Without these two steps, the pipeline still works, with two honest
degradations:
1. Table-pointer chunks are findable via semantic search, but the
   placeholder descriptions are less accurate/detailed than a
   Claude-authored one would be — a query phrased very differently from the
   table's raw column names may not surface it.
2. Cross-reference resolution only catches explicit "Article N" citations,
   not indirect ones, and never resolves table references from prose (a
   chunk saying "per the penalty schedule" won't automatically pull in the
   penalty table via cross-reference resolution — it would need to be
   found via the main retrieval search matching the table's own
   description instead).

Both are acceptable, clearly-labeled placeholders — not silent gaps.
