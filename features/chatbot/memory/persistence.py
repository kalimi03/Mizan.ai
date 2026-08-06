import re
import uuid
from typing import Optional

import bcrypt
from psycopg2.extras import RealDictCursor

from features.common.db import (
    DB_HOST,
    DB_NAME,
    DB_PASSWORD,
    DB_PORT,
    DB_USER,
    get_connection,
    get_filing_notes_for_customer,
    get_last_filing_note,
    initialize_auth_schema,
)

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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


# initialize_auth_schema() now lives in features/common/db.py (imported above,
# re-exported here) — it's genuinely shared infrastructure, not chatbot
# business logic, since Calculator+MCP also needs to call it at its own
# startup for work.user_filing_notes.


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

# get_last_filing_note() / get_filing_notes_for_customer() now live in
# features/common/db.py (imported above, re-exported here) — Calculator+MCP
# needs them too, for its filing-notes-qa endpoint.
