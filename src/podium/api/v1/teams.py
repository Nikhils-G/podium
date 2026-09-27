from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session as DbSession

from podium.db import get_db
from podium.errors import NotFound
from podium.models import User
from podium.schemas.events import TeamCreate
from podium.security.deps import EventContext, load_event, require_user
from podium.security.ratelimit import ip_hash
from podium.services import teams

router = APIRouter(tags=["teams"])


def team_out(view: teams.TeamView) -> dict:
    return {
        "id": view.team.public_id,
        "name": view.team.name,
        "invite_code": view.team.invite_code,
        "members": [{"id": u.public_id, "name": u.name, "role": r.value} for u, r in view.members],
        "project": view.project.public_id if view.project else None,
    }


@router.post("/events/{slug}/teams", status_code=201)
def create_team(
    request: Request,
    body: TeamCreate,
    ctx: EventContext = Depends(load_event),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    """Create a team in the event; the caller becomes its lead and a participant."""
    team = teams.create_team(db, ctx.event, user, body.name, ip_hash=ip_hash(request))
    return {"team": team_out(teams.team_view(db, team, user))}


@router.get("/events/{slug}/teams/mine")
def my_team(
    ctx: EventContext = Depends(load_event),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    """The caller's team in this event, with its members and project."""
    team = teams.team_for(db, ctx.event, user)
    if team is None:
        raise NotFound("You're not in a team for this event.")
    return {"team": team_out(teams.team_view(db, team, user))}


@router.post("/teams/join/{code}")
def join_team(
    request: Request, code: str, user: User = Depends(require_user), db: DbSession = Depends(get_db)
):
    """Join a team by its invite code."""
    team = teams.team_by_invite(db, code)
    teams.join_team(db, team, user, ip_hash=ip_hash(request))
    return {"team": team_out(teams.team_view(db, team, user))}


@router.post("/events/{slug}/teams/leave", status_code=204)
def leave_team(
    request: Request,
    ctx: EventContext = Depends(load_event),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    """Leave your team; refused once the team has submitted a project."""
    team = teams.team_for(db, ctx.event, user)
    if team is None:
        raise NotFound("You're not in a team for this event.")
    teams.leave_team(db, team, user, ip_hash=ip_hash(request))
