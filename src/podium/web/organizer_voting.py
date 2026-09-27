from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from podium.db import get_db
from podium.errors import PodiumError, ValidationFailed
from podium.models import Vote
from podium.security.csrf import verify_csrf
from podium.security.deps import EventContext, require_organizer
from podium.security.ratelimit import ip_hash
from podium.services import events as events_service
from podium.services import voting as voting_service
from podium.web.organizer import _console, _event_form_values
from podium.web.rendering import render

router = APIRouter(include_in_schema=False)


def _ctx(ctx, db, **extra):
    total, used = voting_service.code_stats(db, ctx.event)
    defaults = {
        "tally": voting_service.tally(db, ctx.event),
        "suspicious": voting_service.suspicious(db, ctx.event),
        "codes_total": total,
        "codes_used": used,
        "open": voting_service.voting_is_open(ctx.event),
        "closed": voting_service.voting_has_closed(ctx.event),
        "errors": {},
        "error": "",
        "new_codes": None,
        "window_values": _event_form_values(ctx.event),
        "settings_locked": voting_service.voting_is_open(ctx.event)
        and db.execute(
            select(func.count()).select_from(Vote).where(Vote.event_id == ctx.event.id)
        ).scalar_one()
        > 0,
    }
    defaults.update(extra)
    return _console(ctx, "voting", **defaults)


def _closes_voting_early(db, event, window: dict) -> int | None:
    """Votes already cast + a close time that is now or in the past → ask before ending the
    window under voters' feet. Returns the vote count when confirmation is needed."""
    from podium.models import Vote
    from podium.models.base import utcnow
    from podium.services.events import parse_utc, tz_offset

    if not voting_service.voting_is_open(event):
        return None
    try:
        close = parse_utc(window.get("voting_close_at"), tz_offset(window))
    except ValueError:
        return None
    if close is None or close > utcnow():
        return None
    cast = db.execute(
        select(func.count()).select_from(Vote).where(Vote.event_id == event.id)
    ).scalar_one()
    return cast or None


@router.post("/e/{slug}/organizer/voting/window", dependencies=[Depends(verify_csrf)])
async def voting_window(
    request: Request,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    """Only the two voting dates are read from this form (round-2 blocker B1)."""
    form = await request.form()
    window = {k: str(form.get(k, "")) for k in ("voting_open_at", "voting_close_at")}
    window["tz_offset_minutes"] = str(form.get("tz_offset_minutes", ""))
    try:
        closing = _closes_voting_early(db, ctx.event, window)
        if closing and form.get("confirm_close_voting") != "on":
            c = _ctx(ctx, db, window_values=window, confirm_close_voting=closing)
            return render(request, "organizer/voting.html", status_code=422, title="Voting", **c)
        events_service.update_voting_window(
            db, ctx.event, ctx.user, window, ip_hash=ip_hash(request)
        )
    except ValidationFailed as exc:
        c = _ctx(ctx, db, errors=exc.errors, window_values=window)
        return render(request, "organizer/voting.html", status_code=422, title="Voting", **c)
    except PodiumError as exc:
        c = _ctx(ctx, db, error=exc.message, window_values=window)
        return render(
            request, "organizer/voting.html", status_code=exc.status_code, title="Voting", **c
        )
    return RedirectResponse(f"/e/{ctx.event.slug}/organizer/voting?saved=window", status_code=303)


@router.get("/e/{slug}/organizer/voting")
def voting_page(
    request: Request,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    return render(request, "organizer/voting.html", title="Voting", **_ctx(ctx, db))


@router.post("/e/{slug}/organizer/voting", dependencies=[Depends(verify_csrf)])
async def voting_save(
    request: Request,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    form = await request.form()
    try:
        events_service.update_voting_settings(
            db, ctx.event, ctx.user, {k: str(v) for k, v in form.items()}, ip_hash=ip_hash(request)
        )
    except ValidationFailed as exc:
        c = _ctx(ctx, db)
        c["errors"] = exc.errors
        return render(request, "organizer/voting.html", status_code=422, title="Voting", **c)
    except PodiumError as exc:
        c = _ctx(ctx, db, error=exc.message)
        return render(
            request, "organizer/voting.html", status_code=exc.status_code, title="Voting", **c
        )
    return RedirectResponse(f"/e/{ctx.event.slug}/organizer/voting?saved=1", status_code=303)


@router.post("/e/{slug}/organizer/voting/codes", dependencies=[Depends(verify_csrf)])
def codes_generate(
    request: Request,
    count: int = Form(10),
    emails: str = Form(""),
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    email_list = [e for e in emails.replace(",", "\n").splitlines() if e.strip()]
    try:
        codes = voting_service.generate_codes(db, ctx.event, ctx.user, count, email_list)
    except PodiumError as exc:
        c = _ctx(ctx, db)
        c["errors"] = getattr(exc, "errors", None) or {"count": exc.message}
        return render(
            request, "organizer/voting.html", status_code=exc.status_code, title="Voting", **c
        )
    return render(
        request, "organizer/voting.html", title="Voting", **_ctx(ctx, db, new_codes=codes)
    )


@router.post("/e/{slug}/organizer/voting/votes/{vote_id}/void", dependencies=[Depends(verify_csrf)])
def void_vote(
    request: Request,
    vote_id: int,
    reason: str = Form(""),
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    try:
        voting_service.void(db, ctx.event, ctx.user, vote_id, reason)
    except PodiumError as exc:
        c = _ctx(ctx, db)
        c["errors"] = getattr(exc, "errors", None) or {"void": exc.message}
        return render(
            request, "organizer/voting.html", status_code=exc.status_code, title="Voting", **c
        )
    return RedirectResponse(f"/e/{ctx.event.slug}/organizer/voting#abuse", status_code=303)
