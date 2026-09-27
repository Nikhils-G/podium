"""Integrations (webhooks, embed, API), data (import/export) and certificates for organizers."""

import hashlib
import json
import re
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session as DbSession

from podium.config import Settings, get_settings
from podium.db import get_db
from podium.errors import PodiumError
from podium.models import CertificateKind
from podium.security.csrf import verify_csrf
from podium.security.deps import EventContext, require_organizer
from podium.services import certificates as cert_service
from podium.services import importexport
from podium.services import webhooks as webhooks_service
from podium.web.organizer import _console
from podium.web.rendering import render

router = APIRouter(include_in_schema=False)


# --- integrations --------------------------------------------------------------------------------


def _integrations(ctx, db, settings, **extra):
    defaults = {
        "hooks": webhooks_service.list_hooks(db, ctx.event),
        "deliveries": webhooks_service.recent_deliveries(db, ctx.event),
        "event_types": webhooks_service.EVENT_TYPES,
        "event_descriptions": webhooks_service.EVENT_DESCRIPTIONS,
        "base_url": settings.base_url,
        "errors": {},
        "values": {"url": "", "events": None},
        "new_secret": None,
    }
    defaults.update(extra)
    return _console(ctx, "integrations", **defaults)


@router.get("/e/{slug}/organizer/integrations")
def integrations_page(
    request: Request,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    return render(
        request,
        "organizer/integrations.html",
        title="Integrations",
        **_integrations(ctx, db, settings),
    )


@router.post("/e/{slug}/organizer/webhooks", dependencies=[Depends(verify_csrf)])
async def webhook_create(
    request: Request,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    form = await request.form()
    url = str(form.get("url", ""))
    events = [str(v) for v in form.getlist("events")]
    values = {"url": url, "events": events}
    if not events:  # the service would subscribe to everything; a cleared form must not
        c = _integrations(ctx, db, settings, values=values)
        c["errors"] = {"events": "Choose at least one event type."}
        return render(
            request, "organizer/integrations.html", status_code=422, title="Integrations", **c
        )
    try:
        hook = webhooks_service.create_hook(
            db, ctx.event, ctx.user, url, events, str(form.get("secret", ""))
        )
    except PodiumError as exc:
        c = _integrations(ctx, db, settings, values=values)
        c["errors"] = getattr(exc, "errors", None) or {"url": exc.message}
        return render(
            request,
            "organizer/integrations.html",
            status_code=exc.status_code,
            title="Integrations",
            **c,
        )
    return render(
        request,
        "organizer/integrations.html",
        title="Integrations",
        **_integrations(ctx, db, settings, new_secret=hook.secret, new_hook=hook),
    )


@router.post("/e/{slug}/organizer/webhooks/{hook_id}/toggle", dependencies=[Depends(verify_csrf)])
def webhook_toggle(
    hook_id: str, ctx: EventContext = Depends(require_organizer), db: DbSession = Depends(get_db)
):
    hook = webhooks_service.get_hook(db, ctx.event, hook_id)
    webhooks_service.set_active(db, ctx.event, ctx.user, hook, not hook.active)
    saved = "resumed" if hook.active else "paused"
    return RedirectResponse(
        f"/e/{ctx.event.slug}/organizer/integrations?saved={saved}#webhooks", status_code=303
    )


@router.post("/e/{slug}/organizer/webhooks/{hook_id}/test", dependencies=[Depends(verify_csrf)])
def webhook_test(
    hook_id: str,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    """Queue a `ping` through the normal delivery path so signing and retries are exercised."""
    hook = webhooks_service.get_hook(db, ctx.event, hook_id)
    webhooks_service.emit(
        db,
        ctx.event,
        "ping",
        {"message": "Test delivery from Podium", "webhook": hook.public_id},
        only_hook=hook,
    )
    db.commit()
    return RedirectResponse(
        f"/e/{ctx.event.slug}/organizer/integrations?saved=test#deliveries", status_code=303
    )


@router.post("/e/{slug}/organizer/webhooks/{hook_id}/delete", dependencies=[Depends(verify_csrf)])
def webhook_delete(
    hook_id: str, ctx: EventContext = Depends(require_organizer), db: DbSession = Depends(get_db)
):
    hook = webhooks_service.get_hook(db, ctx.event, hook_id)
    webhooks_service.delete_hook(db, ctx.event, ctx.user, hook)
    return RedirectResponse(
        f"/e/{ctx.event.slug}/organizer/integrations?saved=deleted#webhooks", status_code=303
    )


@router.post(
    "/e/{slug}/organizer/webhooks/deliveries/{delivery_id}/redeliver",
    dependencies=[Depends(verify_csrf)],
)
def webhook_redeliver(
    delivery_id: int,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    webhooks_service.redeliver(db, ctx.event, ctx.user, delivery_id)
    return RedirectResponse(
        f"/e/{ctx.event.slug}/organizer/integrations#deliveries", status_code=303
    )


# --- data: export / import ------------------------------------------------------------------------


@router.get("/e/{slug}/organizer/data")
def data_page(request: Request, ctx: EventContext = Depends(require_organizer)):
    return render(
        request,
        "organizer/data.html",
        title="Import & export",
        **_console(ctx, "data", outcome=None, error=""),
    )


@router.post("/e/{slug}/organizer/data/import", dependencies=[Depends(verify_csrf)])
async def data_import(
    request: Request,
    file: UploadFile | None = File(None),
    mode: str = Form("dry_run"),
    token: str = Form(""),
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Dry run first, always: the uploaded file is kept for an hour so "Apply" imports exactly
    what was previewed, without a second upload."""
    saved_path = None
    if mode == "apply_saved":
        saved_path = _imports_dir(settings) / f"{token}.json" if IMPORT_TOKEN.match(token) else None
        if saved_path is None or not saved_path.exists() or _age(saved_path) > 3600:
            return render(
                request,
                "organizer/data.html",
                status_code=410,
                title="Import & export",
                **_console(
                    ctx,
                    "data",
                    outcome=None,
                    error="That dry run has expired (files are kept for an hour). Upload again.",
                ),
            )
        raw = saved_path.read_bytes()
    elif file is None:
        return render(
            request,
            "organizer/data.html",
            status_code=422,
            title="Import & export",
            **_console(ctx, "data", outcome=None, error="Choose a JSON file first."),
        )
    else:
        raw = await file.read()
    if len(raw) > 20 * 1024 * 1024:
        return render(
            request,
            "organizer/data.html",
            status_code=413,
            title="Import & export",
            **_console(ctx, "data", outcome=None, error="That file is larger than 20 MB."),
        )
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return render(
            request,
            "organizer/data.html",
            status_code=422,
            title="Import & export",
            **_console(ctx, "data", outcome=None, error=f"That isn't valid JSON: {exc}"),
        )
    if (
        isinstance(data, dict)
        and isinstance(data.get("event"), dict)
        and data["event"].get("id") != ctx.event.public_id
    ):
        return render(
            request,
            "organizer/data.html",
            status_code=422,
            title="Import & export",
            **_console(
                ctx,
                "data",
                outcome=None,
                error=f"This file describes event {data['event'].get('id')}, not "
                f"{ctx.event.public_id} ({ctx.event.name}). To create a separate event from "
                "it, send it to POST /api/v1/events/import "
                "(see /api/docs#op-post-api-v1-events-import).",
            ),
        )
    dry_run = mode == "dry_run"
    try:
        outcome = importexport.import_for_user(
            db,
            data,
            ctx.user,
            settings=settings,
            dry_run=dry_run,
            source_sha256=hashlib.sha256(raw).hexdigest(),
        )
    except PodiumError as exc:
        return render(
            request,
            "organizer/data.html",
            status_code=exc.status_code,
            title="Import & export",
            **_console(ctx, "data", outcome=None, error=exc.message),
        )
    import_token = None
    if dry_run:
        import_token = secrets.token_urlsafe(24)
        (_imports_dir(settings) / f"{import_token}.json").write_bytes(raw)
    elif saved_path is not None:
        saved_path.unlink(missing_ok=True)
    return render(
        request,
        "organizer/data.html",
        title="Import & export",
        **_console(ctx, "data", outcome=outcome, error="", import_token=import_token),
    )


IMPORT_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,48}$")


def _imports_dir(settings: Settings) -> Path:
    path = Path(settings.data_dir) / "imports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _age(path: Path) -> float:
    return time.time() - path.stat().st_mtime


# --- certificates ---------------------------------------------------------------------------------


def _certs(ctx, db, **extra):
    from podium.services import reviews as reviews_service

    defaults = {
        "certificates": cert_service.for_event(db, ctx.event),
        "report": None,
        "error": "",
        "judging_open": reviews_service.judging_is_open(ctx.event),
        "published": ctx.event.results_published_at is not None,
    }
    defaults.update(extra)
    return _console(ctx, "certificates", **defaults)


@router.get("/e/{slug}/organizer/certificates")
def certificates_page(
    request: Request,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    return render(request, "organizer/certificates.html", title="Certificates", **_certs(ctx, db))


@router.post("/e/{slug}/organizer/certificates/issue/{kind}", dependencies=[Depends(verify_csrf)])
def certificates_issue(
    request: Request,
    kind: str,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    try:
        if kind == "participation":
            report = cert_service.issue_participation(db, settings, ctx.event, ctx.user)
        elif kind == "judge":
            report = cert_service.issue_judge_records(db, settings, ctx.event, ctx.user)
        elif kind == "winner":
            report = cert_service.issue_winners(db, settings, ctx.event, ctx.user)
        else:
            raise PodiumError("Unknown certificate kind.")
    except PodiumError as exc:
        return render(
            request,
            "organizer/certificates.html",
            status_code=exc.status_code,
            title="Certificates",
            **_certs(ctx, db, error=exc.message),
        )
    return render(
        request,
        "organizer/certificates.html",
        title="Certificates",
        **_certs(ctx, db, report=(kind, report)),
    )


@router.post(
    "/e/{slug}/organizer/certificates/{serial}/revoke", dependencies=[Depends(verify_csrf)]
)
def certificate_revoke(
    serial: str, ctx: EventContext = Depends(require_organizer), db: DbSession = Depends(get_db)
):
    cert = cert_service.by_serial(db, serial)
    if cert.event_id == ctx.event.id:
        cert_service.revoke(db, ctx.event, ctx.user, cert)
    return RedirectResponse(
        f"/e/{ctx.event.slug}/organizer/certificates?saved=revoked", status_code=303
    )


_ = CertificateKind
