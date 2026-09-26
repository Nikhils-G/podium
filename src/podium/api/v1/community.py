"""Voting, comments and tallies. Voter identity follows the event's mode exactly as on the web:
account (session/bearer), link (signed anonymous cookie) or a redeemed code cookie."""

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from podium.config import Settings, get_settings
from podium.db import get_db
from podium.errors import NotFound, Unauthorized
from podium.models import Project, User
from podium.schemas.community import (
    CodeRedeem,
    CodesCreate,
    CommentCreate,
    VoidVote,
    VotingSettings,
)
from podium.schemas.responses import TallyOut
from podium.security.deps import EventContext, load_event, require_organizer, require_user
from podium.security.ratelimit import ip_hash, limiter
from podium.services import comments as comments_service
from podium.services import events as events_service
from podium.services import projects as projects_service
from podium.services import voting as voting_service
from podium.services.voting import Voter
from podium.web.community import VOTER_COOKIE, current_voter, ua_hash

router = APIRouter(tags=["community"])


def _vote_out(event, project, voter, db) -> dict:
    status = voting_service.voter_status(db, event, voter)
    return {
        "project": project.public_id,
        "my_votes": status.votes.get(project.id, 0),
        "credits_spent": status.credits_spent,
        "credits_left": status.credits_left,
        "mode": event.voting_mode.value,
        "voting_open": voting_service.voting_is_open(event),
    }


