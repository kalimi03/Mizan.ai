"""
Mizan.ai — JWT auth utilities.

Stateless JWT: minted only inside /api/auth/register and /api/auth/login,
verified on every protected request (signature + expiry, every time — even
right after login). No server-side token store, no revocation list, no
refresh tokens in v1 — see docs/mizan_backend_auth_handoff_v3.pdf §5.2.
Logout is the frontend discarding the token; nothing to do here for that.

Tokens minted for an account with a pending password reset carry a
must_reset_password claim. get_current_user_id_full_access() rejects those
for regular protected routes — only /api/auth/reset-password (which uses
plain get_current_user_id) accepts them, so a temp password can't be used
for anything except setting a real one.
"""

import os
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# Fail loudly at import time if unset — never default to a placeholder
# secret. A missing JWT_SECRET should break startup, not silently sign
# tokens with a guessable value.
JWT_SECRET = os.environ["JWT_SECRET"]
JWT_ALGORITHM = "HS256"

# auto_error=False so a missing Authorization header falls through to
# get_current_user_id() and gets the same 401 as an invalid/expired one,
# rather than HTTPBearer's default 403.
_bearer_scheme = HTTPBearer(auto_error=False)

_TEMP_PASSWORD_ALPHABET = string.ascii_letters + string.digits


def generate_temp_password(length: int = 12) -> str:
    return "".join(secrets.choice(_TEMP_PASSWORD_ALPHABET) for _ in range(length))


def create_jwt(user_id: str, email: str, must_reset_password: bool = False, expires_in_hours: int = 24) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "email": email,
        "must_reset_password": must_reset_password,
        "iat": now,
        "exp": now + timedelta(hours=expires_in_hours),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise ValueError("Token expired")
    except jwt.InvalidTokenError:
        raise ValueError("Invalid token")


def verify_jwt(token: str) -> str:
    """Validates signature AND expiry. Returns the user_id (sub claim);
    raises ValueError on anything invalid or expired."""
    return _decode_token(token)["sub"]


def get_current_user_id(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> str:
    """FastAPI dependency — apply to any protected route. Injects user_id.
    Accepts a must-reset token, so this is what /api/auth/reset-password
    uses; routes that should be off-limits until the reset is done should
    use get_current_user_id_full_access instead.
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    try:
        return verify_jwt(credentials.credentials)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def get_current_user_id_full_access(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> str:
    """Same as get_current_user_id, but additionally rejects a token minted
    for an account with a pending password reset — apply to any business
    endpoint that shouldn't be usable until the user sets a real password."""
    if credentials is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    try:
        payload = _decode_token(credentials.credentials)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if payload.get("must_reset_password"):
        raise HTTPException(status_code=403, detail="Password reset required before continuing")
    return payload["sub"]
