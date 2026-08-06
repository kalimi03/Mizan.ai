"""
Seed a handful of fake work.user_filing_notes rows so the Feature E Q&A
query path (features/chatbot/filing_notes_qa.py) is verifiable via Swagger
before Feature E's real extraction pipeline exists.

Usage: python scripts/seed_filing_notes.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from features.chatbot.memory.persistence import get_connection, get_user_by_email, initialize_auth_schema

SEED_EMAIL = "seed_filing_test@mizan.ai"
SEED_PASSWORD = "SeedFilingTest123"
SEED_NICKNAME = "Filing Notes Seed User"

SEED_ROWS = [
    ("Ahmed Al-Faisal", "completed", "ZATCA e-invoicing setup verified, all fields validated."),
    ("Acme Trading Co.", "pending", "Awaiting VAT registration certificate upload."),
    ("Sara Al-Otaibi", "issues_found", "Mismatch between invoice total and declared VAT amount — flagged for review."),
    ("Acme Trading Co.", "completed", "Compliance report generated and delivered."),
]


def _get_or_create_seed_user() -> str:
    from features.chatbot.memory.persistence import create_user

    existing = get_user_by_email(SEED_EMAIL)
    if existing:
        return existing["id"]
    return create_user(SEED_EMAIL, SEED_PASSWORD, SEED_NICKNAME)


def main() -> None:
    initialize_auth_schema()
    user_id = _get_or_create_seed_user()

    with get_connection() as conn:
        with conn.cursor() as cur:
            for customer_name, status, notes in SEED_ROWS:
                cur.execute(
                    """
                    INSERT INTO work.user_filing_notes (user_id, customer_name, status, notes)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (user_id, customer_name, status, notes),
                )
            conn.commit()

    print(f"Seeded {len(SEED_ROWS)} filing notes for user_id={user_id} ({SEED_EMAIL}).")
    print(f"Log in as {SEED_EMAIL} / {SEED_PASSWORD} to query them via /api/filing-notes/qa.")


if __name__ == "__main__":
    main()
