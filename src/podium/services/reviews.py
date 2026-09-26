"""Judges' reviews: drafts autosave, submission locks the assignment as done, editing is allowed
while judging is open. Reading is governed by services.authz — never by templates."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from podium.errors import Closed, Forbidden, NotFound, ValidationFailed
from podium.models import (
    Assignment,
    AssignmentStatus,
    Event,
    Project,
    Review,
    ReviewStatus,
    RubricCriterion,
    ScoreItem,
    User,
    utcnow,
)
from podium.services import audit, webhooks
from podium.services.rubric import criteria as rubric_criteria


def judging_is_open(event: Event) -> bool:
    return (
        event.judging_opened_at is not None
        and event.judging_closed_at is None
        and event.archived_at is None
    )


@dataclass
class QueueItem:
    assignment: Assignment
    project: Project
    review: Review | None

    @property
    def state(self) -> str:
        if self.review is not None and self.review.status == ReviewStatus.submitted:
            return "done"
        if self.review is not None:
            return "in_progress"
        return "todo"


def queue(db: DbSession, event: Event, judge: User) -> list[QueueItem]:
    rows = db.execute(
        select(Assignment, Project, Review)
        .join(Project, Project.id == Assignment.project_id)
        .outerjoin(Review, Review.assignment_id == Assignment.id)
        .where(Assignment.event_id == event.id, Assignment.judge_id == judge.id)
        .order_by(Assignment.id)
    ).all()
    return [QueueItem(assignment=a, project=p, review=r) for a, p, r in rows]


def assignment_for(db: DbSession, event: Event, judge: User, project_public_id: str) -> Assignment:
    assignment = db.execute(
        select(Assignment)
        .join(Project, Project.id == Assignment.project_id)
        .where(
            Assignment.event_id == event.id,
            Assignment.judge_id == judge.id,
            Project.public_id == project_public_id,
        )
    ).scalar_one_or_none()
    if assignment is None:
        raise Forbidden("This project isn't assigned to you.")
    return assignment


def review_for(db: DbSession, assignment: Assignment) -> Review | None:
    return db.execute(
        select(Review).where(Review.assignment_id == assignment.id)
    ).scalar_one_or_none()


def scores_of(review: Review | None) -> dict[int, int]:
    return {item.criterion_id: item.value for item in (review.items if review else [])}


def _validate_scores(
    raw: dict[str, str | int], crits: list[RubricCriterion], *, complete: bool
) -> tuple[dict[int, int], dict[str, str]]:
    values: dict[int, int] = {}
    errors: dict[str, str] = {}
    for crit in crits:
        value = raw.get(crit.public_id, raw.get(crit.key))
        if value in (None, ""):
            if complete:
                errors[crit.public_id] = f"Score “{crit.name}” before submitting."
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            errors[crit.public_id] = "Scores are whole numbers."
            continue
        if not crit.min_score <= number <= crit.max_score:
            errors[crit.public_id] = f"Use a score from {crit.min_score} to {crit.max_score}."
            continue
        values[crit.id] = number
    return values, errors


def save(
    db: DbSession,
    event: Event,
    assignment: Assignment,
    judge: User,
    raw_scores: dict,
    comment: str,
    *,
    submit: bool,
    ip_hash: str | None = None,
) -> Review:
    if not judging_is_open(event):
        raise Closed("Judging isn't open right now, so reviews can't be changed.")
    crits = rubric_criteria(db, event)
    if not crits:
        raise Closed("The organizer hasn't published a rubric yet.")
    values, errors = _validate_scores(raw_scores, crits, complete=submit)
    comment = (comment or "").strip()
    if len(comment) > 5000:
        errors["comment"] = "Keep the comment under 5,000 characters."
    if errors:
        raise ValidationFailed(errors=errors)
    review = review_for(db, assignment)
    if review is None:
        review = Review(
            assignment_id=assignment.id,
            event_id=event.id,
            judge_id=judge.id,
            project_id=assignment.project_id,
        )
        db.add(review)
        db.flush()
    existing = {item.criterion_id: item for item in review.items}
    for crit_id, number in values.items():
        if crit_id in existing:
            existing[crit_id].value = number
        else:
            db.add(ScoreItem(review_id=review.id, criterion_id=crit_id, value=number))
    review.comment = comment
    if submit:
        review.status = ReviewStatus.submitted
        review.submitted_at = utcnow()
        assignment.status = AssignmentStatus.done
        action = "review.submitted"
        webhooks.emit(
            db,
            event,
            "review.submitted",
            {
                "review": review.public_id,
                "judge": judge.public_id,
                "project": assignment.project.public_id,
            },
        )
    else:
        if review.status != ReviewStatus.submitted:
            assignment.status = AssignmentStatus.in_progress
        action = "review.draft_saved"
    audit.record(
        db,
        action,
        "review",
        review.public_id,
        event_id=event.id,
        actor_id=judge.id,
        meta={"project": assignment.project.public_id},
        ip_hash=ip_hash,
    )
    db.commit()
    db.refresh(review)
    return review


def reopen(db: DbSession, event: Event, assignment: Assignment, judge: User) -> Review:
    """Turn a submitted review back into a draft so the judge can change it (judging open)."""
    if not judging_is_open(event):
        raise Closed("Judging is closed; submitted reviews are final.")
    review = review_for(db, assignment)
    if review is None:
        raise NotFound("There's no review to edit yet.")
    review.status = ReviewStatus.draft
    assignment.status = AssignmentStatus.in_progress
    audit.record(
        db, "review.reopened", "review", review.public_id, event_id=event.id, actor_id=judge.id
    )
    db.commit()
    return review


def reviews_of_judge(db: DbSession, event: Event, judge: User) -> list[Review]:
    return list(
        db.execute(
            select(Review)
            .where(Review.event_id == event.id, Review.judge_id == judge.id)
            .order_by(Review.id)
        ).scalars()
    )
