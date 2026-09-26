from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session as DbSession

from podium.db import get_db
from podium.models import User
from podium.schemas.events import (
    EventCreate,
    EventUpdate,
    PrizeAward,
    PrizeCreate,
    TrackCreate,
    event_out,
)
from podium.schemas.responses import EventEnvelope, EventsOut
from podium.security.deps import (
    EventContext,
    current_user,
    load_event,
    require_can_create_event,
    require_organizer,
)
from podium.security.ratelimit import ip_hash
from podium.services import events as events_service
from podium.services.events import list_events_for, stage_of

router = APIRouter(tags=["events"])


def _payload(model: EventCreate) -> dict:
    data = model.model_dump()
    for key in ("submissions_open_at", "submissions_close_at", "voting_open_at", "voting_close_at"):
        data[key] = data[key].isoformat() if data[key] else ""
    data["is_public"] = "true" if data["is_public"] else ""
    return data


@router.get("/events", response_model=EventsOut)
def list_events(db: DbSession = Depends(get_db), user: User | None = Depends(current_user)):
    """Events visible to the caller: public ones, plus drafts they organize."""
    return {"events": [event_out(e, stage_of(e).value) for e in list_events_for(db, user)]}


@router.post("/events", status_code=201)
def create_event(
    request: Request,
    body: EventCreate,
    user: User = Depends(require_can_create_event),
    db: DbSession = Depends(get_db),
):
    """Create an event. The caller becomes its organizer."""
    event = events_service.create_event(db, user, _payload(body), ip_hash=ip_hash(request))
    return {"event": event_out(event, stage_of(event).value)}


@router.get("/events/{slug}", response_model=EventEnvelope)
def get_event(ctx: EventContext = Depends(load_event)):
    return {"event": event_out(ctx.event, stage_of(ctx.event).value)}


@router.patch("/events/{slug}")
def update_event(
    request: Request,
    body: EventUpdate,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    event = events_service.update_event(
        db, ctx.event, ctx.user, _payload(body), ip_hash=ip_hash(request)
    )
    return {"event": event_out(event, stage_of(event).value)}


@router.post("/events/{slug}/actions/{action}")
def event_action(
    request: Request,
    action: str,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    """Lifecycle actions: publish_event, unpublish_event, open_judging, close_judging,
    publish_results, unpublish_results, archive."""
    event = events_service.apply_action(db, ctx.event, ctx.user, action, ip_hash=ip_hash(request))
    return {"event": event_out(event, stage_of(event).value)}


@router.post("/events/{slug}/tracks", status_code=201)
def add_track(
    body: TrackCreate,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    track = events_service.add_track(db, ctx.event, ctx.user, body.name, body.description)
    return {"track": {"id": track.public_id, "name": track.name, "description": track.description}}


@router.delete("/events/{slug}/tracks/{track_id}", status_code=204)
def delete_track(
    track_id: str, ctx: EventContext = Depends(require_organizer), db: DbSession = Depends(get_db)
):
    events_service.remove_track(db, ctx.event, ctx.user, track_id)


@router.post("/events/{slug}/prizes", status_code=201)
def add_prize(
    body: PrizeCreate,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    prize = events_service.add_prize(
        db, ctx.event, ctx.user, body.name, body.amount, body.description, body.track
    )
    return {"prize": {"id": prize.public_id, "name": prize.name, "amount": prize.amount_text}}


@router.post("/events/{slug}/prizes/{prize_id}/award")
def award_prize(
    prize_id: str,
    body: PrizeAward,
    request: Request,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    """Award a prize to a submitted project (`project: null` clears it). Winner certificates and
    the public results page follow these awards."""
    prize = events_service.award_prize(
        db, ctx.event, ctx.user, prize_id, body.project, ip_hash=ip_hash(request)
    )
    return {
        "prize": {
            "id": prize.public_id,
            "name": prize.name,
            "project": prize.project.public_id if prize.project else None,
        }
    }


@router.delete("/events/{slug}/prizes/{prize_id}", status_code=204)
def delete_prize(
    prize_id: str, ctx: EventContext = Depends(require_organizer), db: DbSession = Depends(get_db)
):
    events_service.remove_prize(db, ctx.event, ctx.user, prize_id)


@router.get("/events/{slug}/organizers")
def list_organizers(
    ctx: EventContext = Depends(require_organizer), db: DbSession = Depends(get_db)
):
    return {
        "organizers": [
            {"id": u.public_id, "name": u.name, "email": u.email}
            for u in events_service.organizers(db, ctx.event)
        ]
    }


@router.post("/events/{slug}/organizers", status_code=201)
def add_organizer(
    body: dict, ctx: EventContext = Depends(require_organizer), db: DbSession = Depends(get_db)
):
    """Add a co-organizer by email (the account must already exist and hold no other role here)."""
    user = events_service.add_organizer(db, ctx.event, ctx.user, str(body.get("email", "")))
    return {"organizer": {"id": user.public_id, "name": user.name, "email": user.email}}


@router.delete("/events/{slug}/organizers/{user_id}", status_code=204)
def remove_organizer(
    user_id: str, ctx: EventContext = Depends(require_organizer), db: DbSession = Depends(get_db)
):
    events_service.remove_organizer(db, ctx.event, ctx.user, user_id)
