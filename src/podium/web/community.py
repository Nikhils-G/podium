"""Voting, comments and the public results page."""

import hashlib
import hmac
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session as DbSession

from podium.config import Settings, get_settings
from podium.db import get_db
from podium.errors import NotFound, PodiumError
from podium.models import User
from podium.security.csrf import verify_csrf
from podium.security.deps import EventContext, load_event, require_organizer, require_user
from podium.security.ratelimit import ip_hash, limiter
from podium.services import comments as comments_service
from podium.services import events as events_service
from podium.services import projects as projects_service
from podium.services import scoring
from podium.services import voting as voting_service
from podium.services.events import STAGE_LABELS, stage_of
from podium.services.voting import Voter
from podium.web.rendering import is_htmx, render

router = APIRouter(include_in_schema=False)
VOTER_COOKIE = "voter"


def code_cookie_name(event) -> str:
    return f"vcode_{event.public_id}"


def _sign(secret: str, value: str) -> str:
    return hmac.new(secret.encode(), value.encode(), "sha256").hexdigest()[:24]


def current_voter(
    request: Request,
    ctx: EventContext = Depends(load_event),
    settings: Settings = Depends(get_settings),
) -> Voter | None:
    anon = voting_service.parse_voter_cookie(settings.secret_key, request.cookies.get(VOTER_COOKIE))
    code_key = None
    raw = request.cookies.get(code_cookie_name(ctx.event))
    if raw and "." in raw:
        value, sig = raw.rsplit(".", 1)
        if hmac.compare_digest(_sign(settings.secret_key, value), sig):
            code_key = value
    return voting_service.resolve_voter(ctx.event, ctx.user, anon, code_key)


def set_voter_cookie(response, value: str, settings: Settings) -> None:
    """The signed anonymous id: the ballot order key for visitors and, in link mode, the voter."""
    response.set_cookie(
        VOTER_COOKIE,
        value,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
        max_age=60 * 60 * 24 * 90,
        path="/",
    )


def ua_hash(request: Request) -> str:
    return hashlib.sha256(request.headers.get("user-agent", "").encode()).hexdigest()[:32]


def vote_context(db, ctx: EventContext, project, voter: Voter | None) -> dict:
    event = ctx.event
    status = voting_service.voter_status(db, event, voter)
    mine = status.votes.get(project.id, 0)
    visible = voting_service.tallies_visible(event, organizer=ctx.is_organizer)
    count = None
    if visible:
        count = sum(
            t.votes for t in voting_service.tally(db, event).projects if t.project.id == project.id
        )
    return {
        "event": event,
        "project": project,
        "voter": voter,
        "status": status,
        "mine": mine,
        "open": voting_service.voting_is_open(event),
        "closed": voting_service.voting_has_closed(event),
        "cost": voting_service.cost_of_next_vote(mine) if event.quadratic_enabled else 1,
        "count": count,
        "user": ctx.user,
        "mode": event.voting_mode.value,
        "own_team": projects_service.is_member(db, project, ctx.user),
    }


def _control(request: Request, db, ctx, project, voter, status_code=200, error="", compact=False):
    return render(
        request,
        "partials/vote_control.html",
        status_code=status_code,
        error=error,
        compact=compact,
        swapped=True,
        **vote_context(db, ctx, project, voter),
    )


