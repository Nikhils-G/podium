"""Public certificate pages, verification, the signing key, and judges' own records."""

import io

import segno
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session as DbSession

from podium.config import Settings, get_settings
from podium.db import get_db
from podium.models import Event, Role, User
from podium.security.deps import EventContext, current_user, require_event_role
from podium.services import certificates as cert_service
from podium.web.rendering import render

router = APIRouter(include_in_schema=False)


@router.get("/.well-known/podium-signing-key.json")
def signing_key(db: DbSession = Depends(get_db), settings: Settings = Depends(get_settings)):
    return {
        "algorithm": "ed25519",
        "public_key_hex": cert_service.public_key_hex(db, settings),
        "issuer": settings.base_url,
        "verify_url": f"{settings.base_url}/verify/{{serial}}",
    }


@router.get("/certificates/{serial}.json")
def certificate_json(
    serial: str, db: DbSession = Depends(get_db), settings: Settings = Depends(get_settings)
):
    cert = cert_service.by_serial(db, serial)
    return JSONResponse(
        {
            "payload": cert.payload,
            "signature": cert.signature,
            "algorithm": "ed25519",
            "public_key_hex": cert_service.public_key_hex(db, settings),
            "revoked_at": cert.revoked_at.isoformat() if cert.revoked_at else None,
        }
    )


@router.get("/certificates/{serial}")
def certificate_page(
    request: Request,
    serial: str,
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: User | None = Depends(current_user),
):
    verification = cert_service.verify(db, settings, serial)
    cert = verification.certificate
    if cert is None:
        from podium.errors import NotFound

        raise NotFound("No certificate with that serial.")
    event = db.get(Event, cert.event_id)
    return render(
        request,
        "public/certificate.html",
        title=f"Certificate {cert.serial}",
        cert=cert,
        event=event,
        user=user,
        verification=verification,
        verify_url=f"{settings.base_url}/verify/{cert.serial}",
    )


@router.get("/verify")
def verify_form(
    request: Request,
    serial: str = "",
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: User | None = Depends(current_user),
):
    verification = cert_service.verify(db, settings, serial) if serial else None
    event = (
        db.get(Event, verification.certificate.event_id)
        if verification and verification.certificate
        else None
    )
    return render(
        request,
        "public/verify.html",
        title="Verify a certificate",
        serial=serial,
        verification=verification,
        event=event,
        user=user,
    )


@router.get("/verify/{serial}.svg")
def verify_qr(serial: str, settings: Settings = Depends(get_settings)):
    qr = segno.make(f"{settings.base_url}/verify/{serial.upper()}", error="m")
    buffer = io.BytesIO()
    qr.save(buffer, kind="svg", scale=4, border=1, dark="#0b0b0b", light=None)
    return Response(
        buffer.getvalue(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/verify/{serial}")
def verify_page(
    request: Request,
    serial: str,
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: User | None = Depends(current_user),
):
    verification = cert_service.verify(db, settings, serial)
    event = db.get(Event, verification.certificate.event_id) if verification.certificate else None
    return render(
        request,
        "public/verify.html",
        title="Verify a certificate",
        serial=serial.upper(),
        verification=verification,
        event=event,
        user=user,
    )


@router.get("/e/{slug}/judge/records")
def judge_records(
    request: Request,
    ctx: EventContext = Depends(require_event_role(Role.judge)),
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    from podium.web.judge import _console

    certs = cert_service.for_user(db, ctx.event, ctx.user)
    return render(
        request,
        "judge/records.html",
        title="Your records",
        **_console(ctx, "records", certificates=certs, base_url=settings.base_url),
    )
