// Mizan.ai frontend — shared auth/request helpers, used by every page.
//
// Auth here is a JWT Bearer token (not a cookie), minted by
// /api/auth/login or /api/auth/register — see app/auth.py. It's stored in
// localStorage and attached to every protected request by mzFetch() below.
// There is no logout endpoint (stateless JWT, see app/auth.py's own
// docstring) — logging out is just discarding the token client-side.

const MZ_TOKEN_KEY = "mizan_jwt";

function mzSaveToken(token) {
  localStorage.setItem(MZ_TOKEN_KEY, token);
}

function mzGetToken() {
  return localStorage.getItem(MZ_TOKEN_KEY);
}

function mzClearToken() {
  localStorage.removeItem(MZ_TOKEN_KEY);
}

function mzLogout() {
  mzClearToken();
  window.location.href = "login.html";
}

// Call at the top of every page that requires a logged-in user (everything
// except login/register/forgot-password/reset-password). Redirects to
// login immediately if there's no token — doesn't validate it locally
// (expiry/signature), since the backend already does that on every call;
// this is just "did the user ever log in on this browser."
function mzRequireAuth() {
  if (!mzGetToken()) {
    window.location.href = "login.html";
  }
}

// Wraps fetch() to attach the Authorization header automatically, and to
// send the user back to login on a 401 (expired/invalid token) rather than
// making every page handle that case itself.
async function mzFetch(url, options = {}) {
  const token = mzGetToken();
  const headers = new Headers(options.headers || {});
  if (token) {
    headers.set("Authorization", "Bearer " + token);
  }
  const response = await fetch(url, { ...options, headers });
  if (response.status === 401) {
    mzClearToken();
    window.location.href = "login.html";
    throw new Error("Not authenticated");
  }
  return response;
}

// Most endpoints in this repo return {"detail": "..."} on error (FastAPI's
// default HTTPException shape) — this pulls that out consistently instead
// of every page re-implementing the same try/catch.
async function mzErrorDetail(response) {
  try {
    const body = await response.json();
    return body.detail || response.statusText;
  } catch {
    return response.statusText;
  }
}
