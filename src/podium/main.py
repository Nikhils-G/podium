"""Podium application factory."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from podium import __version__
from podium.api.v1 import router as api_router
from podium.config import get_settings
from podium.errors import PodiumError, RateLimited, ValidationFailed
from podium.security.deps import current_user
from podium.security.headers import SecurityHeadersMiddleware
from podium.web import (
    account,
    admin,
    auth,
    certificates,
    community,
    judge,
    organizer,
    organizer_judging,
    organizer_more,
    organizer_voting,
    participant,
    public,
)
from podium.web.rendering import error_page, is_htmx

log = logging.getLogger("podium")
STATIC_DIR = Path(__file__).resolve().parent / "static"


def wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api/") or is_htmx(request)


def _field_errors(exc: RequestValidationError) -> dict[str, str]:
    """pydantic's location tuples as the same field → message map the services produce."""
    errors: dict[str, str] = {}
    for item in exc.errors():
        loc = [str(part) for part in item.get("loc", ()) if part not in ("body", "query", "path")]
        errors.setdefault(".".join(loc) or "body", item.get("msg", "Invalid value."))
    return errors


def json_error(request: Request, status: int, code: str, message: str, **extra) -> JSONResponse:
    headers = {"X-Podium-Message": message}
    if status == 429 and "retry_after" in extra:
        headers["Retry-After"] = str(extra["retry_after"])
    payload = {"error": {"code": code, "message": message, **extra}}
    return JSONResponse(payload, status_code=status, headers=headers)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.default_secret:
        if settings.base_url.lower().startswith("https://"):
            log.error(
                "Refusing to start: PODIUM_SECRET_KEY is the shipped default while "
                "PODIUM_BASE_URL is https. Set a real secret before exposing Podium."
            )
            raise RuntimeError("PODIUM_SECRET_KEY is the shipped default; set a real secret")
        log.warning(
            "PODIUM_SECRET_KEY is the shipped default: fine for a demo, never for a real event"
        )
    stop = asyncio.Event()
    task = None
    if settings.webhook_worker:
        from podium.tasks.webhook_worker import run

        task = asyncio.create_task(run(stop, settings.webhook_interval_seconds))
    yield
    stop.set()
    if task is not None:
        await task


# Operations anyone can call without a session or token; the schema says so explicitly.
PUBLIC_OPERATIONS = {
    ("get", "/api/v1/events"),
    ("get", "/api/v1/events/{slug}"),
    ("get", "/api/v1/events/{slug}/projects"),
    ("get", "/api/v1/events/{slug}/projects/{pid}"),
    ("get", "/api/v1/events/{slug}/results"),
    ("get", "/api/v1/events/{slug}/results/pairwise"),
    ("get", "/api/v1/events/{slug}/tally"),
    ("post", "/api/v1/events/{slug}/projects/{pid}/votes"),
    ("delete", "/api/v1/events/{slug}/projects/{pid}/votes"),
    ("get", "/api/v1/events/{slug}/votes/me"),
    ("post", "/api/v1/events/{slug}/voting/codes/redeem"),
    ("get", "/api/v1/events/{slug}/projects/{pid}/comments"),
    ("get", "/api/v1/events/{slug}/webhooks/types"),
    ("get", "/api/v1/certificates/{serial}"),
    ("get", "/api/v1/verify/{serial}"),
}

ERROR_RESPONSES = {
    "401": "Not signed in (no session cookie or bearer token).",
    "403": "Signed in, but this role can't do that here — or the token is read-only.",
    "404": "No such event, project or record visible to you.",
    "409": "The action conflicts with the event's state (e.g. results published, window closed).",
    "422": "Validation failed; error.errors maps field names to messages.",
    "429": "Rate limited; retry after a moment.",
}


