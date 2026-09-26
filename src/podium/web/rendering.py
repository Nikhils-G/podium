"""Template rendering with the per-request context every page needs (user, CSRF, CSP nonce)."""

from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape

from podium import __version__
from podium.config import get_settings
from podium.models import utcnow
from podium.security.csrf import COOKIE_NAME as CSRF_COOKIE
from podium.security.csrf import ensure_csrf_cookie

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
jinja_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)
templates = Jinja2Templates(env=jinja_env)
templates.env.globals["version"] = __version__
templates.env.globals["now_utc"] = utcnow


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def render(request: Request, name: str, status_code: int = 200, **context) -> HTMLResponse:
    csrf_token = ensure_csrf_cookie(request)
    context.setdefault("user", None)
    context.setdefault("title", None)
    context.setdefault("event", None)
    context.setdefault("nav", None)
    context["csrf_token"] = csrf_token
    context["nonce"] = getattr(request.state, "csp_nonce", "")
    context["settings"] = get_settings()
    response = templates.TemplateResponse(request, name, context, status_code=status_code)
    if getattr(request.state, "new_csrf_token", None):
        response.set_cookie(
            CSRF_COOKIE,
            csrf_token,
            httponly=False,
            samesite="lax",
            secure=get_settings().secure_cookies,
            max_age=60 * 60 * 24 * 30,
            path="/",
        )
    return response


HEADINGS = {
    400: "That request didn't make sense",
    401: "Sign in to continue",
    403: "You can't access this",
    404: "Page not found",
    409: "That conflicts with something",
    422: "Check the details",
    429: "Slow down a moment",
    500: "Something broke on our side",
}


def error_page(request: Request, status: int, message: str, user=None) -> HTMLResponse:
    return render(
        request,
        "errors/error.html",
        status_code=status,
        status=status,
        heading=HEADINGS.get(status, "Something went wrong"),
        message=message,
        title=HEADINGS.get(status, "Error"),
        user=user,
    )
