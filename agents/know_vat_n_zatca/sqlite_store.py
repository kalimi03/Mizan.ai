"""
Mizan.ai — "Know VAT & ZATCA" structured table store (SQLite).

Plain file, no Docker/server — both the offline ingestion pipeline and the
online gateway process read/write this same path via config.SQLITE_DB_PATH.
Two tables: tables_registry (one row per table) and table_rows (the actual
row data as JSON, FK to table_ref). See docs/rag_pipeline_architecture.pdf
§5-6 for the table-pointer pattern this backs.
"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import SQLITE_DB_PATH


@contextmanager
def get_connection():
    Path(SQLITE_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def initialize_schema() -> None:
    with get_connection() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS tables_registry (
            table_ref TEXT PRIMARY KEY,
            document_type TEXT NOT NULL,
            version_label TEXT NOT NULL,
            jurisdiction TEXT NOT NULL DEFAULT 'KSA',
            language TEXT NOT NULL,
            is_current INTEGER NOT NULL DEFAULT 1,
            effective_start_date TEXT,
            effective_end_date TEXT,
            description TEXT,
            corrected INTEGER NOT NULL DEFAULT 0,
            corrected_date TEXT,
            source_site TEXT,
            source_url TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS table_rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_ref TEXT NOT NULL REFERENCES tables_registry(table_ref),
            row_index INTEGER NOT NULL,
            row_data TEXT NOT NULL
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_table_rows_table_ref ON table_rows(table_ref)")
        conn.commit()


def register_table(
    table_ref: str,
    document_type: str,
    version_label: str,
    language: str,
    is_current: bool = True,
    jurisdiction: str = "KSA",
    effective_start_date: Optional[str] = None,
    effective_end_date: Optional[str] = None,
    source_site: Optional[str] = None,
    source_url: Optional[str] = None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO tables_registry
                (table_ref, document_type, version_label, jurisdiction, language,
                 is_current, effective_start_date, effective_end_date, source_site, source_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(table_ref) DO UPDATE SET
                document_type=excluded.document_type,
                version_label=excluded.version_label,
                jurisdiction=excluded.jurisdiction,
                language=excluded.language,
                is_current=excluded.is_current,
                effective_start_date=excluded.effective_start_date,
                effective_end_date=excluded.effective_end_date,
                source_site=excluded.source_site,
                source_url=excluded.source_url
            """,
            (table_ref, document_type, version_label, jurisdiction, language,
             int(is_current), effective_start_date, effective_end_date, source_site, source_url),
        )
        conn.commit()


def set_table_description(table_ref: str, description: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE tables_registry SET description = ? WHERE table_ref = ?",
            (description, table_ref),
        )
        conn.commit()


def mark_table_corrected(table_ref: str, corrected_date: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE tables_registry SET corrected = 1, corrected_date = ? WHERE table_ref = ?",
            (corrected_date, table_ref),
        )
        conn.commit()


def insert_table_rows(table_ref: str, rows: List[Dict[str, Any]]) -> None:
    """Replaces (not appends) — re-ingesting a document is a normal
    operation (retries, corrected manifests, a re-run after a source PDF
    changes), and register_table() is already an upsert. Without the
    delete-first here, every re-ingest of an unchanged table_ref piled
    another full copy of its rows on top of the previous ones (found the
    hard way: three re-ingests of the same 8 tables left 3x their real row
    count sitting in this table before this fix)."""
    with get_connection() as conn:
        conn.execute("DELETE FROM table_rows WHERE table_ref = ?", (table_ref,))
        conn.executemany(
            "INSERT INTO table_rows (table_ref, row_index, row_data) VALUES (?, ?, ?)",
            [(table_ref, i, json.dumps(row, ensure_ascii=False)) for i, row in enumerate(rows)],
        )
        conn.commit()


def get_table_rows(table_ref: str) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT row_data FROM table_rows WHERE table_ref = ? ORDER BY row_index",
            (table_ref,),
        )
        return [json.loads(row["row_data"]) for row in cur.fetchall()]


def get_table_registry_entry(table_ref: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        cur = conn.execute("SELECT * FROM tables_registry WHERE table_ref = ?", (table_ref,))
        row = cur.fetchone()
        return dict(row) if row else None


def list_tables(document_type: Optional[str] = None) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        if document_type:
            cur = conn.execute("SELECT * FROM tables_registry WHERE document_type = ?", (document_type,))
        else:
            cur = conn.execute("SELECT * FROM tables_registry")
        return [dict(row) for row in cur.fetchall()]
