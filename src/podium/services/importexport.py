"""Whole-event export and import in the DOGFOOD fixtures shape, extended with a `podium` block
that carries everything the base shape can't (rubric, prizes, dates, project fields, assignments).
The seeder is this importer, so a fixtures.json and an export.json load the same way."""

import hashlib
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from podium import __version__
from podium.config import Settings
from podium.errors import Conflict, Forbidden, ValidationFailed
from podium.models import (
    Assignment,
    AuditLog,
    Certificate,
    Comment,
    Event,
    EventRole,
    JudgeTrack,
    Prize,
    Project,
    Review,
    ReviewStatus,
    Role,
    RubricCriterion,
    ScoreItem,
    Team,
    TeamMember,
    Track,
    User,
    Vote,
    utcnow,
)
from podium.security.deps import is_organizer
from podium.seed.fixtures import ImportReport, foreign_ids, import_fixtures
from podium.services import audit


def _iso(value):
    return value.isoformat() if value else None


def export_event(db: DbSession, event: Event) -> dict:
    tracks = list(
        db.execute(
            select(Track).where(Track.event_id == event.id).order_by(Track.position)
        ).scalars()
    )
    judges = (
        db.execute(
            select(User)
            .join(EventRole, EventRole.user_id == User.id)
            .where(EventRole.event_id == event.id, EventRole.role == Role.judge)
            .order_by(User.id)
        )
        .scalars()
        .all()
    )
    judge_tracks: dict[int, list[str]] = {}
    for user_id, track_public in db.execute(
        select(JudgeTrack.user_id, Track.public_id)
        .join(Track, Track.id == JudgeTrack.track_id)
        .where(JudgeTrack.event_id == event.id)
    ).all():
        judge_tracks.setdefault(user_id, []).append(track_public)
    teams = list(
        db.execute(select(Team).where(Team.event_id == event.id).order_by(Team.id)).scalars()
    )
    members: dict[int, list[str]] = {}
    for team_id, email in db.execute(
        select(TeamMember.team_id, User.email)
        .join(User, User.id == TeamMember.user_id)
        .join(Team, Team.id == TeamMember.team_id)
        .where(Team.event_id == event.id)
        .order_by(TeamMember.id)
    ).all():
        members.setdefault(team_id, []).append(email)
    projects = list(
        db.execute(
            select(Project).where(Project.event_id == event.id).order_by(Project.id)
        ).scalars()
    )
    criteria = list(
        db.execute(
            select(RubricCriterion)
            .where(RubricCriterion.event_id == event.id)
            .order_by(RubricCriterion.position)
        ).scalars()
    )
    crit_by_id = {c.id: c for c in criteria}
    reviews = db.execute(
        select(Review, User, Project)
        .join(User, User.id == Review.judge_id)
        .join(Project, Project.id == Review.project_id)
        .where(Review.event_id == event.id)
        .order_by(Review.id)
    ).all()
    items: dict[int, dict[str, int]] = {}
    for item in db.execute(
        select(ScoreItem)
        .join(Review, Review.id == ScoreItem.review_id)
        .where(Review.event_id == event.id)
    ).scalars():
        crit = crit_by_id.get(item.criterion_id)
        if crit:
            items.setdefault(item.review_id, {})[crit.key] = item.value
    assignments = db.execute(
        select(Assignment, User, Project)
        .join(User, User.id == Assignment.judge_id)
        .join(Project, Project.id == Assignment.project_id)
        .where(Assignment.event_id == event.id)
        .order_by(Assignment.id)
    ).all()
    prizes = list(
        db.execute(
            select(Prize).where(Prize.event_id == event.id).order_by(Prize.position)
        ).scalars()
    )
    track_public = {t.id: t.public_id for t in tracks}
    team_public = {t.id: t.public_id for t in teams}
    project_public = {p.id: p.public_id for p in projects}
    user_public = {u.id: u.public_id for u in db.execute(select(User)).scalars()}
    votes = list(
        db.execute(select(Vote).where(Vote.event_id == event.id).order_by(Vote.id)).scalars()
    )
    comments = list(
        db.execute(
            select(Comment)
            .join(Project, Project.id == Comment.project_id)
            .where(Project.event_id == event.id)
            .order_by(Comment.id)
        ).scalars()
    )
    certificates = list(
        db.execute(
            select(Certificate).where(Certificate.event_id == event.id).order_by(Certificate.id)
        ).scalars()
    )
    audit_rows = list(
        db.execute(
            select(AuditLog).where(AuditLog.event_id == event.id).order_by(AuditLog.id)
        ).scalars()
    )

    return {
        "event": {
            "id": event.public_id,
            "name": event.name,
            "submissions_close": _iso(event.submissions_close_at),
        },
        "tracks": [{"id": t.public_id, "name": t.name} for t in tracks],
        "judges": [
            {
                "id": j.public_id,
                "name": j.name,
                "email": j.email,
                "tracks": judge_tracks.get(j.id, []),
            }
            for j in judges
        ],
        "teams": [
            {"id": t.public_id, "name": t.name, "members": members.get(t.id, [])} for t in teams
        ],
        "projects": [
            {
                "id": p.public_id,
                "team": team_public.get(p.team_id),
                "track": track_public.get(p.track_id),
                "title": p.title,
                "summary": p.summary,
                "repo_url": p.repo_url,
                "submitted_at": _iso(p.submitted_at),
            }
            for p in projects
        ],
        "scores": [
            {
                "judge": u.public_id,
                "project": p.public_id,
                "criteria": items.get(r.id, {}),
                "comment": r.comment,
            }
            for r, u, p in reviews
            if r.status == ReviewStatus.submitted
        ],
        "podium": {
            "version": __version__,
            "exported_at": utcnow().isoformat(),
            "event": {
                "slug": event.slug,
                "description": event.description,
                "is_public": event.is_public,
                "submissions_open_at": _iso(event.submissions_open_at),
                "judging_opened_at": _iso(event.judging_opened_at),
                "judging_closed_at": _iso(event.judging_closed_at),
                "voting_open_at": _iso(event.voting_open_at),
                "voting_close_at": _iso(event.voting_close_at),
                "results_published_at": _iso(event.results_published_at),
                "voting_mode": event.voting_mode.value,
                "quadratic_enabled": event.quadratic_enabled,
                "voting_credits": event.voting_credits,
                "reviews_per_project": event.reviews_per_project,
                "max_team_size": event.max_team_size,
                "normalization_method": event.normalization_method.value,
                "published_ranking": event.published_ranking.value,
            },
            "tracks": [{"id": t.public_id, "description": t.description} for t in tracks],
            "prizes": [
                {
                    "id": p.public_id,
                    "name": p.name,
                    "amount": p.amount_text,
                    "description": p.description,
                    "track": track_public.get(p.track_id),
                    "project": p.project.public_id if p.project else None,
                }
                for p in prizes
            ],
            "rubric": [
                {
                    "key": c.key,
                    "name": c.name,
                    "description": c.description,
                    "weight": c.weight,
                    "min": c.min_score,
                    "max": c.max_score,
                    "archived": c.archived_at is not None,
                }
                for c in criteria
            ],
            "projects": [
                {
                    "id": p.public_id,
                    "description": p.description,
                    "demo_url": p.demo_url,
                    "video_url": p.video_url,
                    "status": p.status.value,
                    "duplicate_of": p.duplicate_of.public_id if p.duplicate_of else None,
                }
                for p in projects
            ],
            "assignments": [
                {
                    "judge": u.public_id,
                    "project": p.public_id,
                    "status": a.status.value,
                    "method": a.method.value,
                }
                for a, u, p in assignments
            ],
            "reviews": [
                {
                    "judge": u.public_id,
                    "project": p.public_id,
                    "status": r.status.value,
                    "submitted_at": _iso(r.submitted_at),
                }
                for r, u, p in reviews
            ],
            # The sections below are exported for the record and are not re-imported: votes
            # carry hashed voter keys (never an address), certificates are signed by this
            # instance's key, and the audit log is append-only by design.
            "votes": [
                {
                    "project": project_public.get(v.project_id),
                    "voter": hashlib.sha256(v.voter_key.encode()).hexdigest()[:24],
                    "user": user_public.get(v.voter_user_id),
                    "credits": v.credits,
                    "flagged": v.flagged,
                    "flag_reason": v.flag_reason,
                    "cast_at": _iso(v.created_at),
                    "voided_at": _iso(v.voided_at),
                }
                for v in votes
            ],
            "comments": [
                {
                    "id": c.public_id,
                    "project": project_public.get(c.project_id),
                    "user": user_public.get(c.user_id),
                    "body": c.body,
                    "created_at": _iso(c.created_at),
                    "hidden_at": _iso(c.hidden_at),
                }
                for c in comments
            ],
            "certificates": [
                {
                    "serial": c.serial,
                    "kind": c.kind.value,
                    "user": user_public.get(c.user_id),
                    "payload": c.payload,
                    "signature": c.signature,
                    "issued_at": _iso(c.issued_at),
                    "revoked_at": _iso(c.revoked_at),
                }
                for c in certificates
            ],
            "audit": [
                {
                    "id": a.id,
                    "at": _iso(a.created_at),
                    "action": a.action,
                    "entity_type": a.entity_type,
                    "entity_id": a.entity_id,
                    "actor": user_public.get(a.actor_id),
                    "meta": a.meta,
                    "prev_hash": a.prev_hash,
                    "row_hash": a.row_hash,
                }
                for a in audit_rows
            ],
        },
    }


