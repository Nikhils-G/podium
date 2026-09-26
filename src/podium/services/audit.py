"""Append-only audit log with a hash chain. Every row commits to the previous row's hash, so a
deleted or edited entry breaks verification."""

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from podium.models import AuditLog, utcnow

GENESIS = "0" * 64


def _canonical(row: AuditLog) -> str:
    return json.dumps(
        {
            "event_id": row.event_id,
            "actor_id": row.actor_id,
            "action": row.action,
            "entity_type": row.entity_type,
            "entity_id": row.entity_id,
            "meta": row.meta,
            "ip_hash": row.ip_hash,
            "created_at": row.created_at.isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def record(
    db: DbSession,
    action: str,
    entity_type: str,
    entity_id: str | int,
    *,
    event_id: int | None = None,
    actor_id: int | None = None,
    meta: dict | None = None,
    ip_hash: str | None = None,
) -> AuditLog:
    """Append one entry. Caller commits; the chain is computed inside a flush-ordered read."""
    last = db.execute(select(AuditLog.row_hash).order_by(AuditLog.id.desc()).limit(1)).scalar()
    row = AuditLog(
        event_id=event_id,
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        meta=meta or {},
        ip_hash=ip_hash,
        created_at=utcnow(),
        prev_hash=last or GENESIS,
    )
    row.row_hash = hashlib.sha256((row.prev_hash + _canonical(row)).encode()).hexdigest()
    db.add(row)
    db.flush()
    return row


@dataclass
class ChainReport:
    entries: int
    ok: bool
    first_bad_id: int | None = None


def verify_chain(db: DbSession) -> ChainReport:
    prev = GENESIS
    count = 0
    for row in db.execute(select(AuditLog).order_by(AuditLog.id)).scalars():
        count += 1
        expected = hashlib.sha256((prev + _canonical(row)).encode()).hexdigest()
        if row.prev_hash != prev or row.row_hash != expected:
            return ChainReport(entries=count, ok=False, first_bad_id=row.id)
        prev = row.row_hash
    return ChainReport(entries=count, ok=True)


if TYPE_CHECKING:
    from podium.models import User


@dataclass
class AuditPage:
    rows: list[AuditLog]
    total: int
    page: int
    pages: int
    actions: list[str]
    actors: dict[int, "User"] = field(default_factory=dict)  # actor_id → user, for this page


def list_entries(
    db: DbSession,
    event_id: int | None,
    *,
    action: str = "",
    actor_id: int | None = None,
    page: int = 1,
    page_size: int = 50,
) -> AuditPage:
    from sqlalchemy import func

    query = select(AuditLog)
    if event_id is not None:
        query = query.where(AuditLog.event_id == event_id)
    if action:
        query = query.where(AuditLog.action == action)
    if actor_id is not None:
        query = query.where(AuditLog.actor_id == actor_id)
    total = db.execute(select(func.count()).select_from(query.subquery())).scalar_one()
    pages = max(1, -(-total // page_size))
    page = min(max(1, page), pages)
    rows = list(
        db.execute(
            query.order_by(AuditLog.id.desc()).offset((page - 1) * page_size).limit(page_size)
        ).scalars()
    )
    actions_q = select(AuditLog.action).distinct().order_by(AuditLog.action)
    if event_id is not None:
        actions_q = actions_q.where(AuditLog.event_id == event_id)
    actions = list(db.execute(actions_q).scalars())
    from podium.models import User

    actor_ids = {r.actor_id for r in rows if r.actor_id is not None}
    actors = (
        {u.id: u for u in db.execute(select(User).where(User.id.in_(actor_ids))).scalars()}
        if actor_ids
        else {}
    )
    return AuditPage(rows=rows, total=total, page=page, pages=pages, actions=actions, actors=actors)
