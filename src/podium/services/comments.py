"""Project comments: signed-in users only, organizers can hide (never silently delete)."""

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from podium.errors import Closed, NotFound, ValidationFailed
from podium.models import Comment, Event, Project, User, utcnow
from podium.services import audit

MAX_LENGTH = 2000


def list_for(db: DbSession, project: Project, *, include_hidden: bool = False) -> list[Comment]:
    query = select(Comment).where(Comment.project_id == project.id)
    if not include_hidden:
        query = query.where(Comment.hidden_at.is_(None))
    return list(db.execute(query.order_by(Comment.created_at)).scalars())


def add(
    db: DbSession, event: Event, project: Project, user: User, body: str, *, ip_hash=None
) -> Comment:
    if not event.comments_enabled or event.archived_at is not None:
        raise Closed("Comments are turned off for this event.")
    body = body.strip()
    if not body:
        raise ValidationFailed(errors={"body": "Write something first."})
    if len(body) > MAX_LENGTH:
        raise ValidationFailed(errors={"body": f"Keep comments under {MAX_LENGTH:,} characters."})
    comment = Comment(project_id=project.id, user_id=user.id, body=body)
    db.add(comment)
    db.flush()
    audit.record(
        db,
        "comment.added",
        "comment",
        comment.public_id,
        event_id=event.id,
        actor_id=user.id,
        meta={"project": project.public_id},
        ip_hash=ip_hash,
    )
    db.commit()
    return comment


def get(db: DbSession, project: Project, public_id: str) -> Comment:
    comment = db.execute(
        select(Comment).where(Comment.project_id == project.id, Comment.public_id == public_id)
    ).scalar_one_or_none()
    if comment is None:
        raise NotFound("No such comment.")
    return comment


def set_hidden(
    db: DbSession, event: Event, comment: Comment, organizer: User, hidden: bool
) -> Comment:
    comment.hidden_at = utcnow() if hidden else None
    comment.hidden_by = organizer.id if hidden else None
    audit.record(
        db,
        "comment.hidden" if hidden else "comment.unhidden",
        "comment",
        comment.public_id,
        event_id=event.id,
        actor_id=organizer.id,
    )
    db.commit()
    return comment
