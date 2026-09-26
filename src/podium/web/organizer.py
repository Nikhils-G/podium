from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session as DbSession

from podium.db import get_db
from podium.errors import PodiumError, ValidationFailed
from podium.models import User
from podium.security.csrf import verify_csrf
from podium.security.deps import EventContext, require_organizer, require_user
from podium.security.ratelimit import ip_hash
from podium.services import dashboard
from podium.services import events as events_service
from podium.services.events import STAGE_LABELS, stage_of
from podium.web.rendering import render

router = APIRouter(include_in_schema=False)

NAV = [
    ("dashboard", "Dashboard", ""),
    ("settings", "Settings", "/settings"),
    ("rubric", "Rubric", "/rubric"),
    ("judges", "Judges", "/judges"),
    ("assignments", "Assignments", "/assignments"),
    ("progress", "Progress", "/progress"),
    ("voting", "Voting", "/voting"),
    ("results", "Results", "/results"),
    ("certificates", "Certificates", "/certificates"),
    ("data", "Import & export", "/data"),
    ("integrations", "Integrations", "/integrations"),
    ("audit", "Audit log", "/audit"),
]


def _console(ctx: EventContext, active: str, **extra):
    stage = stage_of(ctx.event)
    base = f"/e/{ctx.event.slug}/organizer"
    return {
        "event": ctx.event,
        "user": ctx.user,
        "ctx": ctx,
        "stage": stage.value,
        "stage_label": STAGE_LABELS[stage],
        "console": "organizer",
        "nav_items": [(key, label, base + path) for key, label, path in NAV],
        "active": active,
        **extra,
    }


def _event_form_values(event) -> dict:
    def fmt(dt):
        return dt.strftime("%Y-%m-%dT%H:%M") if dt else ""

    return {
        "name": event.name,
        "description": event.description,
        "is_public": event.is_public,
        "max_team_size": event.max_team_size,
        "submissions_open_at": fmt(event.submissions_open_at),
        "submissions_close_at": fmt(event.submissions_close_at),
        "voting_open_at": fmt(event.voting_open_at),
        "voting_close_at": fmt(event.voting_close_at),
    }


@router.get("/events/new")
def new_event_page(request: Request, user: User = Depends(require_user)):
    values = {
        "name": "",
        "description": "",
        "is_public": False,
        "max_team_size": 4,
        "submissions_open_at": "",
        "submissions_close_at": "",
        "voting_open_at": "",
        "voting_close_at": "",
    }
    return render(
        request,
        "organizer/new_event.html",
        title="Create event",
        user=user,
        values=values,
        errors={},
    )


@router.post("/events/new", dependencies=[Depends(verify_csrf)])
async def new_event_submit(
    request: Request, user: User = Depends(require_user), db: DbSession = Depends(get_db)
):
    form = await request.form()
    data = {k: str(v) for k, v in form.items()}
    try:
        event = events_service.create_event(db, user, data, ip_hash=ip_hash(request))
    except ValidationFailed as exc:
        data["is_public"] = data.get("is_public") in ("on", "true", "1")
        return render(
            request,
            "organizer/new_event.html",
            status_code=422,
            title="Create event",
            user=user,
            values=data,
            errors=exc.errors,
        )
    return RedirectResponse(f"/e/{event.slug}/organizer", status_code=303)


