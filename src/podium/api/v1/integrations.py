"""Account & tokens, webhooks, certificates, import/export — the T4 API surface."""

import hashlib
import json

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session as DbSession

from podium.config import Settings, get_settings
from podium.db import get_db
from podium.errors import NotFound, PayloadTooLarge, ValidationFailed
from podium.models import Role, User
from podium.schemas.integrations import (
    TokenCreate,
    WebhookCreate,
    WebhookUpdate,
    certificate_out,
    delivery_out,
    webhook_out,
)
from podium.schemas.responses import MeOut, TokensOut, VerifyOut
from podium.security.deps import (
    EventContext,
    require_event_role,
    require_organizer,
    require_user,
)
from podium.services import certificates as cert_service
from podium.services import importexport
from podium.services import tokens as tokens_service
from podium.services import webhooks as webhooks_service

router = APIRouter(tags=["integrations"])


# --- me / tokens ----------------------------------------------------------------------------------


@router.get("/me", response_model=MeOut)
def me(user: User = Depends(require_user)):
    """The signed-in account."""
    return {
        "user": {
            "id": user.public_id,
            "name": user.name,
            "email": user.email,
            "is_admin": user.is_admin,
        }
    }


@router.get("/me/tokens", response_model=TokensOut)
def list_tokens(user: User = Depends(require_user), db: DbSession = Depends(get_db)):
    """The caller's personal API tokens: prefix, scope and expiry, never the secret."""
    return {
        "tokens": [
            {
                "id": t.id,
                "name": t.name,
                "prefix": t.prefix,
                "scope": t.scope,
                "expires_at": t.expires_at,
                "created_at": t.created_at,
                "last_used_at": t.last_used_at,
            }
            for t in tokens_service.list_tokens(db, user)
        ]
    }


@router.post("/me/tokens", status_code=201)
def create_token(
    body: TokenCreate, user: User = Depends(require_user), db: DbSession = Depends(get_db)
):
    """Create a personal API token.
    The raw token is returned once; send it as `Authorization: Bearer`."""
    token, raw = tokens_service.create_token(
        db, user, body.name, scope=body.scope, expires_in_days=body.expires_in_days
    )
    return {"token": {"id": token.id, "name": token.name, "prefix": token.prefix, "secret": raw}}


@router.delete("/me/tokens/{token_id}", status_code=204)
def revoke_token(
    token_id: int, user: User = Depends(require_user), db: DbSession = Depends(get_db)
):
    """Revoke one of the caller's tokens immediately."""
    tokens_service.revoke_token(db, user, token_id)


# --- webhooks -------------------------------------------------------------------------------------


@router.get("/events/{slug}/webhooks/types")
def webhook_types():
    """Event types a webhook can subscribe to, with a one-line description of each."""
    return {
        "types": webhooks_service.EVENT_TYPES,
        "descriptions": webhooks_service.EVENT_DESCRIPTIONS,
    }


@router.get("/events/{slug}/webhooks")
def list_webhooks(ctx: EventContext = Depends(require_organizer), db: DbSession = Depends(get_db)):
    """Webhooks registered on the event."""
    return {"webhooks": [webhook_out(h) for h in webhooks_service.list_hooks(db, ctx.event)]}