@router.post(
    "/events/{slug}/projects/{pid}/votes",
    status_code=201,
    dependencies=[Depends(limiter("vote", 30, 60))],
)
def cast_vote(
    request: Request,
    pid: str,
    ctx: EventContext = Depends(load_event),
    voter: Voter | None = Depends(current_voter),
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Cast one vote (or one more credit in quadratic mode) for a project."""
    project = projects_service.get_project(db, ctx.event, pid, ctx.user, organizer=ctx.is_organizer)
    new_cookie = None
    if voter is None and ctx.event.voting_mode.value == "link":
        voter_id, new_cookie = voting_service.new_voter_cookie(settings.secret_key)
        voter = Voter(key=f"anon:{voter_id}", mode=ctx.event.voting_mode)
    if voter is None:
        raise Unauthorized("Sign in or redeem a voting code to vote in this event.")
    voting_service.cast(
        db, ctx.event, project, voter, ip_hash=ip_hash(request), ua_hash=ua_hash(request)
    )
    response = JSONResponse({"vote": _vote_out(ctx.event, project, voter, db)}, status_code=201)
    if new_cookie:
        response.set_cookie(
            VOTER_COOKIE,
            new_cookie,
            httponly=True,
            samesite="lax",
            secure=settings.secure_cookies,
            max_age=60 * 60 * 24 * 90,
            path="/",
        )
    return response


@router.delete("/events/{slug}/projects/{pid}/votes")
def retract_vote(
    request: Request,
    pid: str,
    ctx: EventContext = Depends(load_event),
    voter: Voter | None = Depends(current_voter),
    db: DbSession = Depends(get_db),
):
    project = projects_service.get_project(db, ctx.event, pid, ctx.user, organizer=ctx.is_organizer)
    if voter is None:
        raise Unauthorized("No voter identity on this request.")
    voting_service.retract(db, ctx.event, project, voter, ip_hash=ip_hash(request))
    return {"vote": _vote_out(ctx.event, project, voter, db)}


@router.get("/events/{slug}/votes/me")
def my_votes(
    ctx: EventContext = Depends(load_event),
    voter: Voter | None = Depends(current_voter),
    db: DbSession = Depends(get_db),
):
    status = voting_service.voter_status(db, ctx.event, voter)
    public_ids = {
        p.id: p.public_id
        for p in db.execute(select(Project).where(Project.id.in_(list(status.votes)))).scalars()
    }
    return {
        "identified": voter is not None,
        "mode": ctx.event.voting_mode.value,
        "voting_open": voting_service.voting_is_open(ctx.event),
        "credits_spent": status.credits_spent,
        "credits_left": status.credits_left,
        "votes": {public_ids.get(k, str(k)): v for k, v in status.votes.items()},
    }


@router.get("/events/{slug}/tally", response_model=TallyOut)
def tally(ctx: EventContext = Depends(load_event), db: DbSession = Depends(get_db)):
    """Vote counts per project. Hidden until voting has closed and results are published."""
    if not voting_service.tallies_visible(ctx.event, organizer=ctx.is_organizer):
        raise NotFound("Vote counts aren't public yet.")
    t = voting_service.tally(db, ctx.event)
    return {
        "total_votes": t.total_votes,
        "total_voters": t.total_voters,
        "voided": t.voided,
        "projects": [
            {
                "project": p.project.public_id,
                "title": p.project.title,
                "votes": p.votes,
                "voters": p.voters,
            }
            for p in t.projects
        ],
    }


@router.patch("/events/{slug}/voting")
def voting_settings(
    request: Request,
    body: VotingSettings,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    data = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    for key in ("quadratic_enabled", "comments_enabled"):
        if key not in data:
            data[key] = getattr(ctx.event, key)
    event = events_service.update_voting_settings(
        db, ctx.event, ctx.user, data, ip_hash=ip_hash(request)
    )
    return {
        "voting_mode": event.voting_mode.value,
        "quadratic_enabled": event.quadratic_enabled,
        "voting_credits": event.voting_credits,
        "comments_enabled": event.comments_enabled,
    }


@router.post("/events/{slug}/voting/codes", status_code=201)
def generate_codes(
    body: CodesCreate,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    """Generate voter codes (email/code mode). Codes are returned once — you distribute them."""
    codes = voting_service.generate_codes(db, ctx.event, ctx.user, body.count, body.emails)
    return {"codes": [{"code": c, "email": e} for c, e in codes]}


@router.post("/events/{slug}/voting/codes/redeem")
def redeem_code(
    body: CodeRedeem | None = None,
    code: str = Query("", max_length=40, description='Deprecated: send {"code"} as JSON.'),
    ctx: EventContext = Depends(load_event),
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    from podium.web.community import _sign, code_cookie_name

    key = voting_service.redeem_code(db, ctx.event, (body.code if body else "") or code)
    response = JSONResponse({"redeemed": True})
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


@router.get("/events/{slug}/votes/suspicious")
def suspicious_votes(
    ctx: EventContext = Depends(require_organizer), db: DbSession = Depends(get_db)
):
    return {
        "votes": [
            {
                "id": v.id,
                "project": v.project.public_id,
                "credits": v.credits,
                "ip_hash": v.ip_hash,
                "at": v.created_at,
            }
            for v in voting_service.suspicious(db, ctx.event)
        ]
    }


@router.post("/events/{slug}/votes/{vote_id}/void")
def void_vote(
    vote_id: int,
    body: VoidVote,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    vote = voting_service.void(db, ctx.event, ctx.user, vote_id, body.reason)
    return {"vote": {"id": vote.id, "voided_at": vote.voided_at, "reason": vote.void_reason}}


# --- comments ------------------------------------------------------------------------------------


def _comment_out(c) -> dict:
    return {
        "id": c.public_id,
        "author": c.user.name,
        "body": c.body,
        "created_at": c.created_at,
        "hidden": c.hidden_at is not None,
    }


@router.get("/events/{slug}/projects/{pid}/comments")
def list_comments(
    pid: str, ctx: EventContext = Depends(load_event), db: DbSession = Depends(get_db)
):
    project = projects_service.get_project(db, ctx.event, pid, ctx.user, organizer=ctx.is_organizer)
    return {
        "comments": [
            _comment_out(c)
            for c in comments_service.list_for(db, project, include_hidden=ctx.is_organizer)
        ]
    }


@router.post(
    "/events/{slug}/projects/{pid}/comments",
    status_code=201,
    dependencies=[Depends(limiter("comment", 10, 60))],
)
def add_comment(
    request: Request,
    pid: str,
    body: CommentCreate,
    ctx: EventContext = Depends(load_event),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    project = projects_service.get_project(db, ctx.event, pid, user, organizer=ctx.is_organizer)
    comment = comments_service.add(
        db, ctx.event, project, user, body.body, ip_hash=ip_hash(request)
    )
    return {"comment": _comment_out(comment)}


@router.post("/events/{slug}/projects/{pid}/comments/{cid}/hide")
def hide_comment(
    pid: str,
    cid: str,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    project = projects_service.get_project(db, ctx.event, pid, ctx.user, organizer=True)
    comment = comments_service.set_hidden(
        db, ctx.event, comments_service.get(db, project, cid), ctx.user, True
    )
    return {"comment": _comment_out(comment)}


@router.post("/events/{slug}/projects/{pid}/comments/{cid}/unhide")
def unhide_comment(
    pid: str,
    cid: str,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    project = projects_service.get_project(db, ctx.event, pid, ctx.user, organizer=True)
    comment = comments_service.set_hidden(
        db, ctx.event, comments_service.get(db, project, cid), ctx.user, False
    )
    return {"comment": _comment_out(comment)}
