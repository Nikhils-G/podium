from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session as DbSession

from podium.db import get_db
from podium.errors import PodiumError, ValidationFailed
from podium.models import User
from podium.security.csrf import verify_csrf
from podium.security.deps import (
    EventContext,
    require_can_create_event,
    require_organizer,
    submissions_are_open,
)
from podium.security.ratelimit import ip_hash
from podium.services import dashboard
from podium.services import events as events_service
from podium.services.events import STAGE_LABELS, stage_of, utcnow
from podium.web.rendering import is_htmx, render

router = APIRouter(include_in_schema=False)

NAV_GROUPS = [
    ("", [("dashboard", "Dashboard", "")]),
    (
        "Set up",
        [
            ("settings", "Settings", "/settings"),
            ("rubric", "Rubric", "/rubric"),
            ("judges", "Judges", "/judges"),
        ],
    ),
    (
        "Run",
        [
            ("assignments", "Assignments", "/assignments"),
            ("progress", "Progress", "/progress"),
            ("voting", "Voting", "/voting"),
        ],
    ),
    (
        "Wrap up",
        [("results", "Results", "/results"), ("certificates", "Certificates", "/certificates")],
    ),
    (
        "Tools",
        [
            ("data", "Import & export", "/data"),
            ("integrations", "Integrations", "/integrations"),
            ("audit", "Audit log", "/audit"),
        ],
    ),
]
NAV = [item for _, items in NAV_GROUPS for item in items]


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
        "archived": ctx.event.archived_at is not None,
        "nav_items": [(key, label, base + path) for key, label, path in NAV],
        "nav_groups": [
            (group, [(key, label, base + path) for key, label, path in items])
            for group, items in NAV_GROUPS
        ],
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
def new_event_page(request: Request, user: User = Depends(require_can_create_event)):
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
    request: Request,
    user: User = Depends(require_can_create_event),
    db: DbSession = Depends(get_db),
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
    return RedirectResponse(f"/e/{event.slug}/organizer?saved=created", status_code=303)


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
            steps=dashboard.steps_for(ctx.event),
            step_index=dashboard.step_index(ctx.event),
            next_step=dashboard.next_step(db, ctx.event),
            more_actions=dashboard.more_actions(ctx.event),
            timeline=dashboard.timeline(db, ctx.event),
        ),
    )


