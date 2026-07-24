import os
import re
import uuid
from typing import Optional

import bcrypt
import psycopg2
from psycopg2.extras import RealDictCursor


EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

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


def initialize_memory_schemas():
    """Legacy activity-log and customer-memory tables. User profile data
    (name, preferences) now lives in LangGraph's PostgresStore instead of a
    hand-rolled table — see ChatbotAgent.store_user_profile/get_user_profile.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS chatbot")
            cur.execute("CREATE SCHEMA IF NOT EXISTS work")
            cur.execute("""
            CREATE TABLE IF NOT EXISTS chatbot.user_activity_log (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                activity_summary TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS work.customer_memories (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                customer_name TEXT NOT NULL,
                customer_context TEXT NOT NULL,
                issue_summary TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
            # ALTER TABLE ... ADD CONSTRAINT has no IF NOT EXISTS form, and
            # this function runs on every startup — a unique index does
            # support IF NOT EXISTS and is what ON CONFLICT in
            # upsert_customer_memory() needs to target.
            cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS customer_memories_user_customer_uniq
            ON work.customer_memories (user_id, customer_name)
            """)
            conn.commit()


def initialize_auth_schema():
    """Auth schema (auth.users) plus work.user_filing_notes, which has a
    foreign key to auth.users(id). Both must be created — and auth.users
    specifically must exist first — before any code path that could create
    work.user_filing_notes runs. Call this before ChatbotAgent() is
    instantiated (see app/main.py's startup ordering).
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
            conn.commit()


def create_user(email: str, password: str, nickname: Optional[str] = None) -> str:
    """Create an account. Raises ValueError on malformed email or a password
    outside bcrypt's 8-72 *byte* budget (not char length — see the module
    docstring note in docs/mizan_backend_auth_handoff_v3.pdf §5.1: an
    Arabic password runs out of that budget at roughly half the character
    count of an English one, so a char-length check would silently accept a
    password that then truncates and can never log in again).
    """
    email = email.strip().lower()
    if not EMAIL_REGEX.match(email):
        raise ValueError("Invalid email format")

    password_bytes = password.encode("utf-8")
    if not (8 <= len(password_bytes) <= 72):
        raise ValueError("Password must be between 8 and 72 bytes long")

    user_id = str(uuid.uuid4())
    password_hash = bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode("utf-8")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO auth.users (id, email, password_hash, nickname) VALUES (%s, %s, %s, %s)",
                (user_id, email, password_hash, nickname),
            )
            conn.commit()
    return user_id


def get_user_by_email(email: str) -> Optional[dict]:
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM auth.users WHERE email = %s", (email.strip().lower(),))
            row = cur.fetchone()
            return dict(row) if row else None


def get_user_by_id(user_id: str) -> Optional[dict]:
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM auth.users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def verify_password(plaintext: str, password_hash: str) -> bool:
    return bcrypt.checkpw(plaintext.encode("utf-8"), password_hash.encode("utf-8"))


def set_temp_password(email: str, temp_password: str, expires_in_minutes: int = 15) -> Optional[str]:
    """Issues a temp password for the account matching email, if one exists.
    Returns the user_id on success, None if no account matches — callers
    should still return a generic success response either way, so this
    can't be used to enumerate which emails are registered.
    """
    user = get_user_by_email(email)
    if not user:
        return None

    password_hash = bcrypt.hashpw(temp_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE auth.users
                SET password_hash = %s,
                    must_reset_password = true,
                    temp_password_expires_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 minute'),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (password_hash, expires_in_minutes, user["id"]),
            )
            conn.commit()
    return user["id"]


def reset_password(user_id: str, new_password: str) -> None:
    """Sets a real password and clears the must-reset gate. Raises
    ValueError on the same 8-72 byte budget as create_user()."""
    password_bytes = new_password.encode("utf-8")
    if not (8 <= len(password_bytes) <= 72):
        raise ValueError("Password must be between 8 and 72 bytes long")

    password_hash = bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode("utf-8")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE auth.users
                SET password_hash = %s,
                    must_reset_password = false,
                    temp_password_expires_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (password_hash, user_id),
            )
            conn.commit()


def append_user_activity(user_id: str, activity_summary: str):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chatbot.user_activity_log (user_id, activity_summary) VALUES (%s, %s)",
                (user_id, activity_summary),
            )
            conn.commit()


def get_user_recent_activity(user_id: str, limit: int = 5):
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT activity_summary, created_at FROM chatbot.user_activity_log WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
                (user_id, limit),
            )
            return cur.fetchall()


def store_customer_memory(user_id: str, customer_name: str, customer_context: str, issue_summary: Optional[str] = None):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO work.customer_memories (user_id, customer_name, customer_context, issue_summary, created_at, updated_at)
                VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (user_id, customer_name, customer_context, issue_summary),
            )
            conn.commit()


def get_customer_memories(user_id: str, customer_name: Optional[str] = None, limit: int = 10):
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if customer_name:
                cur.execute(
                    "SELECT * FROM work.customer_memories WHERE user_id = %s AND customer_name ILIKE %s ORDER BY created_at DESC LIMIT %s",
                    (user_id, f"%{customer_name}%", limit),
                )
            else:
                cur.execute(
                    "SELECT * FROM work.customer_memories WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
                    (user_id, limit),
                )
            return cur.fetchall()


def upsert_customer_memory(user_id: str, customer_name: str, customer_context: str, issue_summary: Optional[str] = None):
    """Real entry point for filing customer memories — store_customer_memory()
    above is kept in place, unused, because it's the pre-existing function
    and removing it is a separate cleanup decision. This one fixes the
    duplicate-row behavior: customer_context is appended (running history in
    one row), issue_summary is overwritten with the latest, updated_at is
    bumped. COALESCE guards the first-ever insert — concatenating NULL
    against new text returns NULL in Postgres, which would silently discard
    the very first entry otherwise.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO work.customer_memories (user_id, customer_name, customer_context, issue_summary, created_at, updated_at)
                VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id, customer_name) DO UPDATE SET
                    customer_context = COALESCE(work.customer_memories.customer_context, '') || E'\n' || EXCLUDED.customer_context,
                    issue_summary = EXCLUDED.issue_summary,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, customer_name, customer_context, issue_summary),
            )
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
