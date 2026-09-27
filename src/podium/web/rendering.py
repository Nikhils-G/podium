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
from podium.services.audit import describe
from podium.services.text import plural

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
jinja_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)
templates = Jinja2Templates(env=jinja_env)
templates.env.globals["version"] = __version__
templates.env.globals["now_utc"] = utcnow
jinja_env.filters["plural"] = plural
jinja_env.filters["hue"] = lambda value: sum(ord(c) for c in str(value)) % 8 + 1
jinja_env.globals["describe"] = describe


def wants_partial(request: Request, target: str | None = None) -> bool:
    """True for an htmx request aimed at a region of the page — never for a boosted navigation,
    which needs the full page (a boosted rail link once received the bare polling partial)."""
    if not is_htmx(request) or request.headers.get("HX-Boosted") == "true":
        return False
    return target is None or request.headers.get("HX-Target") == target


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def render(request: Request, name: str, status_code: int = 200, **context) -> HTMLResponse:
    csrf_token = ensure_csrf_cookie(request)
    context.setdefault("user", None)
    context.setdefault("title", None)
    context.setdefault("event", None)
    context.setdefault("nav", None)
    context["csrf_token"] = csrf_token
    if not context.get("meta_description"):
        event_obj = context.get("event")
        text = (getattr(event_obj, "description", "") or "").strip().replace("\n", " ")
        context["meta_description"] = (
            text[:157] + "…" if len(text) > 160 else text
        ) or "Open-source, self-hostable hackathon submissions and judging."
    event_obj = context.get("event")
    if event_obj is not None and not context.get("stage_label"):
        from podium.services.events import STAGE_LABELS, stage_of

        stage = stage_of(event_obj)
        context.setdefault("stage", stage.value)
        context["stage_label"] = STAGE_LABELS[stage]
    context["nonce"] = getattr(request.state, "csp_nonce", "")
    settings = get_settings()
    context["settings"] = settings
    context["demo_mode"] = settings.demo_mode
    context["request_id"] = getattr(request.state, "csp_nonce", "")[:8]
    context["navctx"] = _header_context(request, context.get("user"), context.get("event"))
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


def _header_context(request: Request, user, event) -> dict:
    """Memberships + role links for the header; computed once per request."""
    cached = getattr(request.state, "navctx", None)
    if cached is not None and cached.get("_for") == (id(user), id(event)):
        return cached
    if user is None:
        ctx = {"memberships": [], "links": None}
    else:
        from podium.db import get_sessionmaker
        from podium.services import navigation

        with get_sessionmaker()() as db:
            ctx = navigation.header_context(db, user, event)
    ctx["_for"] = (id(user), id(event))
    request.state.navctx = ctx
    return ctx


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