@router.post("/e/{slug}/organizer/dates", dependencies=[Depends(verify_csrf)])
def shift_date(
    request: Request,
    field: str = Form(""),
    preset: str = Form(""),
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    events_service.shift_date(db, ctx.event, ctx.user, field, preset, ip_hash=ip_hash(request))
    return RedirectResponse(
        f"/e/{ctx.event.slug}/organizer?saved=dates&field={field}", status_code=303
    )


@router.post("/e/{slug}/organizer/actions/{action}", dependencies=[Depends(verify_csrf)])
def lifecycle_action(
    request: Request,
    action: str,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    events_service.apply_action(db, ctx.event, ctx.user, action, ip_hash=ip_hash(request))
    return RedirectResponse(f"/e/{ctx.event.slug}/organizer?saved={action}", status_code=303)


@router.get("/e/{slug}/organizer/settings")
def settings_page(
    request: Request,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    return render(
        request,
        "organizer/settings.html",
        title=f"Settings · {ctx.event.name}",
        **_console(
            ctx,
            "settings",
            values=_event_form_values(ctx.event),
            errors={},
            saved=False,
            organizers=events_service.organizers(db, ctx.event),
        ),
    )


@router.post("/e/{slug}/organizer/organizers", dependencies=[Depends(verify_csrf)])
def organizer_add(
    request: Request,
    email: str = Form(""),
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    try:
        events_service.add_organizer(db, ctx.event, ctx.user, email)
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
                errors={"organizer_email": exc.message},
                saved=False,
                organizers=events_service.organizers(db, ctx.event),
            ),
        )
    return RedirectResponse(
        f"/e/{ctx.event.slug}/organizer/settings?saved=organizer#organizers", status_code=303
    )


@router.post("/e/{slug}/organizer/organizers/{user_id}/remove", dependencies=[Depends(verify_csrf)])
def organizer_remove(
    request: Request,
    user_id: str,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    try:
        events_service.remove_organizer(db, ctx.event, ctx.user, user_id)
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
                errors={"organizer_email": exc.message},
                saved=False,
                organizers=events_service.organizers(db, ctx.event),
            ),
        )
    return RedirectResponse(
        f"/e/{ctx.event.slug}/organizer/settings?saved=organizer#organizers", status_code=303
    )


@router.post("/e/{slug}/organizer/settings", dependencies=[Depends(verify_csrf)])
async def settings_save(
    request: Request,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    form = await request.form()
    data = {k: str(v) for k, v in form.items()}
    clean, _ = events_service.validate_event(data)
    new_close = clean.get("submissions_close_at")
    now = utcnow()
    closing_now = (
        new_close is not None
        and new_close <= now
        and submissions_are_open(ctx.event)
        and data.get("confirm_close") != "on"
    )
    reopening = (
        new_close is not None
        and new_close > now
        and not submissions_are_open(ctx.event)
        and ctx.event.judging_opened_at is not None
        and data.get("confirm_reopen") != "on"
    )
    if closing_now or reopening:
        data["is_public"] = data.get("is_public") in ("on", "true", "1")
        return render(
            request,
            "organizer/settings.html",
            status_code=422,
            title="Settings",
            **_console(
                ctx,
                "settings",
                values=data,
                errors={},
                saved=False,
                organizers=events_service.organizers(db, ctx.event),
                confirm_close=dashboard.overview(db, ctx.event).teams if closing_now else None,
                confirm_reopen=reopening,
            ),
        )
    try:
        events_service.update_event(db, ctx.event, ctx.user, data, ip_hash=ip_hash(request))
    except (ValidationFailed, PodiumError) as exc:
        data["is_public"] = data.get("is_public") in ("on", "true", "1")
        return render(
            request,
            "organizer/settings.html",
            status_code=exc.status_code,
            title="Settings",
            **_console(
                ctx,
                "settings",
                values=data,
                errors=getattr(exc, "errors", None) or {},
                error=getattr(exc, "errors", None) and "" or exc.message,
                saved=False,
                organizers=events_service.organizers(db, ctx.event),
            ),
        )
    return RedirectResponse(f"/e/{ctx.event.slug}/organizer/settings?saved=1", status_code=303)


def _section(request, ctx, db, section: str, errors: dict, status_code: int = 200):
    """Tracks/prizes forms swap their own section over htmx; the page reloads without JS."""
    if not is_htmx(request):
        return None
    db.expire(ctx.event, ["tracks", "prizes"])
    return render(
        request,
        f"partials/settings_{section}.html",
        status_code=status_code,
        event=ctx.event,
        errors=errors,
        oob_prizes=section == "tracks",  # the prize form lists the tracks too
    )


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
        errors = getattr(exc, "errors", None) or {"track_name": exc.message}
        if partial := _section(request, ctx, db, "tracks", errors, exc.status_code):
            return partial
        return render(
            request,
            "organizer/settings.html",
            status_code=exc.status_code,
            title="Settings",
            **_console(
                ctx,
                "settings",
                values=_event_form_values(ctx.event),
                errors=errors,
                saved=False,
            ),
        )
    if partial := _section(request, ctx, db, "tracks", {}):
        return partial
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
        if partial := _section(
            request, ctx, db, "tracks", {"track_name": exc.message}, exc.status_code
        ):
            return partial
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
    if partial := _section(request, ctx, db, "tracks", {}):
        return partial
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
        errors = getattr(exc, "errors", None) or {"prize_name": exc.message}
        if partial := _section(request, ctx, db, "prizes", errors, exc.status_code):
            return partial
        return render(
            request,
            "organizer/settings.html",
            status_code=exc.status_code,
            title="Settings",
            **_console(
                ctx,
                "settings",
                values=_event_form_values(ctx.event),
                errors=errors,
                saved=False,
            ),
        )
    if partial := _section(request, ctx, db, "prizes", {}):
        return partial
    return RedirectResponse(f"/e/{ctx.event.slug}/organizer/settings#prizes", status_code=303)


@router.post("/e/{slug}/organizer/prizes/{prize_id}/delete", dependencies=[Depends(verify_csrf)])
def prize_delete(
    request: Request,
    prize_id: str,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    events_service.remove_prize(db, ctx.event, ctx.user, prize_id)
    if partial := _section(request, ctx, db, "prizes", {}):
        return partial
    return RedirectResponse(f"/e/{ctx.event.slug}/organizer/settings#prizes", status_code=303)
