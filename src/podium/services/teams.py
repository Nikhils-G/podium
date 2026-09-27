"""Teams: one team per person per event, joined by invite link, capped by the event's team size."""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from podium.errors import Closed, Conflict, Forbidden, NotFound, ValidationFailed
from podium.models import (
    Event,
    EventRole,
    MemberRole,
    Project,
    ProjectStatus,
    Role,
    Team,
    TeamMember,
    User,
    new_public_id,
    utcnow,
)
from podium.services import audit
from podium.services.text import utc_text


@dataclass
class TeamView:
    team: Team
    members: list[tuple[User, MemberRole]]
    project: Project | None  # the team's primary project (imported data may hold duplicates)
    projects: list[Project]
    is_lead: bool


def team_for(db: DbSession, event: Event, user: User) -> Team | None:
    return db.execute(
        select(Team)
        .join(TeamMember, TeamMember.team_id == Team.id)
        .where(Team.event_id == event.id, TeamMember.user_id == user.id)
    ).scalar_one_or_none()


def team_view(db: DbSession, team: Team, viewer: User | None) -> TeamView:
    rows = db.execute(
        select(User, TeamMember.role)
        .join(TeamMember, TeamMember.user_id == User.id)
        .where(TeamMember.team_id == team.id)
        .order_by(TeamMember.joined_at)
    ).all()
    projects = list(
        db.execute(
            select(Project)
            .where(Project.team_id == team.id)
            .order_by(
                Project.duplicate_of_id.isnot(None),  # originals before flagged duplicates
                Project.status != ProjectStatus.submitted,  # submitted before drafts/withdrawn
                Project.submitted_at.desc(),
                Project.id.desc(),
            )
        ).scalars()
    )
    is_lead = any(u.id == (viewer.id if viewer else None) and r == MemberRole.lead for u, r in rows)
    return TeamView(
        team=team,
        members=[(u, r) for u, r in rows],
        project=projects[0] if projects else None,
        projects=projects,
        is_lead=is_lead,
    )


def _ensure_participant(db: DbSession, event: Event, user: User) -> None:
    row = db.execute(
        select(EventRole).where(EventRole.event_id == event.id, EventRole.user_id == user.id)
    ).scalar_one_or_none()
    if row is None:
        db.add(EventRole(event_id=event.id, user_id=user.id, role=Role.participant))
        db.flush()
    elif row.role != Role.participant:
        raise Forbidden(f"You're a {row.role} in this event, so you can't also compete in it.")


def teams_are_final(event: Event) -> bool:
    """Teams form until the submission deadline; after it, nobody joins a team whose project
    is already being judged."""
    return event.submissions_close_at is not None and utcnow() >= event.submissions_close_at


def _teams_open(event: Event) -> None:
    if teams_are_final(event):
        raise Closed(
            f"Submissions closed on {utc_text(event.submissions_close_at)}, so teams are final."
        )


def create_team(
    db: DbSession, event: Event, user: User, name: str, *, ip_hash: str | None = None
) -> Team:
    _teams_open(event)
    name = name.strip()
    if not name:
        raise ValidationFailed(errors={"name": "Give your team a name."})
    if len(name) > 120:
        raise ValidationFailed(errors={"name": "Keep the team name under 120 characters."})
    if team_for(db, event, user) is not None:
        raise Conflict("You're already in a team for this event.")
    _ensure_participant(db, event, user)
    team = Team(
        event_id=event.id, name=name, invite_code=new_public_id("join", 10), created_by=user.id
    )
    db.add(team)
    db.flush()
    db.add(TeamMember(team_id=team.id, user_id=user.id, role=MemberRole.lead))
    audit.record(
        db,
        "team.created",
        "team",
        team.public_id,
        event_id=event.id,
        actor_id=user.id,
        meta={"name": name},
        ip_hash=ip_hash,
    )
    db.commit()
    return team


def team_by_invite(db: DbSession, code: str) -> Team:
    team = db.execute(select(Team).where(Team.invite_code == code)).scalar_one_or_none()
    if team is None:
        raise NotFound(
            "That join link doesn't work any more. It was replaced or the team no longer exists. "
            "Ask a teammate for the current link."
        )
    return team


def join_team(db: DbSession, team: Team, user: User, *, ip_hash: str | None = None) -> Team:
    event = db.get(Event, team.event_id)
    if event.archived_at is not None:
        raise Conflict("This event is archived.")
    existing = team_for(db, event, user)
    if existing is not None and existing.id == team.id:
        return team  # opening your own team's link again is fine, even after the deadline
    _teams_open(event)
    if existing is not None:
        raise Conflict(f"You're already in the team “{existing.name}” for this event.")
    _ensure_participant(db, event, user)
    size = db.execute(
        select(func.count()).select_from(TeamMember).where(TeamMember.team_id == team.id)
    ).scalar_one()
    if size >= event.max_team_size:
        raise Conflict(f"This team is full (max {event.max_team_size} members).")
    db.add(TeamMember(team_id=team.id, user_id=user.id, role=MemberRole.member))
    audit.record(
        db,
        "team.joined",
        "team",
        team.public_id,
        event_id=event.id,
        actor_id=user.id,
        ip_hash=ip_hash,
    )
    db.commit()
    return team


def leave_team(db: DbSession, team: Team, user: User, *, ip_hash: str | None = None) -> None:
    member = db.execute(
        select(TeamMember).where(TeamMember.team_id == team.id, TeamMember.user_id == user.id)
    ).scalar_one_or_none()
    if member is None:
        raise NotFound("You're not in this team.")
    # imported data can give a team several projects (duplicates), so check them all
    projects = db.execute(select(Project).where(Project.team_id == team.id)).scalars().all()
    if any(p.submitted_at is not None for p in projects):
        raise Conflict("You can't leave a team that has submitted a project.")
    others = (
        db.execute(
            select(TeamMember)
            .where(TeamMember.team_id == team.id, TeamMember.user_id != user.id)
            .order_by(TeamMember.joined_at)
        )
        .scalars()
        .all()
    )
    if member.role == MemberRole.lead and others:
        others[0].role = MemberRole.lead
    db.delete(member)
    if not others:
        for project in projects:
            db.delete(project)
        db.delete(team)
    audit.record(
        db,
        "team.left",
        "team",
        team.public_id,
        event_id=team.event_id,
        actor_id=user.id,
        ip_hash=ip_hash,
    )
    db.commit()


def regenerate_invite(
    db: DbSession, event: Event, user: User, *, ip_hash: str | None = None
) -> Team:
    """A new join link for the caller's team; the old one stops working at once."""
    team = team_for(db, event, user)
    if team is None:
        raise NotFound("You're not in a team for this event.")
    _teams_open(event)
    team.invite_code = new_public_id("join", 10)
    audit.record(
        db,
        "team.invite_regenerated",
        "team",
        team.public_id,
        event_id=event.id,
        actor_id=user.id,
        ip_hash=ip_hash,
    )
    db.commit()
    return team


def rename_team(db: DbSession, team: Team, user: User, name: str) -> Team:
    name = name.strip()
    if not name or len(name) > 120:
        raise ValidationFailed(errors={"name": "Team names are 1–120 characters."})
    team.name = name
    audit.record(
        db,
        "team.renamed",
        "team",
        team.public_id,
        event_id=team.event_id,
        actor_id=user.id,
        meta={"name": name},
    )
    db.commit()
    return team
