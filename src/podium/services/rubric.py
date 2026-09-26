"""The scoring rubric. Structure (which criteria exist, their scale) locks once judging has opened,
so every review is scored on the same instrument; weights stay editable because totals are
computed on read and re-weight consistently."""

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from podium.errors import Conflict, NotFound, ValidationFailed
from podium.models import Event, RubricCriterion, ScoreItem, User, utcnow
from podium.services import audit


def criteria(
    db: DbSession, event: Event, *, include_archived: bool = False
) -> list[RubricCriterion]:
    query = select(RubricCriterion).where(RubricCriterion.event_id == event.id)
    if not include_archived:
        query = query.where(RubricCriterion.archived_at.is_(None))
    return list(db.execute(query.order_by(RubricCriterion.position)).scalars())


def is_locked(event: Event) -> bool:
    return event.judging_opened_at is not None


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40] or "criterion"


def _validate(name: str, weight, min_score, max_score) -> tuple[dict, dict[str, str]]:
    errors: dict[str, str] = {}
    name = (name or "").strip()
    if not name or len(name) > 120:
        errors["name"] = "Criterion names are 1–120 characters."
    try:
        weight = float(weight)
        if not 0 < weight <= 100:
            raise ValueError
    except (TypeError, ValueError):
        errors["weight"] = "Weight must be a number between 0 and 100."
        weight = 1.0
    try:
        min_score, max_score = int(min_score), int(max_score)
        if not (0 <= min_score < max_score <= 100):
            raise ValueError
    except (TypeError, ValueError):
        errors["scale"] = "Scale must be two whole numbers, minimum below maximum (e.g. 1 to 5)."
        min_score, max_score = 1, 5
    return {"name": name, "weight": weight, "min_score": min_score, "max_score": max_score}, errors


def add_criterion(
    db: DbSession,
    event: Event,
    user: User,
    name: str,
    description: str = "",
    weight=1.0,
    min_score=1,
    max_score=5,
) -> RubricCriterion:
    if is_locked(event):
        raise Conflict(
            "Judging has opened, so the rubric's structure is locked. Weights can still change."
        )
    clean, errors = _validate(name, weight, min_score, max_score)
    if errors:
        raise ValidationFailed(errors=errors)
    key = _key(clean["name"])
    if db.execute(
        select(RubricCriterion.id).where(
            RubricCriterion.event_id == event.id, RubricCriterion.key == key
        )
    ).scalar():
        raise Conflict("A criterion with that name already exists.")
    position = (
        db.execute(
            select(func.coalesce(func.max(RubricCriterion.position), -1)).where(
                RubricCriterion.event_id == event.id
            )
        ).scalar_one()
        + 1
    )
    crit = RubricCriterion(
        event_id=event.id, key=key, description=description.strip(), position=position, **clean
    )
    db.add(crit)
    db.flush()
    audit.record(
        db,
        "rubric.criterion_added",
        "criterion",
        crit.public_id,
        event_id=event.id,
        actor_id=user.id,
        meta=clean,
    )
    db.commit()
    return crit


def get_criterion(db: DbSession, event: Event, public_id: str) -> RubricCriterion:
    crit = db.execute(
        select(RubricCriterion).where(
            RubricCriterion.event_id == event.id, RubricCriterion.public_id == public_id
        )
    ).scalar_one_or_none()
    if crit is None:
        raise NotFound("No such criterion.")
    return crit


def update_criterion(
    db: DbSession,
    event: Event,
    user: User,
    crit: RubricCriterion,
    *,
    name=None,
    description=None,
    weight=None,
    min_score=None,
    max_score=None,
) -> RubricCriterion:
    clean, errors = _validate(
        name if name is not None else crit.name,
        weight if weight is not None else crit.weight,
        min_score if min_score is not None else crit.min_score,
        max_score if max_score is not None else crit.max_score,
    )
    if errors:
        raise ValidationFailed(errors=errors)
    scale_changed = (clean["min_score"], clean["max_score"]) != (crit.min_score, crit.max_score)
    if scale_changed and (is_locked(event) or _has_scores(db, crit)):
        raise Conflict("The scale can't change once judging has opened or scores exist.")
    changes = {}
    for field in ("name", "weight", "min_score", "max_score"):
        if getattr(crit, field) != clean[field]:
            changes[field] = [getattr(crit, field), clean[field]]
            setattr(crit, field, clean[field])
    if description is not None and description.strip() != crit.description:
        changes["description"] = [crit.description, description.strip()]
        crit.description = description.strip()
    if changes:
        audit.record(
            db,
            "rubric.criterion_updated",
            "criterion",
            crit.public_id,
            event_id=event.id,
            actor_id=user.id,
            meta={"changes": changes},
        )
    db.commit()
    return crit


def _has_scores(db: DbSession, crit: RubricCriterion) -> bool:
    return (
        db.execute(select(ScoreItem.id).where(ScoreItem.criterion_id == crit.id).limit(1)).scalar()
        is not None
    )


def archive_criterion(db: DbSession, event: Event, user: User, crit: RubricCriterion) -> None:
    """Criteria with scores are archived (kept for history); unscored ones are deleted."""
    if is_locked(event):
        raise Conflict("Judging has opened, so the rubric's structure is locked.")
    if _has_scores(db, crit):
        crit.archived_at = utcnow()
        action = "rubric.criterion_archived"
    else:
        db.delete(crit)
        action = "rubric.criterion_deleted"
    audit.record(
        db,
        action,
        "criterion",
        crit.public_id,
        event_id=event.id,
        actor_id=user.id,
        meta={"name": crit.name},
    )
    db.commit()
