"""Double-submit cookie CSRF for HTML forms. JSON API calls are exempt (browsers cannot send
cross-site JSON without a CORS preflight, and this app never enables CORS on mutating routes)."""

import hmac
import secrets

from fastapi import Request

from podium.errors import Forbidden

COOKIE_NAME = "csrf"
FIELD_NAME = "csrf_token"
HEADER_NAME = "X-CSRF-Token"


def ensure_csrf_cookie(request: Request) -> str:
    """Return the request's CSRF token, minting one (stored on request.state) if absent."""
    token = request.cookies.get(COOKIE_NAME)
    if not token or len(token) < 32:
        token = getattr(request.state, "new_csrf_token", None) or secrets.token_urlsafe(32)
        request.state.new_csrf_token = token
    return token


async def verify_csrf(request: Request) -> None:
    expected = request.cookies.get(COOKIE_NAME, "")
    supplied = request.headers.get(HEADER_NAME, "")
    if not supplied:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            form = await request.form()
            supplied = str(form.get(FIELD_NAME, ""))
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise Forbidden("Your form expired. Reload the page and try again.")
