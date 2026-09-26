"""CSV exports for every stage. Each generator yields rows; the API streams them."""

import csv
import io
from collections.abc import Iterable, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from podium.models import (
    Assignment,
    AuditLog,
    Event,
    Project,
    Review,
    ScoreItem,
    Team,
    TeamMember,
    Track,
    User,
)
from podium.services import scoring

NAMES = ("projects", "teams", "assignments", "reviews", "scores", "audit")


def _csv(rows: Iterable[Iterable]) -> Iterator[str]:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for row in rows:
        writer.writerow(row)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.2f}"
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def projects(db: DbSession, event: Event) -> Iterator[str]:
    rows = db.execute(
        select(Project, Team.name, Track.name)
        .join(Team, Team.id == Project.team_id)
        .outerjoin(Track, Track.id == Project.track_id)
        .where(Project.event_id == event.id)
        .order_by(Project.id)
    ).all()
    yield from _csv(
        [
            [
                "project_id",
                "title",
                "team",
                "track",
                "status",
                "submitted_at",
                "repo_url",
                "demo_url",
                "video_url",
                "duplicate_of",
            ]
        ]
        + [
            [
                p.public_id,
                p.title,
                team,
                track,
                p.status.value,
                _fmt(p.submitted_at),
                p.repo_url,
                p.demo_url,
                p.video_url,
                p.duplicate_of.public_id if p.duplicate_of else "",
            ]
            for p, team, track in rows
        ]
    )


def teams(db: DbSession, event: Event) -> Iterator[str]:
    rows = db.execute(
        select(Team, User.email, User.name, TeamMember.role)
        .join(TeamMember, TeamMember.team_id == Team.id)
        .join(User, User.id == TeamMember.user_id)
        .where(Team.event_id == event.id)
        .order_by(Team.id, TeamMember.id)
    ).all()
    yield from _csv(
        [["team_id", "team", "member_email", "member_name", "member_role"]]
        + [[t.public_id, t.name, email, name, role.value] for t, email, name, role in rows]
    )


def assignments(db: DbSession, event: Event) -> Iterator[str]:
    rows = db.execute(
        select(Assignment, User, Project)
        .join(User, User.id == Assignment.judge_id)
        .join(Project, Project.id == Assignment.project_id)
        .where(Assignment.event_id == event.id)
        .order_by(Assignment.id)
    ).all()
    yield from _csv(
        [["assignment_id", "judge_id", "judge", "project_id", "project", "status", "method"]]
        + [
            [a.id, u.public_id, u.name, p.public_id, p.title, a.status.value, a.method.value]
            for a, u, p in rows
        ]
    )


def reviews(db: DbSession, event: Event) -> Iterator[str]:
    """One row per submitted review with the raw value of every criterion."""
    criteria = scoring.compute(db, event).criteria
    rows = db.execute(
        select(Review, User, Project)
        .join(User, User.id == Review.judge_id)
        .join(Project, Project.id == Review.project_id)
        .where(Review.event_id == event.id)
        .order_by(Review.id)
    ).all()
    values: dict[int, dict[int, int]] = {}
    for item in db.execute(
        select(ScoreItem)
        .join(Review, Review.id == ScoreItem.review_id)
        .where(Review.event_id == event.id)
    ).scalars():
        values.setdefault(item.review_id, {})[item.criterion_id] = item.value
    header = ["review_id", "judge_id", "judge", "project_id", "project", "status", "submitted_at"]
    header += [c.key for c in criteria] + ["raw_score", "comment"]
    body = []
    for r, u, p in rows:
        vals = values.get(r.id, {})
        body.append(
            [
                r.public_id,
                u.public_id,
                u.name,
                p.public_id,
                p.title,
                r.status.value,
                _fmt(r.submitted_at),
            ]
            + [vals.get(c.id, "") for c in criteria]
            + [_fmt(scoring.raw_score(vals, criteria)), r.comment]
        )
    yield from _csv([header] + body)


def scores(db: DbSession, event: Event) -> Iterator[str]:
    """Aggregated per project: raw mean, normalized score, ranks, disagreement."""
    results = scoring.compute(db, event, include_withdrawn=True)
    yield from _csv(
        [
            [
                "rank",
                "project_id",
                "title",
                "track",
                "status",
                "reviews",
                "raw_mean",
                "normalized",
                "rank_raw",
                "rank_normalized",
                "disagreement",
            ]
        ]
        + [
            [
                p.rank_norm if results.basis.value == "normalized" else p.rank_raw,
                p.project.public_id,
                p.project.title,
                p.track.name if p.track else "",
                p.project.status.value,
                p.n,
                _fmt(p.raw_mean),
                _fmt(p.normalized),
                p.rank_raw or "",
                p.rank_norm or "",
                _fmt(p.disagreement),
            ]
            for p in results.projects
        ]
    )


def audit(db: DbSession, event: Event) -> Iterator[str]:
    rows = (
        db.execute(select(AuditLog).where(AuditLog.event_id == event.id).order_by(AuditLog.id))
        .scalars()
        .all()
    )
    yield from _csv(
        [["id", "at", "actor_id", "action", "entity_type", "entity_id", "meta", "row_hash"]]
        + [
            [
                a.id,
                _fmt(a.created_at),
                a.actor_id or "",
                a.action,
                a.entity_type,
                a.entity_id,
                str(a.meta),
                a.row_hash,
            ]
            for a in rows
        ]
    )


GENERATORS = {
    "projects": projects,
    "teams": teams,
    "assignments": assignments,
    "reviews": reviews,
    "scores": scores,
    "audit": audit,
}
