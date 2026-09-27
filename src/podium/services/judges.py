"""Judge invitations and roster. Invites are links (no email dependency); accepting one grants the
judge role, which is refused for anyone already competing in the event."""

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session as DbSession

from podium.errors import Conflict, Forbidden, NotFound, ValidationFailed
from podium.models import (
    Assignment,
    AssignmentStatus,
    Event,
    EventRole,
    JudgeInvite,
    JudgeTrack,
    Role,
    Track,
    User,
    utcnow,
)
from podium.services import audit
from podium.services.auth import EMAIL_RE, normalize_email
from podium.services.text import plural


@dataclass
class JudgeRow:
    user: User
    tracks: list[Track]
    assigned: int
    done: int


@dataclass
class InviteRow:
    invite: JudgeInvite
    tracks: list[Track] = field(default_factory=list)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _assignment_counts_statement(event_id: int):
    # (judge_id, assigned, done) per judge. CASE, not iif(): portable to PostgreSQL.
    return (
        select(
            Assignment.judge_id,
            func.count(Assignment.id),
            func.sum(case((Assignment.status == AssignmentStatus.done, 1), else_=0)),
        )
        .where(Assignment.event_id == event_id)
        .group_by(Assignment.judge_id)
    )


def list_judges(db: DbSession, event: Event) -> list[JudgeRow]:
    users = (
        db.execute(
            select(User)
            .join(EventRole, EventRole.user_id == User.id)
            .where(EventRole.event_id == event.id, EventRole.role == Role.judge)
            .order_by(User.name)
        )
        .scalars()
        .all()
    )
    tracks_by_user: dict[int, list[Track]] = {}
    for user_id, track in db.execute(
        select(JudgeTrack.user_id, Track)
        .join(Track, Track.id == JudgeTrack.track_id)
        .where(JudgeTrack.event_id == event.id)
        .order_by(Track.position)
    ).all():
        tracks_by_user.setdefault(user_id, []).append(track)
    counts = {
        judge_id: (assigned, done)
        for judge_id, assigned, done in db.execute(_assignment_counts_statement(event.id)).all()
    }
    return [
        JudgeRow(
            user=u,
            tracks=tracks_by_user.get(u.id, []),
            assigned=counts.get(u.id, (0, 0))[0],
            done=int(counts.get(u.id, (0, 0))[1] or 0),
        )
        for u in users
    ]


def pending_invites(db: DbSession, event: Event) -> list[InviteRow]:
    invites = (
        db.execute(
            select(JudgeInvite)
            .where(JudgeInvite.event_id == event.id, JudgeInvite.accepted_at.is_(None))
            .order_by(JudgeInvite.created_at.desc())
        )
        .scalars()
        .all()
    )
    tracks = {t.public_id: t for t in event.tracks}
    return [
        InviteRow(invite=i, tracks=[tracks[p] for p in i.track_public_ids if p in tracks])
        for i in invites
    ]


