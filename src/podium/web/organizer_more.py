"""Integrations (webhooks, embed, API), data (import/export) and certificates for organizers."""

import json

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
        "base_url": settings.base_url,
        "errors": {},
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
    try:
        hook = webhooks_service.create_hook(
            db,
            ctx.event,
            ctx.user,
            str(form.get("url", "")),
            [str(v) for v in form.getlist("events")],
            str(form.get("secret", "")),
        )
    except PodiumError as exc:
        c = _integrations(ctx, db, settings)
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
    return RedirectResponse(f"/e/{ctx.event.slug}/organizer/integrations", status_code=303)


@router.post("/e/{slug}/organizer/webhooks/{hook_id}/delete", dependencies=[Depends(verify_csrf)])
def webhook_delete(
    hook_id: str, ctx: EventContext = Depends(require_organizer), db: DbSession = Depends(get_db)
):
    hook = webhooks_service.get_hook(db, ctx.event, hook_id)
    webhooks_service.delete_hook(db, ctx.event, ctx.user, hook)
    return RedirectResponse(f"/e/{ctx.event.slug}/organizer/integrations", status_code=303)


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
    file: UploadFile = File(...),
    mode: str = Form("dry_run"),
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
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
                error=f"This file describes event {data['event'].get('id')}, "
                f"not {ctx.event.public_id}. "
                "Import it from the events page to create a separate event.",
            ),
        )
    outcome = importexport.import_event(
        db, data, dry_run=(mode != "apply"), default_password=settings.demo_password
    )
    return render(
        request,
        "organizer/data.html",
        title="Import & export",
        **_console(ctx, "data", outcome=outcome, error=""),
    )


# --- certificates ---------------------------------------------------------------------------------


def _certs(ctx, db, **extra):
    defaults = {
        "certificates": cert_service.for_event(db, ctx.event),
        "report": None,
        "error": "",
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
    return RedirectResponse(f"/e/{ctx.event.slug}/organizer/certificates", status_code=303)


_ = CertificateKind