@dataclass
class ImportOutcome:
    report: ImportReport
    dry_run: bool
    event_slug: str | None = None
    errors: list[str] = field(default_factory=list)


def validate_shape(data) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["The file must be a JSON object."]
    ev = data.get("event")
    if not isinstance(ev, dict) or not ev.get("id") or not ev.get("name"):
        errors.append("event.id and event.name are required.")
    for key in ("tracks", "judges", "teams", "projects", "scores"):
        if key in data and not isinstance(data[key], list):
            errors.append(f"{key} must be a list.")
    ids: set[str] = set()
    for p in data.get("projects", []) or []:
        if not isinstance(p, dict) or not p.get("id") or not p.get("title") or not p.get("team"):
            errors.append("every project needs id, title and team.")
            break
        if p["id"] in ids:
            errors.append(f"duplicate project id {p['id']}.")
        ids.add(p["id"])
    return errors


def import_event(
    db: DbSession,
    data: dict,
    *,
    dry_run: bool,
    default_password: str,
    actor: User | None = None,
    source_sha256: str = "",
) -> ImportOutcome:
    errors = validate_shape(data)
    if errors:
        return ImportOutcome(report=ImportReport(), dry_run=dry_run, errors=errors)
    event = db.execute(
        select(Event).where(Event.public_id == data["event"]["id"])
    ).scalar_one_or_none()
    foreign = foreign_ids(db, data, event)
    if foreign:
        shown = ", ".join(foreign[:5]) + (", …" if len(foreign) > 5 else "")
        which = (
            "1 id in this file already belongs"
            if len(foreign) == 1
            else (f"{len(foreign)} ids in this file already belong")
        )
        return ImportOutcome(
            report=ImportReport(),
            dry_run=dry_run,
            errors=[
                f"{which} to another event ({shown}); "
                "export that event again or give these rows new ids."
            ],
        )
    if dry_run:
        # SAVEPOINT: run the whole import, then roll it back — the report is what apply would do.
        db.begin_nested()
        try:
            report = import_fixtures(db, data, default_password=default_password, commit=False)
        finally:
            db.rollback()
        return ImportOutcome(report=report, dry_run=True, event_slug=report.event_slug)
    report = import_fixtures(db, data, default_password=default_password, commit=False)
    event = db.execute(select(Event).where(Event.slug == report.event_slug)).scalar_one()
    audit.record(
        db,
        "event.imported",
        "event",
        event.public_id,
        event_id=event.id,
        actor_id=actor.id if actor else None,
        meta={"counts": dict(report.counts), "sha256": source_sha256},
    )
    db.commit()
    return ImportOutcome(report=report, dry_run=False, event_slug=report.event_slug)