def invite_judge(
    db: DbSession,
    event: Event,
    organizer: User,
    email: str,
    track_public_ids: list[str],
    *,
    days: int = 30,
) -> tuple[JudgeInvite, str]:
    """Create an invite and return it with the raw token (shown once, as a link)."""
    email = normalize_email(email)
    if not EMAIL_RE.match(email):
        raise ValidationFailed(errors={"email": "Enter a valid email address."})
    known = {t.public_id for t in event.tracks}
    tracks = [p for p in track_public_ids if p in known]
    existing = db.execute(
        select(User)
        .join(EventRole, EventRole.user_id == User.id)
        .where(User.email == email, EventRole.event_id == event.id)
    ).scalar_one_or_none()
    if existing is not None:
        role = db.execute(
            select(EventRole.role).where(
                EventRole.event_id == event.id, EventRole.user_id == existing.id
            )
        ).scalar_one()
        if role == Role.judge:
            raise Conflict(f"{email} is already a judge in this event.")
        raise Conflict(f"{email} is a {role} in this event and can't also judge it.")
    pending = db.execute(
        select(JudgeInvite).where(
            JudgeInvite.event_id == event.id,
            JudgeInvite.email == email,
            JudgeInvite.accepted_at.is_(None),
            JudgeInvite.expires_at > utcnow(),
        )
    ).scalar_one_or_none()
    if pending is not None:
        raise Conflict(
            f"{email} already has a pending invitation. Regenerate its link or revoke it below."
        )
    token = secrets.token_urlsafe(24)
    invite = JudgeInvite(
        event_id=event.id,
        email=email,
        token_hash=_hash(token),
        track_public_ids=tracks,
        invited_by=organizer.id,
        expires_at=utcnow() + timedelta(days=days),
    )
    db.add(invite)
    db.flush()
    audit.record(
        db,
        "judge.invited",
        "judge_invite",
        invite.id,
        event_id=event.id,
        actor_id=organizer.id,
        meta={"email": email, "tracks": tracks},
    )
    db.commit()
    return invite, token


def pending_invite(db: DbSession, event: Event, invite_id: int) -> JudgeInvite:
    invite = db.execute(
        select(JudgeInvite).where(
            JudgeInvite.event_id == event.id,
            JudgeInvite.id == invite_id,
            JudgeInvite.accepted_at.is_(None),
        )
    ).scalar_one_or_none()
    if invite is None:
        raise NotFound("No pending invitation with that id.")
    return invite


def regenerate_invite(
    db: DbSession, event: Event, organizer: User, invite_id: int, *, days: int = 30
) -> tuple[JudgeInvite, str]:
    """A fresh link for a pending invite; the old one stops working at once."""
    invite = pending_invite(db, event, invite_id)
    token = secrets.token_urlsafe(24)
    invite.token_hash = _hash(token)
    invite.expires_at = utcnow() + timedelta(days=days)
    audit.record(
        db,
        "judge.invite_regenerated",
        "invite",
        str(invite.id),
        event_id=event.id,
        actor_id=organizer.id,
        meta={"email": invite.email},
    )
    db.commit()
    return invite, token


def revoke_invite(db: DbSession, event: Event, organizer: User, invite_id: int) -> None:
    invite = pending_invite(db, event, invite_id)
    audit.record(
        db,
        "judge.invite_revoked",
        "invite",
        str(invite.id),
        event_id=event.id,
        actor_id=organizer.id,
        meta={"email": invite.email},
    )
    db.delete(invite)
    db.commit()


def invite_refusal(db: DbSession, invite: JudgeInvite, user: User) -> str | None:
    """Why this signed-in person cannot accept the invite, or None when they can."""
    if invite.accepted_at is not None:
        return None if invite.accepted_user_id == user.id else "This invitation was already used."
    if normalize_email(user.email) != invite.email:
        return (
            f"This invitation was sent to {invite.email}, but you're signed in as {user.email}. "
            "Sign out and use the invited address, or ask the organizer for a new link."
        )
    role = db.execute(
        select(EventRole.role).where(
            EventRole.event_id == invite.event_id, EventRole.user_id == user.id
        )
    ).scalar_one_or_none()
    if role is not None and role != Role.judge:
        return (
            f"You're a {role.value} in this event, so you can't judge it. "
            "Conflicts of interest are blocked by design."
        )
    return None


def invite_by_token(db: DbSession, token: str) -> JudgeInvite:
    invite = db.execute(
        select(JudgeInvite).where(JudgeInvite.token_hash == _hash(token))
    ).scalar_one_or_none()
    if invite is None or invite.expires_at <= utcnow():
        raise NotFound("That invitation isn't valid any more.")
    return invite


