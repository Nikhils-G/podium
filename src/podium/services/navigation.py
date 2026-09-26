"""Where a person goes next. Memberships across events, the landing page after sign-in, and the
role links the header shows for the current event."""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from podium.models import (
    Assignment,
    AssignmentStatus,
    Event,
    EventRole,
    Project,
    ProjectStatus,
    Role,
    Team,
    TeamMember,
    User,
)
from podium.services.events import STAGE_LABELS, Stage, stage_of


@dataclass
class Membership:
    event: Event
    role: str  # organizer | judge | participant | admin
    stage: Stage
    stage_label: str
    next_label: str
    next_href: str
    done: int = 0
    total: int = 0

    @property
    def role_label(self) -> str:
        return {
            "organizer": "You organize",
            "judge": "You judge",
            "participant": "You take part",
            "admin": "Admin",
        }[self.role]


def judge_progress(db: DbSession, event: Event, user: User) -> tuple[int, int]:
    rows = db.execute(
        select(Assignment.status, func.count(Assignment.id))
        .where(Assignment.event_id == event.id, Assignment.judge_id == user.id)
        .group_by(Assignment.status)
    ).all()
    total = sum(n for _, n in rows)
    done = sum(n for s, n in rows if s == AssignmentStatus.done)
    return done, total


def participant_state(
    db: DbSession, event: Event, user: User
) -> tuple[Team | None, Project | None]:
    team = db.execute(
        select(Team)
        .join(TeamMember, TeamMember.team_id == Team.id)
        .where(Team.event_id == event.id, TeamMember.user_id == user.id)
    ).scalar_one_or_none()
    project = None
    if team is not None:
        project = (
            db.execute(
                select(Project)
                .where(Project.team_id == team.id)
                .order_by(Project.duplicate_of_id.isnot(None), Project.id.desc())
            )
            .scalars()
            .first()
        )
    return team, project


def _judge_next(db, event, user) -> tuple[str, str, int, int]:
    done, total = judge_progress(db, event, user)
    base = f"/e/{event.slug}/judge"
    if total == 0:
        return "No assignments yet", base, 0, 0
    if done < total:
        return f"Continue judging · {total - done} left", base, done, total
    return "All reviews submitted · compare or view records", f"{base}/compare", done, total


def _participant_next(db, event, user, stage: Stage) -> tuple[str, str]:
    from podium.security.deps import submissions_are_open

    team, project = participant_state(db, event, user)
    base = f"/e/{event.slug}"
    if stage == Stage.published:
        return "Results are out", f"{base}/results"
    if team is None:
        if submissions_are_open(event):
            return "Create or join a team", f"{base}/team"
        return "Submissions are closed", base
    if project is None:
        if submissions_are_open(event):
            return "Submit your project", f"{base}/submit"
        return "Submissions closed before you submitted", f"{base}/team"
    if project.status == ProjectStatus.draft:
        return (
            "Finish and submit your draft"
            if submissions_are_open(event)
            else "Draft was never submitted"
        ), f"{base}/submit"
    if project.status == ProjectStatus.withdrawn:
        return "Your project is withdrawn", f"{base}/projects/{project.public_id}"
    if submissions_are_open(event):
        return "Submitted · edit until the deadline", f"{base}/projects/{project.public_id}"
    return "Submitted · awaiting results", f"{base}/projects/{project.public_id}"


def _organizer_next(db, event) -> tuple[str, str]:
    from podium.services.dashboard import next_step

    step = next_step(db, event)
    return step.label, f"/e/{event.slug}/organizer"


def memberships(db: DbSession, user: User | None) -> list[Membership]:
    if user is None:
        return []
    out: list[Membership] = []
    seen: set[int] = set()
    rows = db.execute(
        select(EventRole, Event)
        .join(Event, Event.id == EventRole.event_id)
        .where(EventRole.user_id == user.id)
        .order_by(Event.created_at.desc())
    ).all()
    for role_row, event in rows:
        seen.add(event.id)
        stage = stage_of(event)
        done = total = 0
        if role_row.role == Role.organizer:
            label, href = _organizer_next(db, event)
        elif role_row.role == Role.judge:
            label, href, done, total = _judge_next(db, event, user)
        else:
            label, href = _participant_next(db, event, user, stage)
        out.append(
            Membership(
                event=event,
                role=role_row.role.value,
                stage=stage,
                stage_label=STAGE_LABELS[stage],
                next_label=label,
                next_href=href,
                done=done,
                total=total,
            )
        )
    if user.is_admin:
        for event in db.execute(select(Event).order_by(Event.created_at.desc())).scalars():
            if event.id in seen:
                continue
            stage = stage_of(event)
            out.append(
                Membership(
                    event=event,
                    role="admin",
                    stage=stage,
                    stage_label=STAGE_LABELS[stage],
                    next_label="Manage as admin",
                    next_href=f"/e/{event.slug}/organizer",
                )
            )
    return out


def landing_for(db: DbSession, user: User) -> str:
    """One membership → straight to its console. Several (or none) → the personal home page."""
    mine = [m for m in memberships(db, user) if m.role != "admin"]
    if len(mine) == 1:
        m = mine[0]
        if m.role == "organizer":
            return f"/e/{m.event.slug}/organizer"
        if m.role == "judge":
            return f"/e/{m.event.slug}/judge"
        return f"/e/{m.event.slug}"
    return "/"


@dataclass
class EventLinks:
    role: str | None
    is_organizer: bool
    judge_done: int = 0
    judge_total: int = 0
    has_team: bool = False
    has_project: bool = False


def event_links(db: DbSession, user: User | None, event: Event) -> EventLinks:
    if user is None:
        return EventLinks(role=None, is_organizer=False)
    role = db.execute(
        select(EventRole.role).where(EventRole.event_id == event.id, EventRole.user_id == user.id)
    ).scalar_one_or_none()
    links = EventLinks(
        role=role.value if role else None, is_organizer=user.is_admin or role == Role.organizer
    )
    if role == Role.judge:
        links.judge_done, links.judge_total = judge_progress(db, event, user)
    if role == Role.participant:
        team, project = participant_state(db, event, user)
        links.has_team, links.has_project = team is not None, project is not None
    return links


def header_context(db: DbSession, user: User | None, event: Event | None) -> dict:
    return {
        "memberships": memberships(db, user),
        "links": event_links(db, user, event) if event is not None else None,
    }
