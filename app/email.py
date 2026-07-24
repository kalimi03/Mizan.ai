"""
Mizan.ai — transactional email via Mailgun's HTTP API.

Plain requests call, not the Mailgun SDK — keeps this dependency-free beyond
what the project already has, matching the "minimal dependencies" constraint
from docs/mizan_backend_auth_handoff_v3.pdf. Currently used for the
forgot-password temp-password email only.
"""

import os

import requests

# Fail loudly at import time, same reasoning as JWT_SECRET in app/auth.py:
# a missing mail config should break startup, not silently no-op on send.
MAILGUN_API_KEY = os.environ["MAILGUN_API_KEY"]
MAILGUN_DOMAIN = os.environ["MAILGUN_DOMAIN"]
MAILGUN_FROM_EMAIL = os.environ["MAILGUN_FROM_EMAIL"]

_MAILGUN_URL = f"https://api.mailgun.net/v3/{MAILGUN_DOMAIN}/messages"


def send_email(to: str, subject: str, text: str, timeout: int = 15) -> None:
    response = requests.post(
        _MAILGUN_URL,
        auth=("api", MAILGUN_API_KEY),
        data={"from": MAILGUN_FROM_EMAIL, "to": to, "subject": subject, "text": text},
        timeout=timeout,
    )
    response.raise_for_status()
