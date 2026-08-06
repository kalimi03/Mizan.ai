"""
Mizan.ai — VAT Compliance Center batch store. Same shape as
features/calculator/filing_store.py's TTL/ownership pattern (24h TTL
matching a JWT's own expiry, Postgres rather than in-memory since this
service is meant to eventually run as multiple Kubernetes replicas), but
generalized to a "batch" — one run over N items (invoices, or later
period-return transactions), some of which may need attention — rather
than a single filing. One table serves both jobs of the VAT Compliance
Center (`kind` distinguishes them) since the underlying shape is the same
idea both times: a batch with N sub-results, some flagged.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from psycopg2.extras import Json, RealDictCursor

from features.common.db import get_connection

BATCH_TTL_HOURS = 24


def initialize_compliance_batches_schema():
    """work.compliance_batches has a foreign key to auth.users(id) — must
    run after features.common.db.initialize_auth_schema(). Idempotent (IF
    NOT EXISTS), safe to call on every startup."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS work")
            cur.execute("""
            CREATE TABLE IF NOT EXISTS work.compliance_batches (
                batch_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES auth.users(id),
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                data JSONB NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL
            )
            """)
            conn.commit()


def store_batch(user_id: str, kind: str, status: str, data: dict, ttl_hours: int = BATCH_TTL_HOURS) -> str:
    """Mints a fresh batch_id (same role filing_id/session_id play
    elsewhere in this repo) and stores the batch. Returns the batch_id."""
    batch_id = str(uuid.uuid4())
    expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO work.compliance_batches (batch_id, user_id, kind, status, data, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (batch_id, user_id, kind, status, Json(data), expires_at),
            )
            conn.commit()
    return batch_id


def get_batch(batch_id: str, user_id: str) -> Optional[dict]:
    """None if batch_id doesn't exist, doesn't belong to user_id, or has
    expired — all indistinguishable to the caller on purpose (never reveal
    whether a batch_id exists to someone who doesn't own it). Returns
    {"kind", "status", "data"} on success."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT kind, status, data FROM work.compliance_batches
                WHERE batch_id = %s AND user_id = %s AND expires_at > CURRENT_TIMESTAMP
                """,
                (batch_id, user_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def update_batch(batch_id: str, user_id: str, status: str, data: dict) -> None:
    """Ownership enforced in the WHERE clause, same as every getter here."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE work.compliance_batches SET status = %s, data = %s
                WHERE batch_id = %s AND user_id = %s
                """,
                (status, Json(data), batch_id, user_id),
            )
            conn.commit()