def accept_invite(db: DbSession, invite: JudgeInvite, user: User) -> Event:
    event = db.get(Event, invite.event_id)
    if invite.accepted_at is not None:
        if invite.accepted_user_id == user.id:
            return event
        raise Conflict("This invitation was already used.")
    if normalize_email(user.email) != invite.email:
        raise Forbidden(
            f"This invitation was sent to {invite.email}, but you're signed in as {user.email}. "
            "Sign out and use the invited address, or ask the organizer for a new link."
        )
    role = db.execute(
        select(EventRole).where(EventRole.event_id == event.id, EventRole.user_id == user.id)
    ).scalar_one_or_none()
    if role is not None and role.role != Role.judge:
        raise Forbidden(
            f"You're a {role.role} in this event, so you can't judge it. Conflicts of interest are "
            "blocked by design."
        )
    if role is None:
        db.add(EventRole(event_id=event.id, user_id=user.id, role=Role.judge))
    set_judge_tracks(db, event, user, invite.track_public_ids, commit=False)
    invite.accepted_at = utcnow()
    invite.accepted_user_id = user.id
    audit.record(
        db,
        "judge.accepted",
        "user",
        user.public_id,
        event_id=event.id,
        actor_id=user.id,
        meta={"invite": invite.id},
    )
    db.commit()
    return event


def set_judge_tracks(
    db: DbSession, event: Event, judge: User, track_public_ids: list[str], *, commit: bool = True
) -> None:
    wanted = {t.id for t in event.tracks if t.public_id in track_public_ids}
    current = (
        db.execute(
            select(JudgeTrack).where(
                JudgeTrack.event_id == event.id, JudgeTrack.user_id == judge.id
            )
        )
        .scalars()
        .all()
    )
    for row in current:
        if row.track_id not in wanted:
            db.delete(row)
    have = {row.track_id for row in current}
    for track_id in wanted - have:
        db.add(JudgeTrack(event_id=event.id, user_id=judge.id, track_id=track_id))
    if commit:
        db.commit()


def remove_judge(db: DbSession, event: Event, organizer: User, judge_public_id: str) -> None:
    judge = db.execute(select(User).where(User.public_id == judge_public_id)).scalar_one_or_none()
    role = (
        db.execute(
            select(EventRole).where(EventRole.event_id == event.id, EventRole.user_id == judge.id)
        ).scalar_one_or_none()
        if judge
        else None
    )
    if judge is None or role is None or role.role != Role.judge:
        raise NotFound("That person isn't a judge in this event.")
    done = db.execute(
        select(func.count())
        .select_from(Assignment)
        .where(
            Assignment.event_id == event.id,
            Assignment.judge_id == judge.id,
            Assignment.status == AssignmentStatus.done,
        )
    ).scalar_one()
    if done:
        raise Conflict(
            f"{judge.name} has submitted {plural(done, 'review')}. Remove their assignments first."
        )
    for row in db.execute(
        select(JudgeTrack).where(JudgeTrack.event_id == event.id, JudgeTrack.user_id == judge.id)
    ).scalars():
        db.delete(row)
    drafts = 0
    for row in db.execute(
        select(Assignment).where(Assignment.event_id == event.id, Assignment.judge_id == judge.id)
    ).scalars():
        # reviews.assignment_id is NOT NULL and has no ORM cascade: drop the draft first
        if row.review is not None:
            db.delete(row.review)
            drafts += 1
        db.delete(row)
    db.delete(role)
    audit.record(
        db,
        "judge.removed",
        "user",
        judge.public_id,
        event_id=event.id,
        actor_id=organizer.id,
        meta={"drafts_discarded": drafts},
    )
    db.commit()


def judge_by_public_id(db: DbSession, event: Event, public_id: str) -> User:
    judge = db.execute(
        select(User)
        .join(EventRole, EventRole.user_id == User.id)
        .where(
            User.public_id == public_id,
            EventRole.event_id == event.id,
            EventRole.role == Role.judge,
        )
    ).scalar_one_or_none()
    if judge is None:
        raise NotFound("No such judge in this event.")
    return judge