def _document_api(app: FastAPI) -> None:
    """One shared error shape and both auth schemes on every operation, so the generated docs
    tell an integrator the truth without reading the handlers."""
    from fastapi.openapi.utils import get_openapi

    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
            tags=app.openapi_tags,
        )
        components = schema.setdefault("components", {})
        components.setdefault("schemas", {})["ErrorResponse"] = {
            "type": "object",
            "required": ["error"],
            "properties": {
                "error": {
                    "type": "object",
                    "required": ["code", "message"],
                    "properties": {
                        "code": {
                            "type": "string",
                            "enum": [
                                "unauthorized",
                                "forbidden",
                                "not_found",
                                "conflict",
                                "closed",
                                "payload_too_large",
                                "validation_failed",
                                "rate_limited",
                                "server_error",
                            ],
                        },
                        "message": {"type": "string"},
                        "errors": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                            "description": "Field → message, on 422 only.",
                        },
                    },
                }
            },
        }
        components["securitySchemes"] = {
            "sessionCookie": {"type": "apiKey", "in": "cookie", "name": "session"},
            "bearerToken": {
                "type": "http",
                "scheme": "bearer",
                "description": "Personal token from /account (`pdm_…`). Read-only tokens get "
                "403 on anything but GET.",
            },
        }
        schema["security"] = [{"sessionCookie": []}, {"bearerToken": []}]
        error_ref = {"$ref": "#/components/schemas/ErrorResponse"}
        for path, path_item in schema.get("paths", {}).items():
            for method, operation in path_item.items():
                if not isinstance(operation, dict) or "responses" not in operation:
                    continue
                public = (method, path) in PUBLIC_OPERATIONS
                if public:
                    operation["security"] = []
                for status, text in ERROR_RESPONSES.items():
                    if status in ("401", "403") and public:
                        continue
                    if status == "409" and method == "get":
                        continue
                    if status == "422" and not (
                        operation.get("requestBody") or operation.get("parameters")
                    ):
                        continue
                    operation["responses"][status] = {
                        "description": text,
                        "content": {"application/json": {"schema": error_ref}},
                    }
                if (method, path) == ("post", "/api/v1/events/import"):
                    operation["responses"]["413"] = {
                        "description": "The file is larger than 20 MB.",
                        "content": {"application/json": {"schema": error_ref}},
                    }
        for name in ("HTTPValidationError", "ValidationError"):  # FastAPI's own 422 shape
            if f'"#/components/schemas/{name}"' not in json.dumps(schema["paths"]):
                components["schemas"].pop(name, None)
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi


def create_app() -> FastAPI:
    app = FastAPI(
        title="Podium",
        lifespan=lifespan,
        version=__version__,
        description=(
            "Open-source, self-hostable hackathon submission and judging platform. "
            "Everything the web console can do, this API can do. Authenticate with a session "
            "cookie or a personal token (`Authorization: Bearer pdm_…`, created on /account). "
            "Judging data is isolated in the service layer: judges read only their own reviews."
        ),
        openapi_tags=[
            {"name": "events", "description": "Events, tracks, prizes and lifecycle actions."},
            {"name": "teams", "description": "Team formation by invite code."},
            {"name": "projects", "description": "Submissions, gallery data, drafts, withdrawal."},
            {
                "name": "judging",
                "description": "Judges, rubric, assignments, reviews, results, "
                "exports, audit. Role isolation enforced here.",
            },
            {"name": "community", "description": "Voting, voter codes, tallies, comments."},
            {
                "name": "integrations",
                "description": "Tokens, webhooks, certificates, import/export.",
            },
        ],
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(public.router)
    app.include_router(auth.router)
    app.include_router(participant.router)
    app.include_router(organizer.router)
    app.include_router(organizer_judging.router)
    app.include_router(judge.router)
    app.include_router(community.router)
    app.include_router(organizer_voting.router)
    app.include_router(organizer_more.router)
    app.include_router(account.router)
    app.include_router(admin.router)
    app.include_router(certificates.router)
    app.include_router(api_router)
    _document_api(app)

    @app.exception_handler(PodiumError)
    async def podium_error(request: Request, exc: PodiumError):
        extra = {}
        if isinstance(exc, ValidationFailed):
            extra["errors"] = exc.errors
        if isinstance(exc, RateLimited):
            extra["retry_after"] = exc.retry_after
        if wants_json(request):
            return json_error(request, exc.status_code, exc.code, exc.message, **extra)
        if exc.status_code == 401 and request.method == "GET":
            return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
        user = None
        try:
            from podium.db import get_sessionmaker

            with get_sessionmaker()() as db:
                user = current_user(request, db)
        except Exception:  # never let the error page itself fail
            user = None
        return error_page(request, exc.status_code, exc.message, user=user)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if wants_json(request):
            return json_error(
                request,
                422,
                "validation_failed",
                "Check the highlighted fields.",
                errors=_field_errors(exc),
            )
        return error_page(request, 422, "The form contained values we couldn't accept.")

    @app.exception_handler(404)
    async def not_found(request: Request, exc):
        if wants_json(request):
            return json_error(request, 404, "not_found", "Not found.")
        return error_page(request, 404, "We couldn't find that page.")

    @app.exception_handler(500)
    async def server_error(request: Request, exc):
        log.exception("unhandled error on %s", request.url.path)
        if wants_json(request):
            return json_error(request, 500, "server_error", "Something went wrong on our side.")
        return error_page(request, 500, "Something went wrong on our side. It has been logged.")

    return app


app = create_app()
