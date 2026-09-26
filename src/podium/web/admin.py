from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from podium.config import Settings, get_settings
from podium.db import get_db
from podium.models import Event, EventRole, Role, User
from podium.security.deps import require_admin
from podium.services import certificates as cert_service
from podium.services.events import STAGE_LABELS, stage_of
from podium.web.rendering import render

router = APIRouter(include_in_schema=False)


@router.get("/admin")
def admin_page(
    request: Request,
    user: User = Depends(require_admin),
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    events = list(db.execute(select(Event).order_by(Event.created_at.desc())).scalars())
    organizers = {}
    for event_id, name in db.execute(
        select(EventRole.event_id, User.name)
        .join(User, User.id == EventRole.user_id)
        .where(EventRole.role == Role.organizer)
    ).all():
        organizers.setdefault(event_id, []).append(name)
    users = db.execute(select(func.count()).select_from(User)).scalar_one()
    rows = [
        (e, stage_of(e).value, STAGE_LABELS[stage_of(e)], organizers.get(e.id, [])) for e in events
    ]
    return render(
        request,
        "admin/index.html",
        title="Admin",
        user=user,
        events=rows,
        users=users,
        key_hex=cert_service.public_key_hex(db, settings),
    )
