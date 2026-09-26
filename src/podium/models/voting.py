from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from podium.models.base import Base, UTCDateTime, utcnow
from podium.models.projects import Project


class Vote(Base):
    __tablename__ = "votes"
    __table_args__ = (
        UniqueConstraint(
            "event_id", "project_id", "voter_key", name="uq_votes_event_project_voter"
        ),
        Index("ix_votes_event_project", "event_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    voter_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    voter_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    credits: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    ua_hash: Mapped[str | None] = mapped_column(String(64))
    flagged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    voided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    void_reason: Mapped[str | None] = mapped_column(String(200))

    project: Mapped[Project] = relationship()


class VoterLedger(Base):
    __tablename__ = "voter_ledger"
    __table_args__ = (UniqueConstraint("event_id", "voter_key", name="uq_voter_ledger_event_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    voter_key: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    email_hash: Mapped[str | None] = mapped_column(String(64))
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    credits_spent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class VoterCode(Base):
    __tablename__ = "voter_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(254))
    issued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
