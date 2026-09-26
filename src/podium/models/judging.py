import enum
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from podium.models.base import Base, TimestampMixin, UTCDateTime, public_id_column, utcnow
from podium.models.events import Event, RubricCriterion
from podium.models.projects import Project
from podium.models.users import User


def _enum(e):
    return Enum(e, native_enum=False, length=20, validate_strings=True)


class AssignmentStatus(enum.StrEnum):
    pending = "pending"
    in_progress = "in_progress"
    done = "done"


class AssignmentMethod(enum.StrEnum):
    manual = "manual"
    auto = "auto"
    imported = "import"


class Assignment(TimestampMixin, Base):
    __tablename__ = "assignments"
    __table_args__ = (
        UniqueConstraint("judge_id", "project_id", name="uq_assignments_judge_project"),
        Index("ix_assignments_event_judge", "event_id", "judge_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    judge_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[AssignmentStatus] = mapped_column(
        _enum(AssignmentStatus), default=AssignmentStatus.pending, nullable=False
    )
    method: Mapped[AssignmentMethod] = mapped_column(_enum(AssignmentMethod), nullable=False)
    assigned_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    event: Mapped[Event] = relationship()
    judge: Mapped[User] = relationship(foreign_keys=[judge_id])
    project: Mapped[Project] = relationship()
    review: Mapped["Review | None"] = relationship(back_populates="assignment", uselist=False)


class ReviewStatus(enum.StrEnum):
    draft = "draft"
    submitted = "submitted"


class Review(TimestampMixin, Base):
    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("judge_id", "project_id", name="uq_reviews_judge_project"),
        Index("ix_reviews_event_judge", "event_id", "judge_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = public_id_column("rev")
    assignment_id: Mapped[int] = mapped_column(
        ForeignKey("assignments.id", ondelete="CASCADE"), unique=True
    )
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    judge_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    comment: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[ReviewStatus] = mapped_column(
        _enum(ReviewStatus), default=ReviewStatus.draft, nullable=False
    )
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    assignment: Mapped[Assignment] = relationship(back_populates="review")
    judge: Mapped[User] = relationship(foreign_keys=[judge_id])
    project: Mapped[Project] = relationship()
    items: Mapped[list["ScoreItem"]] = relationship(
        back_populates="review", cascade="all, delete-orphan"
    )


class ScoreItem(Base):
    __tablename__ = "score_items"
    __table_args__ = (
        UniqueConstraint("review_id", "criterion_id", name="uq_score_items_review_criterion"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[int] = mapped_column(ForeignKey("reviews.id", ondelete="CASCADE"), index=True)
    criterion_id: Mapped[int] = mapped_column(
        ForeignKey("rubric_criteria.id", ondelete="CASCADE"), index=True
    )
    value: Mapped[int] = mapped_column(Integer, nullable=False)

    review: Mapped[Review] = relationship(back_populates="items")
    criterion: Mapped[RubricCriterion] = relationship()


class ComparisonSource(enum.StrEnum):
    judge = "judge"
    derived = "derived"


class PairwiseComparison(Base):
    __tablename__ = "pairwise_comparisons"
    __table_args__ = (Index("ix_pairwise_event_judge", "event_id", "judge_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    judge_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_a_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    project_b_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    winner_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    skip_reason: Mapped[str | None] = mapped_column(String(80))
    source: Mapped[ComparisonSource] = mapped_column(
        _enum(ComparisonSource), default=ComparisonSource.judge, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
