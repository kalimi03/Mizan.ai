"""Shared Postgres infrastructure — connection config, schema init, and the
few read functions used by more than one service (Chatbot+common owns auth
CRUD and calls this at startup; Calculator+MCP reads work.user_filing_notes
for its filing-notes-qa endpoint and also calls initialize_auth_schema() at
its own startup). Everything else touching Postgres for chatbot-only
concerns (activity log, customer memories) stays in
features/chatbot/memory/persistence.py.

store_filing_note() is the write side of work.user_filing_notes — added
2026-07-27 to close a real gap: nothing in the real calculation flow ever
wrote to this table before (only scripts/seed_filing_notes.py, for test
data), even though the read side (get_last_filing_note/
get_filing_notes_for_customer, used by filing_notes_qa.py) has always
worked. Calculator's /api/compliance/{filing_id}/validate now calls this
when approved=True — the one moment in that flow explicitly designed to
mean "this is final."
"""

import os
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "mizan_db")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres123")


def get_connection():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )


def initialize_auth_schema():
    """Auth schema (auth.users) plus work.user_filing_notes, which has a
    foreign key to auth.users(id). Both must be created — and auth.users
    specifically must exist first — before any code path that could create
    work.user_filing_notes runs. Every statement here is idempotent
    (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS), so this is safe to call
    independently, at its own startup, from every service that needs these
    tables (Chatbot+common and Calculator+MCP today) — no cross-service
    startup ordering is required.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS auth")
            cur.execute("CREATE SCHEMA IF NOT EXISTS work")
            cur.execute("""
            CREATE TABLE IF NOT EXISTS auth.users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                nickname TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
            # Password-reset tracking. ADD COLUMN IF NOT EXISTS so this is
            # safe to run against a table that already existed before these
            # columns were introduced, not just on a fresh CREATE TABLE.
            cur.execute("ALTER TABLE auth.users ADD COLUMN IF NOT EXISTS must_reset_password BOOLEAN NOT NULL DEFAULT false")
            cur.execute("ALTER TABLE auth.users ADD COLUMN IF NOT EXISTS temp_password_expires_at TIMESTAMP")
            cur.execute("""
            CREATE TABLE IF NOT EXISTS work.user_filing_notes (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES auth.users(id),
                customer_name TEXT,
                status TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
            # Free-text feedback from the frontend's "Contact us" page —
            # deliberately no email delivery involved, just a row someone
            # checks manually (e.g. via psql) later.
            cur.execute("""
            CREATE TABLE IF NOT EXISTS work.user_feedback (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES auth.users(id),
                message TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
            conn.commit()


def get_last_filing_note(user_id: str) -> Optional[dict]:
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM work.user_filing_notes WHERE user_id = %s ORDER BY created_at DESC LIMIT 1",
                (user_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def get_filing_notes_for_customer(user_id: str, customer_name: str, limit: int = 10):
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM work.user_filing_notes
                WHERE user_id = %s AND customer_name ILIKE %s
                ORDER BY created_at DESC LIMIT %s
                """,
                (user_id, f"%{customer_name}%", limit),
            )
            return cur.fetchall()


def store_filing_note(user_id: str, customer_name: Optional[str], status: str, notes: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO work.user_filing_notes (user_id, customer_name, status, notes) VALUES (%s, %s, %s, %s)",
                (user_id, customer_name, status, notes),
            )
            conn.commit()


def store_feedback(user_id: str, message: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO work.user_feedback (user_id, message) VALUES (%s, %s)",
                (user_id, message),
            )
            conn.commit()