@router.post(
    "/e/{slug}/projects/{pid}/vote",
    dependencies=[Depends(verify_csrf), Depends(limiter("vote", 30, 60))],
)
def vote(
    request: Request,
    pid: str,
    website: str = Form(""),
    compact_flag: str = Form("", alias="compact"),
    ctx: EventContext = Depends(load_event),
    voter: Voter | None = Depends(current_voter),
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    compact = compact_flag == "1"
    project = projects_service.get_project(db, ctx.event, pid, ctx.user, organizer=ctx.is_organizer)
    new_cookie = None
    if voter is None and ctx.event.voting_mode.value == "link":
        voter_id, new_cookie = voting_service.new_voter_cookie(settings.secret_key)
        voter = Voter(key=f"anon:{voter_id}", mode=ctx.event.voting_mode)
    if voter is None:
        return _control(
            request,
            db,
            ctx,
            project,
            voter,
            status_code=401,
            error="Sign in or enter your voting code to vote.",
        )
    try:
        voting_service.cast(
            db,
            ctx.event,
            project,
            voter,
            ip_hash=ip_hash(request),
            ua_hash=ua_hash(request),
            honeypot=website,
        )
    except PodiumError as exc:
        return _control(
            request,
            db,
            ctx,
            project,
            voter,
            status_code=exc.status_code,
            error=exc.message,
            compact=compact,
        )
    response = (
        _control(request, db, ctx, project, voter, compact=compact)
        if is_htmx(request)
        else RedirectResponse(f"/e/{ctx.event.slug}/projects/{pid}", status_code=303)
    )
    if new_cookie:
        set_voter_cookie(response, new_cookie, settings)
    return response


@router.post("/e/{slug}/projects/{pid}/unvote", dependencies=[Depends(verify_csrf)])
def unvote(
    request: Request,
    pid: str,
    compact_flag: str = Form("", alias="compact"),
    ctx: EventContext = Depends(load_event),
    voter: Voter | None = Depends(current_voter),
    db: DbSession = Depends(get_db),
):
    compact = compact_flag == "1"
    project = projects_service.get_project(db, ctx.event, pid, ctx.user, organizer=ctx.is_organizer)
    if voter is None:
        return _control(
            request,
            db,
            ctx,
            project,
            voter,
            status_code=401,
            error="Nothing to remove.",
            compact=compact,
        )
    try:
        voting_service.retract(db, ctx.event, project, voter, ip_hash=ip_hash(request))
    except PodiumError as exc:
        return _control(
            request,
            db,
            ctx,
            project,
            voter,
            status_code=exc.status_code,
            error=exc.message,
            compact=compact,
        )
    if is_htmx(request):
        return _control(request, db, ctx, project, voter, compact=compact)
    return RedirectResponse(f"/e/{ctx.event.slug}/projects/{pid}", status_code=303)


@router.get("/e/{slug}/judging")
def how_judged_page(
    request: Request,
    ctx: EventContext = Depends(load_event),
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Everything a participant needs to trust the result: rubric, coverage, what normalization
    did, how votes counted, pairwise summary, and the signed audit anchor. Public once results
    are published; organizers can always preview it."""
    from statistics import median

    from podium.services import audit as audit_service
    from podium.services import dashboard, pairwise, rubric

    event = ctx.event
    if not voting_service.results_visible(event, organizer=ctx.is_organizer):
        raise NotFound("Results haven't been published yet.")
    results = scoring.compute(db, event)
    progress = dashboard.progress(db, event)
    counts = [p.n for p in results.projects if p.n]
    movers = sorted(
        [p for p in results.projects if p.rank_delta],
        key=lambda p: -abs(p.rank_delta or 0),
    )[:5]
    stage = stage_of(event)
    anchor = audit_service.anchor(db, settings)
    return render(
        request,
        "public/judging.html",
        title=f"How this event was judged · {event.name}",
        event=event,
        user=ctx.user,
        ctx=ctx,
        stage=stage.value,
        stage_label=STAGE_LABELS[stage],
        nav="results",
        criteria=rubric.criteria(db, event),
        results=results,
        progress=progress,
        coverage={
            "min": min(counts) if counts else 0,
            "median": int(median(counts)) if counts else 0,
            "max": max(counts) if counts else 0,
        },
        movers=movers,
        pairwise=pairwise.results(db, event),
        anchor=anchor,
        anchor_at=datetime.fromisoformat(anchor["at"]),
        confidence=scoring.confidence(results, seed=event.id),
    )


@router.get("/e/{slug}/vote")
def ballot_page(
    request: Request,
    ctx: EventContext = Depends(load_event),
    voter: Voter | None = Depends(current_voter),
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """The ballot: every project in this voter's fixed order with one control per row."""
    event = ctx.event
    open_now = voting_service.voting_is_open(event)
    new_cookie = None
    if voter is not None:
        key = voter.key
    else:
        anon = voting_service.parse_voter_cookie(
            settings.secret_key, request.cookies.get(VOTER_COOKIE)
        )
        if anon is None and open_now:
            anon, new_cookie = voting_service.new_voter_cookie(settings.secret_key)
        key = f"anon:{anon or 'visitor'}"
    rows = voting_service.ballot(db, event, voter, key)
    status = voting_service.voter_status(db, event, voter)
    stage = stage_of(event)
    response = render(
        request,
        "public/ballot.html",
        title=f"Ballot · {event.name}",
        event=event,
        user=ctx.user,
        ctx=ctx,
        stage=stage.value,
        stage_label=STAGE_LABELS[stage],
        nav="vote",
        rows=rows,
        status=status,
        voter=voter,
        open=open_now,
        closed=voting_service.voting_has_closed(event),
        mode=event.voting_mode.value,
        results_visible=voting_service.results_visible(event, organizer=ctx.is_organizer),
    )
    if new_cookie:
        set_voter_cookie(response, new_cookie, settings)
    return response


@router.get("/e/{slug}/vote/code")
def code_page(
    request: Request,
    ctx: EventContext = Depends(load_event),
    voter: Voter | None = Depends(current_voter),
    code: str = Query("", max_length=40),
):
    stage = stage_of(ctx.event)
    return render(
        request,
        "public/vote_code.html",
        title="Enter your voting code",
        event=ctx.event,
        user=ctx.user,
        stage=stage.value,
        stage_label=STAGE_LABELS[stage],
        voter=voter,
        error="",
        code=code,
    )


@router.post(
    "/e/{slug}/vote/code",
    dependencies=[Depends(verify_csrf), Depends(limiter("vote_code", 10, 60))],
)
def code_submit(
    request: Request,
    code: str = Form(""),  # noqa: B008
    ctx: EventContext = Depends(load_event),
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    stage = stage_of(ctx.event)
    try:
        key = voting_service.redeem_code(db, ctx.event, code)
    except PodiumError as exc:
        return render(
            request,
            "public/vote_code.html",
            status_code=exc.status_code,
            title="Enter your voting code",
            event=ctx.event,
            user=ctx.user,
            stage=stage.value,
            stage_label=STAGE_LABELS[stage],
            voter=None,
            code=code,
            error=exc.message,
        )
    response = RedirectResponse(f"/e/{ctx.event.slug}/vote", status_code=303)
    response.set_cookie(
        code_cookie_name(ctx.event),
        f"{key}.{_sign(settings.secret_key, key)}",
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
        max_age=60 * 60 * 24 * 30,
        path="/",
    )
    return response


# --- comments ---------------------------------------------------------------------------------


def comments_context(db, ctx: EventContext, project) -> dict:
    rows = comments_service.list_for(db, project, include_hidden=ctx.is_organizer)
    return {
        "event": ctx.event,
        "project": project,
        "comments": rows,
        "user": ctx.user,
        "ctx": ctx,
        "enabled": ctx.event.comments_enabled and ctx.event.archived_at is None,
        "errors": {},
        "body": "",
    }


@router.post(
    "/e/{slug}/projects/{pid}/comments",
    dependencies=[Depends(verify_csrf), Depends(limiter("comment", 10, 60))],
)
def comment_add(
    request: Request,
    pid: str,
    body: str = Form(""),
    ctx: EventContext = Depends(load_event),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    project = projects_service.get_project(db, ctx.event, pid, user, organizer=ctx.is_organizer)
    try:
        comments_service.add(db, ctx.event, project, user, body, ip_hash=ip_hash(request))
    except PodiumError as exc:
        c = comments_context(db, ctx, project)
        c["errors"] = getattr(exc, "errors", None) or {"body": exc.message}
        c["body"] = body
        return render(request, "partials/comments.html", status_code=exc.status_code, **c)
    if is_htmx(request):
        return render(request, "partials/comments.html", **comments_context(db, ctx, project))
    return RedirectResponse(f"/e/{ctx.event.slug}/projects/{pid}#comments", status_code=303)


@router.post("/e/{slug}/projects/{pid}/comments/{cid}/hide", dependencies=[Depends(verify_csrf)])
def comment_hide(
    request: Request,
    pid: str,
    cid: str,
    hidden: str = Form("1"),
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    project = projects_service.get_project(db, ctx.event, pid, ctx.user, organizer=True)
    comment = comments_service.get(db, project, cid)
    comments_service.set_hidden(db, ctx.event, comment, ctx.user, hidden == "1")
    if is_htmx(request):
        return render(request, "partials/comments.html", **comments_context(db, ctx, project))
    return RedirectResponse(f"/e/{ctx.event.slug}/projects/{pid}#comments", status_code=303)


# --- public results ---------------------------------------------------------------------------


@router.get("/e/{slug}/results")
def results_page(
    request: Request, ctx: EventContext = Depends(load_event), db: DbSession = Depends(get_db)
):
    stage = stage_of(ctx.event)
    base = {
        "event": ctx.event,
        "user": ctx.user,
        "ctx": ctx,
        "stage": stage.value,
        "stage_label": STAGE_LABELS[stage],
        "nav": "results",
        "title": f"Results · {ctx.event.name}",
    }
    if not voting_service.results_visible(ctx.event, organizer=ctx.is_organizer):
        return render(request, "public/results.html", published=False, **base)
    results = scoring.compute(db, ctx.event)
    tallies = (
        voting_service.tally(db, ctx.event)
        if voting_service.tallies_visible(ctx.event, organizer=ctx.is_organizer)
        else None
    )
    votes_by_project = {t.project.id: t.votes for t in tallies.projects} if tallies else {}
    ranked = [
        p
        for p in results.projects
        if (p.rank_norm if results.basis.value == "normalized" else p.rank_raw)
    ]
    top = ranked[:3]
    favourites = []
    if tallies and tallies.total_votes >= 3:
        top_votes = max((t.votes for t in tallies.projects), default=0)
        favourites = [t for t in tallies.projects if t.votes == top_votes and t.votes > 0]
    return render(
        request,
        "public/results.html",
        published=True,
        results=results,
        ranked=ranked,
        top=top,
        tallies=tallies,
        votes_by_project=votes_by_project,
        favourites=favourites,
        awards=events_service.awards(ctx.event),
        confidence=scoring.confidence(results, seed=ctx.event.id),
        **base,
    )


_ = Response