def import_for_user(
    db: DbSession,
    data,
    user: User,
    *,
    settings: Settings,
    dry_run: bool,
    source_sha256: str = "",
) -> ImportOutcome:
    """The import rules for one account: update only an event you organize, create one only where
    event creation is allowed to you, and refuse (409) a file that reaches into another event."""
    errors = validate_shape(data)
    if errors:
        raise ValidationFailed("; ".join(errors), errors={"file": "; ".join(errors)})
    existing = db.execute(
        select(Event).where(Event.public_id == data["event"]["id"])
    ).scalar_one_or_none()
    if existing is not None and not is_organizer(db, existing, user):
        raise Forbidden("An event with that id exists and you don't organize it.")
    if existing is None and not (user.is_admin or settings.open_event_creation):
        raise Forbidden("Only instance admins can create events on this Podium.")
    outcome = import_event(
        db,
        data,
        dry_run=dry_run,
        default_password=settings.demo_password,
        actor=user,
        source_sha256=source_sha256,
    )
    if outcome.errors:
        raise Conflict(" ".join(outcome.errors))
    if not dry_run and existing is None:
        event = db.execute(select(Event).where(Event.slug == outcome.event_slug)).scalar_one()
        if not is_organizer(db, event, user):
            db.add(EventRole(event_id=event.id, user_id=user.id, role=Role.organizer))
            audit.record(
                db,
                "organizer.added",
                "user",
                user.public_id,
                event_id=event.id,
                actor_id=user.id,
                meta={"email": user.email, "via": "import"},
            )
            db.commit()
    return outcome
