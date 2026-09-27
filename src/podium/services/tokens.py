"""Personal API tokens for the REST API (Bearer auth).

The raw token is shown once; only its SHA-256 is stored."""

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from podium.errors import NotFound, ValidationFailed
from podium.models import ApiToken, User, utcnow
from podium.services import audit


def list_tokens(db: DbSession, user: User) -> list[ApiToken]:
    return list(
        db.execute(
            select(ApiToken)
            .where(ApiToken.user_id == user.id, ApiToken.revoked_at.is_(None))
            .order_by(ApiToken.created_at.desc())
        ).scalars()
    )


SCOPES = ("read", "write")


def create_token(
    db: DbSession,
    user: User,
    name: str,
    *,
    scope: str = "read",
    expires_in_days: int | None = 90,
) -> tuple[ApiToken, str]:
    name = name.strip()
    if not name or len(name) > 80:
        raise ValidationFailed(errors={"name": "Give the token a name (1–80 characters)."})
    if scope not in SCOPES:
        raise ValidationFailed(errors={"scope": "Scope is read or write."})
    if expires_in_days is not None and not 1 <= expires_in_days <= 3650:
        raise ValidationFailed(errors={"expires_in_days": "Expiry is 1 to 3,650 days."})
    raw = "pdm_" + secrets.token_urlsafe(32)
    token = ApiToken(
        user_id=user.id,
        name=name,
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        prefix=raw[:12],
        scope=scope,
        expires_at=utcnow() + timedelta(days=expires_in_days) if expires_in_days else None,
    )
    db.add(token)
    db.flush()
    audit.record(
        db,
        "token.created",
        "api_token",
        token.id,
        actor_id=user.id,
        meta={
            "name": name,
            "scope": scope,
            "expires_at": token.expires_at.isoformat() if token.expires_at else None,
        },
    )
    db.commit()
    return token, raw


def revoke_token(db: DbSession, user: User, token_id: int) -> None:
    token = db.get(ApiToken, token_id)
    if token is None or token.user_id != user.id or token.revoked_at is not None:
        raise NotFound("No such token.")
    token.revoked_at = utcnow()
    audit.record(db, "token.revoked", "api_token", token.id, actor_id=user.id)
    db.commit()