@router.get("/e/{slug}/organizer")
def dashboard_page(
    request: Request,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    data = dashboard.overview(db, ctx.event)
    return render(
        request,
        "organizer/dashboard.html",
        title=f"Organizer · {ctx.event.name}",
        **_console(
            ctx,
            "dashboard",
            overview=data,
            steps=dashboard.STEPS,
            step_index=dashboard.step_index(data.stage),
            actions=dashboard.next_actions(ctx.event, data.stage),
        ),
    )


@router.post("/e/{slug}/organizer/actions/{action}", dependencies=[Depends(verify_csrf)])
def lifecycle_action(
    request: Request,
    action: str,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    events_service.apply_action(db, ctx.event, ctx.user, action, ip_hash=ip_hash(request))
    return RedirectResponse(f"/e/{ctx.event.slug}/organizer", status_code=303)


@router.get("/e/{slug}/organizer/settings")
def settings_page(request: Request, ctx: EventContext = Depends(require_organizer)):
    return render(
        request,
        "organizer/settings.html",
        title=f"Settings · {ctx.event.name}",
        **_console(ctx, "settings", values=_event_form_values(ctx.event), errors={}, saved=False),
    )


@router.post("/e/{slug}/organizer/settings", dependencies=[Depends(verify_csrf)])
async def settings_save(
    request: Request,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    form = await request.form()
    data = {k: str(v) for k, v in form.items()}
    try:
        events_service.update_event(db, ctx.event, ctx.user, data, ip_hash=ip_hash(request))
    except ValidationFailed as exc:
        data["is_public"] = data.get("is_public") in ("on", "true", "1")
        return render(
            request,
            "organizer/settings.html",
            status_code=422,
            title="Settings",
            **_console(ctx, "settings", values=data, errors=exc.errors, saved=False),
        )
    return RedirectResponse(f"/e/{ctx.event.slug}/organizer/settings?saved=1", status_code=303)


@router.post("/e/{slug}/organizer/tracks", dependencies=[Depends(verify_csrf)])
def track_add(
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    try:
        events_service.add_track(db, ctx.event, ctx.user, name, description)
    except PodiumError as exc:
        return render(
            request,
            "organizer/settings.html",
            status_code=exc.status_code,
            title="Settings",
            **_console(
                ctx,
                "settings",
                values=_event_form_values(ctx.event),
                errors=getattr(exc, "errors", {"track_name": exc.message}),
                saved=False,
            ),
        )
    return RedirectResponse(f"/e/{ctx.event.slug}/organizer/settings#tracks", status_code=303)


@router.post("/e/{slug}/organizer/tracks/{track_id}/delete", dependencies=[Depends(verify_csrf)])
def track_delete(
    request: Request,
    track_id: str,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    try:
        events_service.remove_track(db, ctx.event, ctx.user, track_id)
    except PodiumError as exc:
        return render(
            request,
            "organizer/settings.html",
            status_code=exc.status_code,
            title="Settings",
            **_console(
                ctx,
                "settings",
                values=_event_form_values(ctx.event),
                errors={"track_name": exc.message},
                saved=False,
            ),
        )
    return RedirectResponse(f"/e/{ctx.event.slug}/organizer/settings#tracks", status_code=303)


@router.post("/e/{slug}/organizer/prizes", dependencies=[Depends(verify_csrf)])
def prize_add(
    request: Request,
    name: str = Form(""),
    amount: str = Form(""),
    description: str = Form(""),
    track: str = Form(""),
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    try:
        events_service.add_prize(db, ctx.event, ctx.user, name, amount, description, track)
    except PodiumError as exc:
        return render(
            request,
            "organizer/settings.html",
            status_code=exc.status_code,
            title="Settings",
            **_console(
                ctx,
                "settings",
                values=_event_form_values(ctx.event),
                errors=getattr(exc, "errors", {"prize_name": exc.message}),
                saved=False,
            ),
        )
    return RedirectResponse(f"/e/{ctx.event.slug}/organizer/settings#prizes", status_code=303)


@router.post("/e/{slug}/organizer/prizes/{prize_id}/delete", dependencies=[Depends(verify_csrf)])
def prize_delete(
    prize_id: str, ctx: EventContext = Depends(require_organizer), db: DbSession = Depends(get_db)
):
    events_service.remove_prize(db, ctx.event, ctx.user, prize_id)
    return RedirectResponse(f"/e/{ctx.event.slug}/organizer/settings#prizes", status_code=303)