@router.post("/events/{slug}/webhooks", status_code=201)
def create_webhook(
    body: WebhookCreate,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    """Register a webhook.
    Deliveries are signed: `X-Podium-Signature: sha256=HMAC(secret, body)`."""
    hook = webhooks_service.create_hook(db, ctx.event, ctx.user, body.url, body.events, body.secret)
    return {"webhook": webhook_out(hook) | {"secret": hook.secret}}


@router.patch("/events/{slug}/webhooks/{hook_id}")
def update_webhook(
    hook_id: str,
    body: WebhookUpdate,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    """Change a webhook's URL, subscriptions or active flag."""
    hook = webhooks_service.get_hook(db, ctx.event, hook_id)
    return {
        "webhook": webhook_out(
            webhooks_service.set_active(db, ctx.event, ctx.user, hook, body.active)
        )
    }


@router.delete("/events/{slug}/webhooks/{hook_id}", status_code=204)
def delete_webhook(
    hook_id: str, ctx: EventContext = Depends(require_organizer), db: DbSession = Depends(get_db)
):
    """Delete a webhook."""
    webhooks_service.delete_hook(
        db, ctx.event, ctx.user, webhooks_service.get_hook(db, ctx.event, hook_id)
    )


@router.get("/events/{slug}/webhooks/deliveries")
def list_deliveries(
    ctx: EventContext = Depends(require_organizer), db: DbSession = Depends(get_db)
):
    """Recent deliveries with status, attempts and the last error."""
    return {
        "deliveries": [delivery_out(d) for d in webhooks_service.recent_deliveries(db, ctx.event)]
    }


@router.post("/events/{slug}/webhooks/deliveries/{delivery_id}/redeliver")
def redeliver(
    delivery_id: int,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    """Queue a delivery to be sent again."""
    return {
        "delivery": delivery_out(webhooks_service.redeliver(db, ctx.event, ctx.user, delivery_id))
    }


# --- certificates ---------------------------------------------------------------------------------


@router.post("/events/{slug}/certificates/issue/{kind}")
def issue_certificates(
    kind: str,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Bulk-issue: participation (every member of a submitted team), judge (every judge with a
    submitted review), winner (top three after results are published)."""
    issuers = {
        "participation": cert_service.issue_participation,
        "judge": cert_service.issue_judge_records,
        "winner": cert_service.issue_winners,
    }
    if kind not in issuers:
        raise NotFound("kind must be participation, judge or winner.")
    report = issuers[kind](db, settings, ctx.event, ctx.user)
    return {"issued": report.issued, "skipped": report.skipped}


@router.get("/events/{slug}/certificates")
def list_certificates(
    ctx: EventContext = Depends(require_organizer), db: DbSession = Depends(get_db)
):
    """Certificates issued for the event, including revoked ones."""
    return {"certificates": [certificate_out(c) for c in cert_service.for_event(db, ctx.event)]}


@router.post("/events/{slug}/certificates/{serial}/revoke")
def revoke_certificate(
    serial: str, ctx: EventContext = Depends(require_organizer), db: DbSession = Depends(get_db)
):
    """Revoke a certificate; verification then reports it as revoked."""
    cert = cert_service.by_serial(db, serial)
    if cert.event_id != ctx.event.id:
        raise NotFound("No such certificate in this event.")
    return {"certificate": certificate_out(cert_service.revoke(db, ctx.event, ctx.user, cert))}


@router.get("/events/{slug}/judges/me/records")
def my_records(
    ctx: EventContext = Depends(require_event_role(Role.judge)), db: DbSession = Depends(get_db)
):
    """The caller's judge records: signed certificates of the reviews they submitted."""
    return {"records": [certificate_out(c) for c in cert_service.for_user(db, ctx.event, ctx.user)]}


@router.get("/certificates/{serial}")
def get_certificate(
    serial: str, db: DbSession = Depends(get_db), settings: Settings = Depends(get_settings)
):
    """The signed record for a serial (public)."""
    cert = cert_service.by_serial(db, serial)
    return {
        "certificate": certificate_out(cert),
        "public_key_hex": cert_service.public_key_hex(db, settings),
    }


@router.get("/verify/{serial}", response_model=VerifyOut)
def verify_certificate(
    serial: str, db: DbSession = Depends(get_db), settings: Settings = Depends(get_settings)
):
    """Verify a serial against the instance signing key: valid | revoked | invalid | not_found."""
    v = cert_service.verify(db, settings, serial)
    return {
        "serial": serial.upper(),
        "status": v.status,
        "public_key_hex": v.public_key,
        "certificate": certificate_out(v.certificate) if v.certificate else None,
    }


# --- import / export ------------------------------------------------------------------------------


@router.get("/events/{slug}/export.json")
def export_event(ctx: EventContext = Depends(require_organizer), db: DbSession = Depends(get_db)):
    """Everything about the event in the fixtures shape plus a `podium` block. Re-importable."""
    data = importexport.export_event(db, ctx.event)
    return JSONResponse(
        data,
        headers={"Content-Disposition": f'attachment; filename="{ctx.event.slug}-export.json"'},
    )


@router.post("/events/import")
async def import_event(
    request: Request,
    dry_run: bool = Query(True),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Import a fixtures/export JSON.
    Creates the event (you become organizer) or updates it in place
    if you organize it. `dry_run=true` (default) reports what would change without writing."""
    limit = 20 * 1024 * 1024
    if int(request.headers.get("content-length") or 0) > limit:
        raise PayloadTooLarge("That file is larger than 20 MB.")
    raw = await request.body()
    if len(raw) > limit:
        raise PayloadTooLarge("That file is larger than 20 MB.")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationFailed(errors={"body": f"Invalid JSON: {exc}"}) from exc
    outcome = importexport.import_for_user(
        db,
        data,
        user,
        settings=settings,
        dry_run=dry_run,
        source_sha256=hashlib.sha256(raw).hexdigest(),
    )
    return {
        "dry_run": outcome.dry_run,
        "event": outcome.event_slug,
        "counts": outcome.report.counts,
        "duplicates": outcome.report.duplicates,
        "warnings": outcome.report.warnings,
    }
