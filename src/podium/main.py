"""Podium application factory."""

import asyncio
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


def json_error(request: Request, status: int, code: str, message: str, **extra) -> JSONResponse:
    headers = {"X-Podium-Message": message}
    if status == 429 and "retry_after" in extra:
        headers["Retry-After"] = str(extra["retry_after"])
    payload = {"error": {"code": code, "message": message, **extra}}
    return JSONResponse(payload, status_code=status, headers=headers)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    stop = asyncio.Event()
    task = None
    if settings.webhook_worker:
        from podium.tasks.webhook_worker import run

        task = asyncio.create_task(run(stop, settings.webhook_interval_seconds))
    yield
    stop.set()
    if task is not None:
        await task


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
    app.include_router(certificates.router)
    app.include_router(api_router)

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
                errors=[{"loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()],
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
