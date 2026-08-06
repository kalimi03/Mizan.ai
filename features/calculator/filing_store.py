"""
Mizan.ai — Feature E filing-extraction store. Closes the gap between
POST /api/compliance/upload (stores the raw uploaded files),
POST /api/compliance/{filing_id}/extract (extracts real data from them —
a separate, explicit step so the user chooses when extraction actually
runs), and GET /api/compliance/{filing_id}/extraction (fetches the result
back) — these used to be two unconnected mock endpoints; now they're a
real store keyed by a freshly-minted filing_id (the same role session_id
plays for /api/chat — see app/main.py's docstring there) plus user_id for
ownership.

A single row moves through two states: right after upload, `files` is
populated and `extractions` is NULL; after /extract runs, `extractions`
gets filled in (files is left in place — cheap, and lets /extract be
re-run if needed without asking the user to re-upload).

TTL-based expiry, not "wipe on logout" — this repo's JWTs are stateless
with no server-side session/logout concept (see app/auth.py), so there is
no real "session end" event to key cleanup off of. 24h TTL matches a JWT's
own expiry, so a filing lives about as long as the login session that
created it. Expiry is enforced directly in each getter's WHERE clause — an
expired filing_id simply isn't found, no separate cleanup job required for
correctness.

Postgres, not in-memory: this service is meant to eventually run as
multiple Kubernetes replicas, where an in-memory dict wouldn't survive a
pod restart or be visible across replicas (see project memory / the
deployment-target discussion for the full reasoning) — that's also why the
raw uploaded files themselves are stored here rather than on local disk
between the upload and extract calls (a later replica handling /extract
might not be the same one that handled /upload). Reuses
features/common/db.py's get_connection() — same shared Postgres already used
for auth and filing-notes, no new infra dependency.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from psycopg2.extras import Json, RealDictCursor

from features.common.db import get_connection

FILING_EXTRACTION_TTL_HOURS = 24


def initialize_filing_extractions_schema():
    """work.filing_extractions has a foreign key to auth.users(id) — must
    run after features.common.db.initialize_auth_schema() (services/calculator/
    main.py already calls that first, at its own startup, before this).
    Idempotent (IF NOT EXISTS / DROP NOT NULL throughout), safe to call on
    every startup, including against a table created before the
    upload/extract split (ADD COLUMN IF NOT EXISTS + DROP NOT NULL bring an
    older table up to the current shape without losing data)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS work")
            cur.execute("""
            CREATE TABLE IF NOT EXISTS work.filing_extractions (
                filing_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES auth.users(id),
                files JSONB,
                extractions JSONB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL
            )
            """)
            cur.execute("ALTER TABLE work.filing_extractions ADD COLUMN IF NOT EXISTS files JSONB")
            cur.execute("ALTER TABLE work.filing_extractions ALTER COLUMN extractions DROP NOT NULL")
            conn.commit()


def store_filing_upload(filing_id: str, user_id: str, files: List[dict], ttl_hours: int = FILING_EXTRACTION_TTL_HOURS) -> None:
    """files: [{"filename": ..., "content_base64": ...}, ...] — the raw
    uploaded bytes, kept only long enough for /extract to read them back.
    extractions starts NULL; store_filing_extraction() fills it in later."""
    expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO work.filing_extractions (filing_id, user_id, files, expires_at)
                VALUES (%s, %s, %s, %s)
                """,
                (filing_id, user_id, Json(files), expires_at),
            )
            conn.commit()


def get_filing_files(filing_id: str, user_id: str) -> Optional[List[dict]]:
    """None if filing_id doesn't exist, doesn't belong to user_id, or has
    expired — same indistinguishable-404 reasoning as get_filing_extraction()
    below."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT files FROM work.filing_extractions
                WHERE filing_id = %s AND user_id = %s AND expires_at > CURRENT_TIMESTAMP
                """,
                (filing_id, user_id),
            )
            row = cur.fetchone()
            return row["files"] if row else None


def store_filing_extraction(filing_id: str, user_id: str, extractions: List[dict]) -> None:
    """extractions: one route_and_extract()-shaped dict per uploaded file,
    each tagged with its own "filename" key by the caller
    (services/calculator/main.py). Updates the row store_filing_upload()
    already created — ownership is enforced in the WHERE clause, same as
    every getter here."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE work.filing_extractions SET extractions = %s
                WHERE filing_id = %s AND user_id = %s
                """,
                (Json(extractions), filing_id, user_id),
            )
            conn.commit()


def get_filing_extraction(filing_id: str, user_id: str) -> Optional[List[dict]]:
    """None if filing_id doesn't exist, doesn't belong to user_id, has
    expired, or simply hasn't been through /extract yet (extractions still
    NULL) — all indistinguishable to the caller on purpose (never reveal
    whether a filing_id exists to someone who doesn't own it)."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT extractions FROM work.filing_extractions
                WHERE filing_id = %s AND user_id = %s AND expires_at > CURRENT_TIMESTAMP
                """,
                (filing_id, user_id),
            )
            row = cur.fetchone()
            return row["extractions"] if row else None
