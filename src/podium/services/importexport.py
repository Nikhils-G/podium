"""Whole-event export and import in the DOGFOOD fixtures shape, extended with a `podium` block
that carries everything the base shape can't (rubric, prizes, dates, project fields, assignments).
The seeder is this importer, so a fixtures.json and an export.json load the same way."""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from podium import __version__
from podium.models import (
    Assignment,
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
    utcnow,
)
from podium.seed.fixtures import ImportReport, import_fixtures


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
    db: DbSession, data: dict, *, dry_run: bool, default_password: str
) -> ImportOutcome:
    errors = validate_shape(data)
    if errors:
        return ImportOutcome(report=ImportReport(), dry_run=dry_run, errors=errors)
    if dry_run:
        # SAVEPOINT: run the whole import, then roll it back — the report is what apply would do.
        db.begin_nested()
        try:
            report = import_fixtures(db, data, default_password=default_password, commit=False)
        finally:
            db.rollback()
        return ImportOutcome(report=report, dry_run=True, event_slug=report.event_slug)
    report = import_fixtures(db, data, default_password=default_password, commit=True)
    return ImportOutcome(report=report, dry_run=False, event_slug=report.event_slug)
