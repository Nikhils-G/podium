from dataclasses import dataclass

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from podium.config import get_settings
from podium.db import get_db
from podium.models import Event, Project, ProjectStatus, User
from podium.security.deps import EventContext, current_user, load_event
from podium.services import navigation, projects
from podium.services import voting as voting_service
from podium.services.events import STAGE_LABELS, list_events_for, stage_of
from podium.services.voting import Voter
from podium.web.community import current_voter
from podium.web.rendering import is_htmx, render

router = APIRouter(include_in_schema=False)


@dataclass
class EventItem:
    event: Event
    stage: str
    label: str


@router.get("/healthz")
def healthz(db: DbSession = Depends(get_db)):
    db.execute(select(1))
    return {"status": "ok"}


@router.get("/")
def home(
    request: Request, db: DbSession = Depends(get_db), user: User | None = Depends(current_user)
):
    items = [
        EventItem(e, stage_of(e).value, STAGE_LABELS[stage_of(e)])
        for e in list_events_for(db, user)
    ]
    mine = navigation.memberships(db, user) if user else []
    grouped = {"organizer": [], "judge": [], "participant": [], "admin": []}
    for m in mine:
        grouped[m.role].append(m)
    settings = get_settings()
    can_create = user is not None and (user.is_admin or settings.open_event_creation)
    return render(
        request,
        "public/home.html",
        events=items,
        user=user,
        title=None,
        mine=mine,
        grouped=grouped,
        can_create=can_create,
    )


@router.get("/e/{slug}")
def event_page(
    request: Request, ctx: EventContext = Depends(load_event), db: DbSession = Depends(get_db)
):
    stage = stage_of(ctx.event)
    count = db.execute(
        select(func.count())
        .select_from(Project)
        .where(Project.event_id == ctx.event.id, Project.status == ProjectStatus.submitted)
    ).scalar_one()
    from podium.security.deps import submissions_are_open
    from podium.services import dashboard
    from podium.services import voting as voting_service

    links = navigation.event_links(db, ctx.user, ctx.event)
    team = project = None
    if ctx.user is not None and links.role == "participant":
        team, project = navigation.participant_state(db, ctx.event, ctx.user)
    return render(
        request,
        "public/event.html",
        event=ctx.event,
        stage=stage.value,
        stage_label=STAGE_LABELS[stage],
        project_count=count,
        user=ctx.user,
        nav="event",
        title=ctx.event.name,
        links=links,
        team=team,
        project=project,
        submissions_open=submissions_are_open(ctx.event),
        voting_open=voting_service.voting_is_open(ctx.event),
        next_step=dashboard.next_step(db, ctx.event) if ctx.is_organizer else None,
    )


@router.get("/e/{slug}/projects")
def gallery(
    request: Request,
    ctx: EventContext = Depends(load_event),
    db: DbSession = Depends(get_db),
    q: str = Query("", max_length=120),
    track: str = Query("", max_length=40),
    sort: str = Query("newest", max_length=20),
    page: int = Query(1, ge=1, le=10_000),
    voter: Voter | None = Depends(current_voter),
):
    data = projects.gallery(
        db, ctx.event, q=q, track=track, sort=sort, page=page, include_hidden=ctx.is_organizer
    )
    ballot = voting_service.voting_is_open(ctx.event)
    voted: set[str] = set()
    if ballot:
        # a ballot is randomized per voter and stable across reloads; sorting is disabled
        key = voter.key if voter else "visitor"
        ordered = voting_service.ballot_order(data.cards, key, ctx.event.id)
        data.cards = ordered
        if voter:
            status = voting_service.voter_status(db, ctx.event, voter)
            ids = {c.public_id for c in data.cards}
            by_id = {
                p.public_id: p.id
                for p in db.execute(
                    select(Project).where(
                        Project.event_id == ctx.event.id, Project.public_id.in_(ids)
                    )
                ).scalars()
            }
            voted = {pid for pid, iid in by_id.items() if status.votes.get(iid)}
    stage = stage_of(ctx.event)
    template = (
        "partials/gallery_grid.html"
        if is_htmx(request) and request.headers.get("HX-Target") == "gallery-results"
        else "public/gallery.html"
    )
    return render(
        request,
        template,
        event=ctx.event,
        page=data,
        stage=stage.value,
        stage_label=STAGE_LABELS[stage],
        user=ctx.user,
        nav="gallery",
        title=f"Projects · {ctx.event.name}",
        ballot=ballot,
        voted=voted,
        voter=voter,
        oob=template != "public/gallery.html",
    )


@router.get("/e/{slug}/embed")
def embed(
    request: Request,
    ctx: EventContext = Depends(load_event),
    db: DbSession = Depends(get_db),
    theme: str = Query("", max_length=10),
    track: str = Query("", max_length=40),
):
    """A compact, frameable gallery for other sites (frame-ancestors is relaxed on this route)."""
    data = projects.gallery(db, ctx.event, track=track, page_size=200)
    return render(
        request,
        "public/embed.html",
        event=ctx.event,
        page=data,
        theme=theme if theme in ("light", "dark") else "",
        title=f"{ctx.event.name} projects",
    )


@router.get("/api/docs")
def api_docs(request: Request, user: User | None = Depends(current_user)):
    """Interactive API reference (vendored Swagger UI — works offline)."""
    return render(request, "public/api_docs.html", title="API reference", user=user)
