import enum
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from podium.errors import Conflict, Forbidden, NotFound, ValidationFailed
from podium.models import Event, EventRole, Prize, Project, ProjectStatus, Role, Track, User, utcnow
from podium.services import audit, webhooks


class Stage(enum.StrEnum):
    draft = "draft"
    upcoming = "upcoming"
    open = "open"
    closed = "closed"
    judging = "judging"
    voting = "voting"
    published = "published"
    archived = "archived"


STAGE_LABELS = {
    Stage.draft: "Draft",
    Stage.upcoming: "Upcoming",
    Stage.open: "Submissions open",
    Stage.closed: "Submissions closed",
    Stage.judging: "Judging",
    Stage.voting: "Voting",
    Stage.published: "Results published",
    Stage.archived: "Archived",
}


def stage_of(event: Event, now: datetime | None = None) -> Stage:
    """The stage is derived from timestamps and flags; it is never stored."""
    now = now or utcnow()
    if event.archived_at is not None:
        return Stage.archived
    if event.results_published_at is not None:
        return Stage.published
    if (
        event.voting_open_at is not None
        and event.voting_open_at <= now
        and (event.voting_close_at is None or now < event.voting_close_at)
    ):
        return Stage.voting
    if event.judging_opened_at is not None and event.judging_closed_at is None:
        return Stage.judging
    if not event.is_public:
        return Stage.draft
    if event.submissions_open_at is not None and now < event.submissions_open_at:
        return Stage.upcoming
    if event.submissions_close_at is not None and now >= event.submissions_close_at:
        return Stage.closed
    return Stage.open


def stage_label(event: Event) -> str:
    return STAGE_LABELS[stage_of(event)]


def list_events_for(db: DbSession, user: User | None) -> list[Event]:
    """Public events, plus draft events the user organizes (or all of them for admins)."""
    if user is not None and user.is_admin:
        return list(db.execute(select(Event).order_by(Event.created_at.desc())).scalars())
    query = select(Event).where(Event.is_public.is_(True))
    events = list(db.execute(query.order_by(Event.created_at.desc())).scalars())
    if user is not None:
        mine = db.execute(
            select(Event)
            .join(EventRole, EventRole.event_id == Event.id)
            .where(EventRole.user_id == user.id, Event.is_public.is_(False))
        ).scalars()
        seen = {e.id for e in events}
        events.extend(e for e in mine if e.id not in seen)
    return events


# --- creating and editing ----------------------------------------------------------------------

DATE_FIELDS = (
    "submissions_open_at",
    "submissions_close_at",
    "voting_open_at",
    "voting_close_at",
)


def slugify(name: str) -> str:
    out = "".join(ch.lower() if ch.isalnum() else "-" for ch in name).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out[:60] or "event"


