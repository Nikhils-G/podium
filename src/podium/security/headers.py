"""Security headers with a per-request CSP nonce. Nothing loads from outside the app itself."""

import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


def csp_for(request: Request, nonce: str) -> str:
    frame = "frame-ancestors *" if request.url.path.endswith("/embed") else "frame-ancestors 'none'"
    docs = request.url.path == "/api/docs/console"  # Swagger UI needs inline styles; nothing else
    return "; ".join(
        [
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}'",
            "style-src 'self' 'unsafe-inline'" if docs else "style-src 'self'",
            "img-src 'self' data:",
            "font-src 'self' data:" if docs else "font-src 'self'",
            "connect-src 'self'",
            "form-action 'self'",
            "base-uri 'none'",
            "object-src 'none'",
            frame,
        ]
    )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = csp_for(request, nonce)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if not request.url.path.endswith("/embed"):
            response.headers["X-Frame-Options"] = "DENY"
        return response
