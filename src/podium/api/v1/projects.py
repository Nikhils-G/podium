from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session as DbSession

from podium.db import get_db
from podium.models import User
from podium.schemas.projects import ProjectCreate, ProjectOut, ProjectUpdate
from podium.schemas.responses import GalleryOut
from podium.security.deps import EventContext, load_event, require_submissions_open, require_user
from podium.security.ratelimit import ip_hash
from podium.services import projects

router = APIRouter(tags=["projects"])


@router.get("/events/{slug}/projects", response_model=GalleryOut)
def list_projects(
    response: Response,
    ctx: EventContext = Depends(load_event),
    db: DbSession = Depends(get_db),
    q: str = Query("", max_length=120),
    track: str = Query("", max_length=40),
    sort: str = Query("newest", max_length=20),
    page: int = Query(1, ge=1),
):
    """Public gallery data for an event: submitted projects with search, track filter and paging."""
    response.headers["Access-Control-Allow-Origin"] = "*"  # public, read-only: safe for widgets
    data = projects.gallery(db, ctx.event, q=q, track=track, sort=sort, page=page)
    return {
        "event": ctx.event.slug,
        "total": data.total,
        "page": data.page,
        "pages": data.pages,
        "projects": [
            ProjectOut(
                id=c.public_id,
                title=c.title,
                summary=c.summary,
                team=c.team_name,
                track=c.track_name,
                repo_url=c.repo_url,
                demo_url=c.demo_url,
                video_url=c.video_url,
                status=c.status.value,
                submitted_at=c.submitted_at,
            ).model_dump()
            for c in data.cards
        ],
    }


@router.post("/events/{slug}/projects", status_code=201)
def create_project(
    request: Request,
    body: ProjectCreate,
    ctx: EventContext = Depends(require_submissions_open),
    db: DbSession = Depends(get_db),
):
    """Submit (or draft) the caller's team project. Refused with 403 once submissions close —
    the window check runs before the body is parsed, so a bad body never turns into a 422."""
    project = projects.create_project(
        db, ctx.event, ctx.user, body.model_dump(), submit=body.submit, ip_hash=ip_hash(request)
    )
    return {
        "project": ProjectOut(
            id=project.public_id,
            title=project.title,
            summary=project.summary,
            team=project.team.name,
            track=project.track.name if project.track else None,
            repo_url=project.repo_url,
            demo_url=project.demo_url,
            video_url=project.video_url,
            status=project.status.value,
            submitted_at=project.submitted_at,
        ).model_dump()
    }


def _out(project) -> dict:
    return ProjectOut(
        id=project.public_id,
        title=project.title,
        summary=project.summary,
        team=project.team.name,
        track=project.track.name if project.track else None,
        repo_url=project.repo_url,
        demo_url=project.demo_url,
        video_url=project.video_url,
        status=project.status.value,
        submitted_at=project.submitted_at,
    ).model_dump() | {"description": project.description}


@router.get("/events/{slug}/projects/{pid}")
def get_project(pid: str, ctx: EventContext = Depends(load_event), db: DbSession = Depends(get_db)):
    """One project. Drafts are visible only to their team and to organizers."""
    project = projects.get_project(db, ctx.event, pid, ctx.user, organizer=ctx.is_organizer)
    return {"project": _out(project)}


@router.patch("/events/{slug}/projects/{pid}")
def update_project(
    request: Request,
    pid: str,
    body: ProjectUpdate,
    ctx: EventContext = Depends(load_event),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    """Edit the team's project. Refused with 403 once the window closes (unless unlocked)."""
    project = projects.get_project(db, ctx.event, pid, user, organizer=ctx.is_organizer)
    data = body.model_dump(exclude_unset=True)
    merged = {
        k: data.get(
            k,
            getattr(project, k)
            if k != "track"
            else (project.track.public_id if project.track else ""),
        )
        for k in ("title", "summary", "description", "repo_url", "demo_url", "video_url", "track")
    }
    project = projects.update_project(
        db, ctx.event, project, user, merged, submit=data.get("submit"), ip_hash=ip_hash(request)
    )
    return {"project": _out(project)}


@router.post("/events/{slug}/projects/{pid}/withdraw")
def withdraw_project(
    request: Request,
    pid: str,
    ctx: EventContext = Depends(load_event),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    """Withdraw a project from the gallery and from judging (it can be restored)."""
    project = projects.get_project(db, ctx.event, pid, user, organizer=ctx.is_organizer)
    project = projects.withdraw_project(
        db, ctx.event, project, user, organizer=ctx.is_organizer, ip_hash=ip_hash(request)
    )
    return {"project": _out(project)}


@router.post("/events/{slug}/projects/{pid}/restore")
def restore_project(
    request: Request,
    pid: str,
    ctx: EventContext = Depends(load_event),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    """Put a withdrawn project back."""
    project = projects.get_project(db, ctx.event, pid, user, organizer=ctx.is_organizer)
    project = projects.withdraw_project(
        db,
        ctx.event,
        project,
        user,
        restore=True,
        organizer=ctx.is_organizer,
        ip_hash=ip_hash(request),
    )
    return {"project": _out(project)}
