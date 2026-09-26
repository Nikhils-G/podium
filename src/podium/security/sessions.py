"""Opaque session tokens. The cookie carries the token; the database stores only its SHA-256."""

import hashlib
import hmac
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from podium.models import Session, User, utcnow

COOKIE_NAME = "session"
DEMO_PREFIXES = {
    "organizer": "org",
    "judge_a": "jdg_a",
    "judge_b": "jdg_b",
    "participant": "prt",
    "admin": "adm",
}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def demo_session_token(secret_key: str, role: str) -> str:
    """Deterministic per install + role so a committed .dogfood.toml keeps working after reboots."""
    digest = hmac.new(secret_key.encode(), f"demo-session:{role}".encode(), "sha256").hexdigest()
    return f"{DEMO_PREFIXES[role]}_{digest[:20]}"


def create_session(
    db: DbSession, user: User, *, days: int, label: str | None = None, token: str | None = None
) -> str:
    token = token or secrets.token_urlsafe(32)
    row = db.get(Session, hash_token(token))
    if row is None:
        row = Session(token_hash=hash_token(token), user_id=user.id)
        db.add(row)
    row.user_id = user.id
    row.label = label
    row.revoked_at = None
    row.expires_at = utcnow() + timedelta(days=days)
    return token


def resolve_session(db: DbSession, token: str) -> User | None:
    row = db.execute(
        select(Session).where(Session.token_hash == hash_token(token))
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None or row.expires_at <= utcnow():
        return None
    return db.get(User, row.user_id)


def revoke_session(db: DbSession, token: str) -> None:
    """Revoke a session server-side. Demo sessions are shared, deterministic tokens that the
    acceptance checker and the demo video rely on, so signing out of one only drops the cookie."""
    row = db.get(Session, hash_token(token))
    if row is not None and not (row.label or "").startswith("demo:"):
        row.revoked_at = utcnow()