def parse_utc(value: str | None, offset_minutes: int = 0) -> datetime | None:
    """Accept '2026-03-01T18:00' (datetime-local) or ISO 8601. A naive value is read as UTC, or
    as local time when the form says how far its clock is from UTC (`offset_minutes`, east +)."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("Enter a date and time like 2026-03-01T18:00") from exc
    if parsed.tzinfo is None:
        return (parsed - timedelta(minutes=offset_minutes)).replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def tz_offset(data: dict) -> int:
    """Minutes east of UTC that the browser reported (hidden field); 0 without JavaScript."""
    try:
        offset = int(data.get("tz_offset_minutes") or 0)
    except (TypeError, ValueError):
        return 0
    return max(-14 * 60, min(14 * 60, offset))


def validate_event(data: dict) -> tuple[dict, dict[str, str]]:
    clean: dict = {
        "name": (data.get("name") or "").strip(),
        "description": (data.get("description") or "").strip(),
    }
    errors: dict[str, str] = {}
    if not clean["name"]:
        errors["name"] = "Give the event a name."
    elif len(clean["name"]) > 160:
        errors["name"] = "Keep the name under 160 characters."
    offset = tz_offset(data)
    for field in DATE_FIELDS:
        try:
            clean[field] = parse_utc(data.get(field), offset)
        except ValueError as exc:
            errors[field] = str(exc)
            clean[field] = None
    if (
        clean.get("submissions_open_at")
        and clean.get("submissions_close_at")
        and clean["submissions_close_at"] <= clean["submissions_open_at"]
    ):
        errors["submissions_close_at"] = "Submissions must close after they open."
    if (
        clean.get("voting_open_at")
        and clean.get("voting_close_at")
        and clean["voting_close_at"] <= clean["voting_open_at"]
    ):
        errors["voting_close_at"] = "Voting must close after it opens."
    try:
        size = int(data.get("max_team_size") or 4)
    except (TypeError, ValueError):
        size = 0
    if not 1 <= size <= 20:
        errors["max_team_size"] = "Team size must be between 1 and 20."
    clean["max_team_size"] = size
    clean["is_public"] = str(data.get("is_public", "")).lower() in ("1", "true", "on", "yes")
    return clean, errors


def create_event(db: DbSession, user: User, data: dict, *, ip_hash: str | None = None) -> Event:
    clean, errors = validate_event(data)
    if errors:
        raise ValidationFailed(errors=errors)
    base = slugify(clean["name"])
    slug, n = base, 2
    while db.execute(select(Event.id).where(Event.slug == slug)).scalar() is not None:
        slug, n = f"{base}-{n}", n + 1
    event = Event(
        slug=slug,
        name=clean["name"],
        description=clean["description"],
        is_public=clean["is_public"],
        submissions_open_at=clean["submissions_open_at"],
        submissions_close_at=clean["submissions_close_at"],
        voting_open_at=clean["voting_open_at"],
        voting_close_at=clean["voting_close_at"],
        max_team_size=clean["max_team_size"],
        created_by=user.id,
    )
    db.add(event)
    db.flush()
    db.add(EventRole(event_id=event.id, user_id=user.id, role=Role.organizer))
    audit.record(
        db,
        "event.created",
        "event",
        event.public_id,
        event_id=event.id,
        actor_id=user.id,
        meta={"name": event.name},
        ip_hash=ip_hash,
    )
    db.commit()
    return event


def update_event(db: DbSession, event: Event, user: User, data: dict, *, ip_hash=None) -> Event:
    if event.archived_at is not None:
        raise Conflict("Archived events can't be edited.")
    clean, errors = validate_event(data)
    if errors:
        raise ValidationFailed(errors=errors)
    changes = {}
    for field in ("name", "description", "is_public", "max_team_size", *DATE_FIELDS):
        old = getattr(event, field)
        if old != clean[field]:
            changes[field] = [
                str(old) if old is not None else None,
                str(clean[field]) if clean[field] is not None else None,
            ]
            setattr(event, field, clean[field])
    if changes:
        audit.record(
            db,
            "event.updated",
            "event",
            event.public_id,
            event_id=event.id,
            actor_id=user.id,
            meta={"changes": changes},
            ip_hash=ip_hash,
        )
    db.commit()
    return event


# --- lifecycle actions -------------------------------------------------------------------------

ACTIONS = (
    "publish_event",
    "unpublish_event",
    "open_judging",
    "close_judging",
    "publish_results",
    "unpublish_results",
    "archive",
)


def apply_action(db: DbSession, event: Event, user: User, action: str, *, ip_hash=None) -> Event:
    now = utcnow()
    if action not in ACTIONS:
        raise NotFound("Unknown action.")
    if event.archived_at is not None and action != "archive":
        raise Conflict("This event is archived.")
    if action == "publish_event":
        event.is_public = True
    elif action == "unpublish_event":
        event.is_public = False
    elif action == "open_judging":
        if event.judging_opened_at is not None and event.judging_closed_at is None:
            raise Conflict("Judging is already open.")
        if event.results_published_at is not None:
            raise Conflict("Unpublish results before reopening judging.")
        event.judging_opened_at, event.judging_closed_at = now, None
    elif action == "close_judging":
        if event.judging_opened_at is None or event.judging_closed_at is not None:
            raise Conflict("Judging isn't open.")
        event.judging_closed_at = now
    elif action == "publish_results":
        if event.results_published_at is not None:
            raise Conflict("Results are already published.")
        if event.judging_opened_at is not None and event.judging_closed_at is None:
            raise Conflict(
                "Close judging before publishing results, so scores can't change afterwards."
            )
        event.results_published_at = now
    elif action == "unpublish_results":
        if event.results_published_at is None:
            raise Conflict("Results aren't published.")
        event.results_published_at = None
    elif action == "archive":
        event.archived_at = now
    hook_type = {
        "open_judging": "judging.opened",
        "close_judging": "judging.closed",
        "publish_results": "results.published",
        "unpublish_results": "results.unpublished",
    }.get(action)
    if hook_type:
        webhooks.emit(db, event, hook_type, {"event": event.public_id, "slug": event.slug})
    audit.record(
        db,
        f"event.{action}",
        "event",
        event.public_id,
        event_id=event.id,
        actor_id=user.id,
        ip_hash=ip_hash,
    )
    db.commit()
    return event


# --- tracks and prizes -------------------------------------------------------------------------


def add_track(db: DbSession, event: Event, user: User, name: str, description: str = "") -> Track:
    name = name.strip()
    if not name or len(name) > 120:
        raise ValidationFailed(errors={"track_name": "Track names are 1–120 characters."})
    if db.execute(select(Track.id).where(Track.event_id == event.id, Track.name == name)).scalar():
        raise Conflict("A track with that name already exists.")
    position = (
        db.execute(
            select(func.coalesce(func.max(Track.position), -1)).where(Track.event_id == event.id)
        ).scalar_one()
        + 1
    )
    track = Track(event_id=event.id, name=name, description=description.strip(), position=position)
    db.add(track)
    db.flush()
    audit.record(
        db,
        "track.added",
        "track",
        track.public_id,
        event_id=event.id,
        actor_id=user.id,
        meta={"name": name},
    )
    db.commit()
    return track


def remove_track(db: DbSession, event: Event, user: User, public_id: str) -> None:
    track = db.execute(
        select(Track).where(Track.event_id == event.id, Track.public_id == public_id)
    ).scalar_one_or_none()
    if track is None:
        raise NotFound("No such track.")
    used = db.execute(
        select(func.count()).select_from(Project).where(Project.track_id == track.id)
    ).scalar_one()
    if used:
        raise Conflict(f"{used} project(s) are in this track. Move them first.")
    audit.record(
        db,
        "track.removed",
        "track",
        track.public_id,
        event_id=event.id,
        actor_id=user.id,
        meta={"name": track.name},
    )
    db.delete(track)
    db.commit()


def add_prize(
    db: DbSession,
    event: Event,
    user: User,
    name: str,
    amount: str = "",
    description: str = "",
    track_public_id: str = "",
) -> Prize:
    name = name.strip()
    if not name or len(name) > 120:
        raise ValidationFailed(errors={"prize_name": "Prize names are 1–120 characters."})
    track_id = None
    if track_public_id:
        track_id = db.execute(
            select(Track.id).where(Track.event_id == event.id, Track.public_id == track_public_id)
        ).scalar()
    position = (
        db.execute(
            select(func.coalesce(func.max(Prize.position), -1)).where(Prize.event_id == event.id)
        ).scalar_one()
        + 1
    )
    prize = Prize(
        event_id=event.id,
        name=name,
        amount_text=amount.strip()[:60],
        description=description.strip(),
        track_id=track_id,
        position=position,
    )
    db.add(prize)
    db.flush()
    audit.record(
        db,
        "prize.added",
        "prize",
        prize.public_id,
        event_id=event.id,
        actor_id=user.id,
        meta={"name": name},
    )
    db.commit()
    return prize


def remove_prize(db: DbSession, event: Event, user: User, public_id: str) -> None:
    prize = db.execute(
        select(Prize).where(Prize.event_id == event.id, Prize.public_id == public_id)
    ).scalar_one_or_none()
    if prize is None:
        raise NotFound("No such prize.")
    audit.record(
        db,
        "prize.removed",
        "prize",
        prize.public_id,
        event_id=event.id,
        actor_id=user.id,
        meta={"name": prize.name},
    )
    db.delete(prize)
    db.commit()


def award_prize(
    db: DbSession,
    event: Event,
    user: User,
    prize_public_id: str,
    project_public_id: str | None,
    *,
    ip_hash: str | None = None,
) -> Prize:
    """Link a prize to a submitted project (or clear it). Winner certificates and the public
    results page read these awards; the ranking itself is never changed by them."""
    prize = db.execute(
        select(Prize).where(Prize.event_id == event.id, Prize.public_id == prize_public_id)
    ).scalar_one_or_none()
    if prize is None:
        raise NotFound("No such prize.")
    project = None
    if project_public_id:
        project = db.execute(
            select(Project).where(
                Project.event_id == event.id,
                Project.public_id == project_public_id,
                Project.status == ProjectStatus.submitted,
            )
        ).scalar_one_or_none()
        if project is None:
            raise NotFound("Choose a submitted project from this event.")
    prize.project_id = project.id if project else None
    audit.record(
        db,
        "prize.awarded" if project else "prize.cleared",
        "prize",
        prize.public_id,
        event_id=event.id,
        actor_id=user.id,
        ip_hash=ip_hash,
        meta={"name": prize.name, "project": project.public_id if project else None},
    )
    db.commit()
    return prize


def awards(event: Event) -> list[Prize]:
    """Prizes that have been awarded, in display order."""
    return [p for p in sorted(event.prizes, key=lambda p: p.position) if p.project_id is not None]


def require_can_manage(db: DbSession, event: Event, user: User) -> None:
    from podium.security.deps import is_organizer

    if not is_organizer(db, event, user):
        raise Forbidden("Only organizers of this event can do this.")


VOTING_MODES = {"link", "email", "account"}


def update_voting_settings(
    db: DbSession, event: Event, user: User, data: dict, *, ip_hash=None
) -> Event:
    from podium.models import VotingMode

    errors: dict[str, str] = {}
    mode = str(data.get("voting_mode") or event.voting_mode.value)
    if mode not in VOTING_MODES:
        errors["voting_mode"] = "Choose link, email or account."
    try:
        credits = int(data.get("voting_credits") or event.voting_credits)
        if not 1 <= credits <= 1000:
            raise ValueError
    except (TypeError, ValueError):
        errors["voting_credits"] = "Credits must be a whole number from 1 to 1,000."
        credits = event.voting_credits
    if errors:
        raise ValidationFailed(errors=errors)
    from podium.models import Vote
    from podium.services.voting import voting_is_open

    if voting_is_open(event):
        cast = db.execute(
            select(func.count()).select_from(Vote).where(Vote.event_id == event.id)
        ).scalar_one()
        if cast:
            raise Conflict(
                f"Voting is open and {cast} vote(s) are in. "
                "Close the window before changing how people vote."
            )
    quadratic = str(data.get("quadratic_enabled", "")).lower() in ("1", "true", "on", "yes")
    comments = str(data.get("comments_enabled", "")).lower() in ("1", "true", "on", "yes")
    changes = {}
    for field, value in (
        ("voting_mode", VotingMode(mode)),
        ("voting_credits", credits),
        ("quadratic_enabled", quadratic),
        ("comments_enabled", comments),
    ):
        old = getattr(event, field)
        if old != value:
            changes[field] = [getattr(old, "value", old), getattr(value, "value", value)]
            setattr(event, field, value)
    if changes:
        audit.record(
            db,
            "voting.settings",
            "event",
            event.public_id,
            event_id=event.id,
            actor_id=user.id,
            meta={"changes": changes},
            ip_hash=ip_hash,
        )
    db.commit()
    return event


# --- organizers ------------------------------------------------------------------------------


def organizers(db: DbSession, event: Event) -> list[User]:
    return list(
        db.execute(
            select(User)
            .join(EventRole, EventRole.user_id == User.id)
            .where(EventRole.event_id == event.id, EventRole.role == Role.organizer)
            .order_by(User.name)
        ).scalars()
    )


def add_organizer(db: DbSession, event: Event, actor: User, email: str) -> User:
    from podium.services.auth import normalize_email

    email = normalize_email(email)
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        raise NotFound("No account with that email yet. Ask them to create one first.")
    role = db.execute(
        select(EventRole).where(EventRole.event_id == event.id, EventRole.user_id == user.id)
    ).scalar_one_or_none()
    if role is not None:
        if role.role == Role.organizer:
            raise Conflict(f"{user.name} already organizes this event.")
        raise Conflict(f"{user.name} is a {role.role} in this event and can't also organize it.")
    db.add(EventRole(event_id=event.id, user_id=user.id, role=Role.organizer))
    audit.record(
        db,
        "organizer.added",
        "user",
        user.public_id,
        event_id=event.id,
        actor_id=actor.id,
        meta={"email": email},
    )
    db.commit()
    return user


def remove_organizer(db: DbSession, event: Event, actor: User, public_id: str) -> None:
    user = db.execute(select(User).where(User.public_id == public_id)).scalar_one_or_none()
    role = (
        db.execute(
            select(EventRole).where(EventRole.event_id == event.id, EventRole.user_id == user.id)
        ).scalar_one_or_none()
        if user
        else None
    )
    if user is None or role is None or role.role != Role.organizer:
        raise NotFound("That person doesn't organize this event.")
    if len(organizers(db, event)) <= 1:
        raise Conflict("An event needs at least one organizer.")
    db.delete(role)
    audit.record(
        db, "organizer.removed", "user", user.public_id, event_id=event.id, actor_id=actor.id
    )
    db.commit()
