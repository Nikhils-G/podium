import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session as DbSession

from podium.errors import Closed, Conflict, Forbidden, NotFound, ValidationFailed
from podium.models import (
    Event,
    Project,
    ProjectStatus,
    Team,
    TeamMember,
    Track,
    User,
    utcnow,
)
from podium.services import audit, webhooks

PAGE_SIZE = 60
URL_RE = re.compile(r"^https?://[^\s]+$")


@dataclass
class ProjectCard:
    id: int
    team_id: int
    public_id: str
    title: str
    summary: str
    team_name: str
    track_name: str | None
    track_position: int | None
    repo_url: str
    demo_url: str
    video_url: str
    status: ProjectStatus
    submitted_at: datetime | None
    is_duplicate: bool


@dataclass
class GalleryPage:
    cards: list[ProjectCard]
    total: int
    page: int
    pages: int
    q: str
    track: str
    sort: str
    tracks: list[Track]


def gallery(
    db: DbSession,
    event: Event,
    *,
    q: str = "",
    track: str = "",
    sort: str = "newest",
    page: int = 1,
    page_size: int = PAGE_SIZE,
    include_hidden: bool = False,
    ballot_key: str | None = None,
) -> GalleryPage:
    """Gallery cards. With `ballot_key` (an open voting window) the whole filtered set is
    shuffled deterministically for that voter *before* paging, so page 1 favours nobody."""
    query = (
        select(Project, Team.name, Track.name, Track.position)
        .join(Team, Team.id == Project.team_id)
        .outerjoin(Track, Track.id == Project.track_id)
        .where(Project.event_id == event.id)
    )
    if include_hidden:
        query = query.where(Project.status != ProjectStatus.draft)
    else:
        query = query.where(Project.status == ProjectStatus.submitted)
    q = q.strip()
    if q:
        needle = f"%{q.lower()}%"
        query = query.where(
            or_(
                func.lower(Project.title).like(needle),
                func.lower(Project.summary).like(needle),
                func.lower(Team.name).like(needle),
            )
        )
    if track:
        query = query.where(Track.public_id == track)
    if ballot_key is not None:
        from podium.services.voting import ballot_order

        sort = "ballot"
        all_rows = ballot_order(db.execute(query.order_by(Project.id)).all(), ballot_key, event.id)
        total = len(all_rows)
        pages = max(1, -(-total // page_size))
        page = min(max(1, page), pages)
        rows = all_rows[(page - 1) * page_size : page * page_size]
    else:
        if sort == "title":
            query = query.order_by(func.lower(Project.title))
        else:
            sort = "newest"
            query = query.order_by(Project.submitted_at.desc().nulls_last(), Project.id.desc())
        total = db.execute(select(func.count()).select_from(query.subquery())).scalar_one()
        pages = max(1, -(-total // page_size))
        page = min(max(1, page), pages)
        rows = db.execute(query.offset((page - 1) * page_size).limit(page_size)).all()
    cards = [
        ProjectCard(
            id=p.id,
            team_id=p.team_id,
            public_id=p.public_id,
            title=p.title,
            summary=p.summary,
            team_name=team_name,
            track_name=track_name,
            track_position=track_position,
            repo_url=p.repo_url,
            demo_url=p.demo_url,
            video_url=p.video_url,
            status=p.status,
            submitted_at=p.submitted_at,
            is_duplicate=p.duplicate_of_id is not None,
        )
        for p, team_name, track_name, track_position in rows
    ]
    tracks = list(
        db.execute(
            select(Track).where(Track.event_id == event.id).order_by(Track.position)
        ).scalars()
    )
    return GalleryPage(cards, total, page, pages, q, track, sort, tracks)


def team_for(db: DbSession, event: Event, user: User) -> Team | None:
    return db.execute(
        select(Team)
        .join(TeamMember, TeamMember.team_id == Team.id)
        .where(Team.event_id == event.id, TeamMember.user_id == user.id)
    ).scalar_one_or_none()


def is_member(db: DbSession, project: Project, user: User | None) -> bool:
    if user is None:
        return False
    return (
        db.execute(
            select(TeamMember.id).where(
                TeamMember.team_id == project.team_id, TeamMember.user_id == user.id
            )
        ).scalar()
        is not None
    )


def get_project(
    db: DbSession, event: Event, public_id: str, viewer: User | None, *, organizer: bool = False
) -> Project:
    """Load a project the viewer may see. Drafts are visible to their team and organizers only."""
    project = db.execute(
        select(Project).where(Project.event_id == event.id, Project.public_id == public_id)
    ).scalar_one_or_none()
    if project is None:
        raise NotFound("No such project.")
    if project.status == ProjectStatus.draft and not (organizer or is_member(db, project, viewer)):
        raise NotFound("No such project.")
    return project


def editable_now(event: Event, project: Project) -> bool:
    from podium.security.deps import submissions_are_open

    if project.status == ProjectStatus.withdrawn:
        return False
    if project.unlocked_until is not None and utcnow() < project.unlocked_until:
        return True
    return submissions_are_open(event)


def update_project(
    db: DbSession,
    event: Event,
    project: Project,
    user: User,
    data: dict,
    *,
    submit: bool | None = None,
    ip_hash: str | None = None,
) -> Project:
    """Edit fields and optionally change draft ↔ submitted. Only while the window is open
    (or the organizer unlocked this project) and only by a member of the team."""
    if not is_member(db, project, user):
        raise Forbidden("Only members of the team can edit this project.")
    if not editable_now(event, project):
        closes = event.submissions_close_at
        when = closes.strftime("%d %b %Y, %H:%M UTC") if closes else ""
        raise Closed(f"Submissions closed{' on ' + when if when else ''}; this project is locked.")
    clean, errors = validate_project(data, event, db)
    if errors:
        raise ValidationFailed(errors=errors)
    for field in ("title", "summary", "description", "repo_url", "demo_url", "video_url"):
        setattr(project, field, clean[field])
    project.track_id = clean["track_id"]
    _flag_duplicate(db, event, project)
    action = "project.updated"
    if submit is True and project.status == ProjectStatus.draft:
        project.status = ProjectStatus.submitted
        project.submitted_at = utcnow()
        action = "project.submitted"
    elif submit is False and project.status == ProjectStatus.submitted:
        project.status = ProjectStatus.draft
        project.submitted_at = None
        action = "project.unsubmitted"
    audit.record(
        db,
        action,
        "project",
        project.public_id,
        event_id=event.id,
        actor_id=user.id,
        ip_hash=ip_hash,
    )
    if action == "project.submitted":
        webhooks.emit(
            db,
            event,
            "project.submitted",
            {"project": project.public_id, "title": project.title, "team": project.team.public_id},
        )
    db.commit()
    return project


def withdraw_project(
    db: DbSession,
    event: Event,
    project: Project,
    user: User,
    *,
    restore: bool = False,
    organizer: bool = False,
    ip_hash: str | None = None,
) -> Project:
    """After the deadline only organizers withdraw or restore (unless the project is unlocked);
    once results are published nobody does, since results are computed from submitted projects."""
    from podium.security.deps import submissions_are_open

    if not (organizer or is_member(db, project, user)):
        raise Forbidden("Only the team or an organizer can withdraw this project.")
    if event.results_published_at is not None:
        raise Conflict(
            "Results are published. Unpublish them before withdrawing or restoring a project."
        )
    unlocked = project.unlocked_until is not None and utcnow() < project.unlocked_until
    if not (organizer or unlocked or submissions_are_open(event)):
        raise Closed(
            "Submissions are closed, so only an organizer can withdraw or restore this project now."
        )
    if restore:
        if project.status != ProjectStatus.withdrawn:
            raise Conflict("This project isn't withdrawn.")
        project.status = ProjectStatus.submitted if project.submitted_at else ProjectStatus.draft
        project.withdrawn_at = None
        action = "project.restored"
    else:
        if project.status == ProjectStatus.withdrawn:
            raise Conflict("This project is already withdrawn.")
        project.status = ProjectStatus.withdrawn
        project.withdrawn_at = utcnow()
        action = "project.withdrawn"
    audit.record(
        db,
        action,
        "project",
        project.public_id,
        event_id=event.id,
        actor_id=user.id,
        ip_hash=ip_hash,
    )
    db.commit()
    return project


def unlock_project(
    db: DbSession, event: Event, project: Project, organizer: User, hours: int
) -> Project:
    from datetime import timedelta

    project.unlocked_until = utcnow() + timedelta(hours=hours) if hours > 0 else None
    audit.record(
        db,
        "project.unlocked",
        "project",
        project.public_id,
        event_id=event.id,
        actor_id=organizer.id,
        meta={"hours": hours},
    )
    db.commit()
    return project


def validate_project(data: dict, event: Event, db: DbSession) -> tuple[dict, dict[str, str]]:
    clean = {
        "title": (data.get("title") or "").strip(),
        "summary": (data.get("summary") or "").strip(),
        "description": (data.get("description") or "").strip(),
        "repo_url": (data.get("repo_url") or "").strip(),
        "demo_url": (data.get("demo_url") or "").strip(),
        "video_url": (data.get("video_url") or "").strip(),
        "track": (data.get("track") or "").strip(),
    }
    errors: dict[str, str] = {}
    if not clean["title"]:
        errors["title"] = "Give your project a title."
    elif len(clean["title"]) > 140:
        errors["title"] = "Keep the title under 140 characters."
    if len(clean["summary"]) > 280:
        errors["summary"] = "Keep the summary under 280 characters."
    for field in ("repo_url", "demo_url", "video_url"):
        if clean[field] and not URL_RE.match(clean[field]):
            errors[field] = "Enter a full URL starting with http:// or https://."
    track_id = None
    if clean["track"]:
        track_id = db.execute(
            select(Track.id).where(Track.event_id == event.id, Track.public_id == clean["track"])
        ).scalar()
        if track_id is None:
            errors["track"] = "Choose one of the event's tracks."
    clean["track_id"] = track_id
    return clean, errors


def create_project(
    db: DbSession, event: Event, user: User, data: dict, *, submit: bool, ip_hash: str | None = None
) -> Project:
    """Create the team's project. Callers must already have checked the submissions window."""
    team = team_for(db, event, user)
    if team is None:
        raise Conflict("Join or create a team before submitting a project.")
    existing = db.execute(
        select(Project.id).where(Project.event_id == event.id, Project.team_id == team.id)
    ).scalar()
    if existing is not None:
        raise Conflict("Your team already has a project. Edit it instead of creating another.")
    clean, errors = validate_project(data, event, db)
    if errors:
        raise ValidationFailed(errors=errors)
    project = Project(
        event_id=event.id,
        team_id=team.id,
        track_id=clean["track_id"],
        title=clean["title"],
        summary=clean["summary"],
        description=clean["description"],
        repo_url=clean["repo_url"],
        demo_url=clean["demo_url"],
        video_url=clean["video_url"],
        status=ProjectStatus.submitted if submit else ProjectStatus.draft,
        submitted_at=utcnow() if submit else None,
    )
    db.add(project)
    db.flush()
    _flag_duplicate(db, event, project)
    audit.record(
        db,
        "project.submitted" if submit else "project.created",
        "project",
        project.public_id,
        event_id=event.id,
        actor_id=user.id,
        ip_hash=ip_hash,
    )
    if submit:
        webhooks.emit(
            db,
            event,
            "project.submitted",
            {"project": project.public_id, "title": project.title, "team": team.public_id},
        )
    db.commit()
    return project


def _flag_duplicate(db: DbSession, event: Event, project: Project) -> None:
    """Same repository URL as an earlier project in this event (any team) → flag, don't block."""
    if not project.repo_url:
        project.duplicate_of_id = None
        return
    earlier = (
        db.execute(
            select(Project)
            .where(
                Project.event_id == event.id,
                Project.id != project.id,
                Project.repo_url == project.repo_url,
                Project.status != ProjectStatus.withdrawn,
            )
            .order_by(Project.id)
        )
        .scalars()
        .first()
    )
    project.duplicate_of_id = earlier.id if earlier else None
