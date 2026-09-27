import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from podium.errors import ValidationFailed
from podium.models import User, utcnow
from podium.security.passwords import hash_password, needs_rehash, verify_password
from podium.services import audit

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_registration(db: DbSession, email: str, name: str, password: str) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not EMAIL_RE.match(email):
        errors["email"] = "Enter a valid email address."
    elif db.execute(select(User.id).where(User.email == email)).scalar() is not None:
        errors["email"] = "An account with this email already exists. Sign in instead."
    if not name.strip():
        errors["name"] = "Enter your name."
    elif len(name.strip()) > 120:
        errors["name"] = "Keep your name under 120 characters."
    if len(password) < 8:
        errors["password"] = "Use at least 8 characters."
    return errors


def register(
    db: DbSession, *, email: str, name: str, password: str, ip_hash: str | None = None
) -> User:
    email = normalize_email(email)
    errors = validate_registration(db, email, name, password)
    if errors:
        raise ValidationFailed(errors=errors)
    first = instance_is_empty(db)
    user = User(
        email=email, name=name.strip(), password_hash=hash_password(password), is_admin=first
    )
    db.add(user)
    db.flush()
    audit.record(db, "user.registered", "user", user.public_id, actor_id=user.id, ip_hash=ip_hash)
    if first:
        # A fresh self-hosted instance has nobody who could create an event or promote anyone;
        # the person who installs it and signs up first is its admin. Seeded installs never
        # reach this branch because seeding runs before the first request.
        audit.record(
            db,
            "user.bootstrap_admin",
            "user",
            user.public_id,
            actor_id=user.id,
            ip_hash=ip_hash,
            meta={"reason": "first account on an empty instance"},
        )
    db.commit()
    return user


def instance_is_empty(db: DbSession) -> bool:
    return db.execute(select(func.count()).select_from(User)).scalar_one() == 0


def authenticate(
    db: DbSession, *, email: str, password: str, ip_hash: str | None = None
) -> User | None:
    email = normalize_email(email)
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None or not verify_password(user.password_hash, password):
        audit.record(db, "user.login_failed", "user", email, ip_hash=ip_hash)
        db.commit()
        return None
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    user.last_login_at = utcnow()
    audit.record(db, "user.login", "user", user.public_id, actor_id=user.id, ip_hash=ip_hash)
    db.commit()
    return user
