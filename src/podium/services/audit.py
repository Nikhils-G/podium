"""Append-only audit log with a hash chain. Every row commits to the previous row's hash, so a
deleted or edited entry breaks verification."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select, text
from sqlalchemy.orm import Session as DbSession

from podium.models import AuditLog, utcnow
from podium.services.text import plural

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
    """Append one entry. Caller commits.

    Insert first, link second: the placeholder INSERT takes the database write lock (SQLite's
    implicit BEGIN before the first DML; an advisory lock on PostgreSQL), so no other writer can
    commit a row until this transaction ends. Only then is the previous row read and hashed. The
    old read-then-insert let two concurrent requests link to the same head and fork the chain.
    """
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(7340031)"))  # constant: "the audit chain"
    row = AuditLog(
        event_id=event_id,
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id)[:64],  # failed sign-ins store the typed email; column is 64
        meta=meta or {},
        ip_hash=ip_hash,
        created_at=utcnow(),
        prev_hash="",
        row_hash="",
    )
    db.add(row)
    db.flush()
    last = db.execute(
        select(AuditLog.row_hash).where(AuditLog.id < row.id).order_by(AuditLog.id.desc()).limit(1)
    ).scalar()
    row.prev_hash = last or GENESIS
    row.row_hash = hashlib.sha256((row.prev_hash + _canonical(row)).encode()).hexdigest()
    db.flush()
    return row


@dataclass
class ChainReport:
    entries: int
    ok: bool
    first_bad_id: int | None = None
    head_hash: str = GENESIS


def verify_chain(db: DbSession) -> ChainReport:
    prev = GENESIS
    count = 0
    for row in db.execute(select(AuditLog).order_by(AuditLog.id)).scalars():
        count += 1
        expected = hashlib.sha256((prev + _canonical(row)).encode()).hexdigest()
        if row.prev_hash != prev or row.row_hash != expected:
            return ChainReport(entries=count, ok=False, first_bad_id=row.id, head_hash=prev)
        prev = row.row_hash
    return ChainReport(entries=count, ok=True, head_hash=prev)


FIELD_LABELS = {
    "submissions_open_at": "submissions opening",
    "submissions_close_at": "submission deadline",
    "voting_open_at": "voting opening",
    "voting_close_at": "voting closing",
    "name": "event name",
    "description": "description",
    "is_public": "visibility",
    "max_team_size": "maximum team size",
}


def describe(entry: "AuditLog") -> str:
    """One readable sentence per entry (the raw action and JSON stay available)."""
    meta = entry.meta or {}
    action, eid = entry.action, entry.entity_id
    if action == "event.updated":
        changes = meta.get("changes") or {}
        parts = []
        for field, (old, new) in changes.items():
            label = FIELD_LABELS.get(field, field.replace("_", " "))
            parts.append(f"changed the {label} {_short(old)} → {_short(new)}")
        sentence = "; ".join(parts)  # capitalise the first letter only: keep "Mar", "UTC", names
        return sentence[:1].upper() + sentence[1:] if parts else "Saved the event settings"
    if action == "event.imported":
        return _imported(meta.get("counts") or {})
    table = {
        "event.created": "Created the event",
        "event.publish_event": "Made the event public",
        "event.unpublish_event": "Made the event private",
        "event.open_judging": "Opened judging",
        "event.close_judging": "Closed judging",
        "event.publish_results": "Published the results",
        "event.unpublish_results": "Unpublished the results",
        "event.archive": "Archived the event",
        "judging.opened": "Opened judging",
        "judging.closed": "Closed judging",
        "results.published": "Published the results",
        "results.unpublished": "Unpublished the results",
        "results.settings": "Changed the ranking settings",
        "voting.settings": "Changed the voting settings",
        "voting.codes_generated": f"Generated {meta.get('count', '?')} voting codes",
        "judge.invited": f"Invited {meta.get('email', '?')} to judge",
        "judge.invite_regenerated": f"Regenerated the invitation for {meta.get('email', '?')}",
        "judge.invite_revoked": f"Revoked the invitation for {meta.get('email', '?')}",
        "judge.accepted": f"Accepted a judge invitation ({eid})",
        "judge.removed": f"Removed judge {eid}",
        "organizer.added": f"Added organizer {eid}"
        + (" (by importing an event file)" if meta.get("via") == "import" else ""),
        "organizer.removed": f"Removed organizer {eid}",
        "prize.added": f"Added prize “{meta.get('name', eid)}”",
        "prize.removed": f"Removed prize “{meta.get('name', eid)}”",
        "prize.awarded": f"Awarded “{meta.get('name', eid)}” to {meta.get('project')}",
        "prize.cleared": f"Cleared the award for “{meta.get('name', eid)}”",
        "track.added": f"Added track {eid}",
        "track.removed": f"Removed track {eid}",
        "rubric.criterion_added": f"Added rubric criterion {eid}",
        "rubric.criterion_updated": f"Updated rubric criterion {eid}",
        "rubric.criterion_archived": f"Archived rubric criterion {eid}",
        "rubric.criterion_deleted": f"Deleted rubric criterion {eid}",
        "assignments.auto_applied": "Applied an auto-assignment plan",
        "assignments.manual": "Assigned projects to a judge",
        "assignments.removed": "Removed an assignment",
        "assignment.created": f"Created assignment {eid}",
        "review.draft_saved": f"Saved a review draft for {eid}",
        "review.submitted": f"Submitted a review of {eid}",
        "review.reopened": f"Reopened the review of {eid}",
        "comparison.recorded": f"Recorded a pairwise comparison ({eid})",
        "comparison.undone": "Undid a pairwise comparison",
        "project.created": f"Created project {eid}",
        "project.updated": f"Edited project {eid}",
        "project.submitted": f"Submitted project {eid}",
        "project.unsubmitted": f"Returned project {eid} to draft",
        "project.withdrawn": f"Withdrew project {eid}",
        "project.restored": f"Restored project {eid}",
        "project.unlocked": f"Unlocked project {eid} for editing",
        "team.created": f"Created team {eid}",
        "team.joined": f"Joined team {eid}",
        "team.left": f"Left team {eid}",
        "team.renamed": f"Renamed team {eid}",
        "team.invite_regenerated": f"Regenerated the join link for team {eid}",
        "vote.cast": f"Cast a vote on {eid}",
        "vote.retracted": f"Retracted a vote on {eid}",
        "vote.rejected": f"Had a vote on {eid} refused ({meta.get('reason', 'rule')})",
        "vote.voided": f"Voided a vote ({meta.get('reason', 'no reason given')})",
        "comment.added": f"Commented on {meta.get('project', eid)}",
        "comment.hidden": f"Hid comment {eid}",
        "comment.unhidden": f"Unhid comment {eid}",
        "certificate.issued": f"Issued certificate {eid}",
        "certificate.revoked": f"Revoked certificate {eid}"
        + (f" ({meta['reason']})" if meta.get("reason") else ""),
        "webhook.created": f"Added webhook {eid}",
        "webhook.deleted": f"Deleted webhook {eid}",
        "webhook.disabled": f"Paused webhook {eid}",
        "webhook.enabled": f"Resumed webhook {eid}",
        "webhook.redelivered": f"Redelivered webhook call {eid}",
        "token.created": (
            f"Created API token “{meta.get('name', eid)}” ({meta.get('scope', 'write')})"
        ),
        "token.revoked": f"Revoked API token {eid}",
        "user.registered": "Registered an account",
        "user.bootstrap_admin": "Became the instance admin (first account)",
        "user.login": "Signed in",
        "user.login_failed": f"Failed sign-in for {eid}",
        "user.password_changed": "Changed their password",
        "user.profile_updated": "Updated their profile",
        "signing.key": "Generated the instance signing key",
    }
    return table.get(action) or f"{action.replace('.', ' ').replace('_', ' ').capitalize()} ({eid})"


SINGULAR = {"criteria": "criterion"}  # import counts are keyed by plural nouns


def _imported(counts: dict) -> str:
    """'Imported 41 projects, 30 judges and 126 reviews' from an import's counts."""
    parts = [plural(n, SINGULAR.get(key, key.removesuffix("s")), key) for key, n in counts.items()]
    if not parts:
        return "Imported an event file"
    listed = ", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else parts[0]
    return f"Imported {listed}"


def _short(value) -> str:
    """A value for an audit sentence; timestamps read the way the rest of the app shows them."""
    if value is None:
        return "—"
    text = str(value)
    if len(text) >= 16 and text[:4].isdigit() and text[4] == "-":
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text[:16]
        return f"{moment.day} {moment:%b %Y, %H:%M} UTC"
    return text


def anchor(db: DbSession, settings) -> dict:
    """A signed statement of the chain head. Publish it anywhere public (a tweet, a wiki page,
    a mailing list) and any later rewrite of earlier rows becomes provable."""
    from podium.services import certificates

    report = verify_chain(db)
    payload = {
        "entries": report.entries,
        "head_hash": report.head_hash,
        "ok": report.ok,
        "at": utcnow().isoformat(),
        "public_key_hex": certificates.public_key_hex(db, settings),
    }
    return {**payload, "signature": certificates.sign(settings, payload)}


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
