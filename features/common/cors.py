"""
Mizan.ai — shared CORS setup, used by every service's FastAPI app.

Browsers block cross-origin requests by default (the frontend will run on
its own origin, e.g. http://localhost:5500 in dev, different from each
service's own port) unless the server explicitly allows it. Configurable
via MIZAN_CORS_ORIGINS (comma-separated) so this can be tightened for
production without a code change — same pattern as every other env-var-
driven setting in this repo. Defaults to common local dev server ports so
this works out of the box before that env var is ever set.

allow_credentials=False is deliberate, not an oversight: auth here is a
Bearer token in the Authorization header, not a cookie, so CORS's
"credentials" mode (which governs cookies/browser-managed auth) doesn't
apply — turning it on would only additionally require allow_origins to be
an explicit list (no "*"), for no actual benefit here.
"""

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

DEFAULT_DEV_ORIGINS = [
    "http://localhost:5500",   # VS Code "Live Server" extension default
    "http://127.0.0.1:5500",
    "http://localhost:8090",   # `python -m http.server 8090` in frontend/
    "http://127.0.0.1:8090",
    "http://localhost:3000",   # kept in case a bundler-based dev server is ever used
    "http://127.0.0.1:3000",
    "http://localhost:5173",   # Vite default
    "http://127.0.0.1:5173",
]


def configure_cors(app: FastAPI) -> None:
    origins_env = os.getenv("MIZAN_CORS_ORIGINS")
    origins = [o.strip() for o in origins_env.split(",") if o.strip()] if origins_env else DEFAULT_DEV_ORIGINS

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
