"""Request-scoped dependencies: who is calling, which event, which role. Every one of these runs
before a request body is parsed, so a closed event answers 403 even to a malformed body."""

import hashlib
from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from podium.config import Settings, get_settings
from podium.db import get_db
from podium.errors import Closed, Forbidden, NotFound, Unauthorized
from podium.models import ApiToken, Event, EventRole, Role, User, utcnow
from podium.security.sessions import COOKIE_NAME, resolve_session


def current_user(
    request: Request, db: DbSession = Depends(get_db), settings: Settings = Depends(get_settings)
) -> User | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token_hash = hashlib.sha256(auth[7:].strip().encode()).hexdigest()
        row = db.execute(
            select(ApiToken).where(ApiToken.token_hash == token_hash)
        ).scalar_one_or_none()
        if row is None or row.revoked_at is not None:
            return None
        if row.expires_at is not None and row.expires_at <= utcnow():
            return None
        if row.scope == "read" and request.method not in ("GET", "HEAD", "OPTIONS"):
            raise Forbidden("This token is read-only. Create a write token on your account page.")
        row.last_used_at = utcnow()
        db.commit()
        return db.get(User, row.user_id)
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return resolve_session(db, token)


def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise Unauthorized("Sign in to continue.")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise Forbidden("Only instance admins can do this.")
    return user


def role_in_event(db: DbSession, event: Event, user: User | None) -> Role | None:
    if user is None:
        return None
    row = db.execute(
        select(EventRole.role).where(EventRole.event_id == event.id, EventRole.user_id == user.id)
    ).scalar_one_or_none()
    return row


def is_organizer(db: DbSession, event: Event, user: User | None) -> bool:
    if user is None:
        return False
    return user.is_admin or role_in_event(db, event, user) == Role.organizer


@dataclass
class EventContext:
    event: Event
    user: User | None
    role: Role | None  # explicit event role; admins have role None but is_organizer() is True

    @property
    def is_organizer(self) -> bool:
        return (self.user is not None and self.user.is_admin) or self.role == Role.organizer


def load_event(
    slug: str, db: DbSession = Depends(get_db), user: User | None = Depends(current_user)
) -> EventContext:
    event = db.execute(select(Event).where(Event.slug == slug)).scalar_one_or_none()
    if event is None:
        raise NotFound("No such event.")
    role = role_in_event(db, event, user)
    ctx = EventContext(event=event, user=user, role=role)
    if not event.is_public and not ctx.is_organizer:
        raise NotFound("No such event.")
    return ctx


def require_event_role(*roles: Role):
    """Allow the listed event roles; organizers and admins always pass."""

    def dependency(ctx: EventContext = Depends(load_event)) -> EventContext:
        if ctx.user is None:
            raise Unauthorized("Sign in to continue.")
        if ctx.is_organizer or ctx.role in roles:
            return ctx
        raise Forbidden("You don't have access to this part of the event.")

    dependency.roles = roles  # read by web/apiref.py to print "who can call it"
    return dependency


def require_organizer(ctx: EventContext = Depends(load_event)) -> EventContext:
    if ctx.user is None:
        raise Unauthorized("Sign in to continue.")
    if not ctx.is_organizer:
        raise Forbidden("Only organizers of this event can do this.")
    return ctx


def submissions_are_open(event: Event) -> bool:
    now = utcnow()
    if event.archived_at is not None:
        return False
    if event.submissions_open_at is not None and now < event.submissions_open_at:
        return False
    return not (event.submissions_close_at is not None and now >= event.submissions_close_at)


def require_submissions_open(
    ctx: EventContext = Depends(require_event_role(Role.participant)),
) -> EventContext:
    if not submissions_are_open(ctx.event):
        closes = ctx.event.submissions_close_at
        when = closes.strftime("%d %b %Y, %H:%M UTC") if closes else "for this event"
        raise Closed(f"Submissions closed on {when}.")
    return ctx


def require_can_create_event(
    user: User = Depends(require_user), settings: Settings = Depends(get_settings)
) -> User:
    if user.is_admin or settings.open_event_creation:
        return user
    raise Forbidden("Only instance admins can create events on this Podium.")
