import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from podium.models.base import Base, TimestampMixin, UTCDateTime, public_id_column, utcnow
from podium.models.users import User


class Role(enum.StrEnum):
    participant = "participant"
    judge = "judge"
    organizer = "organizer"


class VotingMode(enum.StrEnum):
    link = "link"
    email = "email"
    account = "account"


class NormalizationMethod(enum.StrEnum):
    none = "none"
    zscore = "zscore"


class RankingBasis(enum.StrEnum):
    raw = "raw"
    normalized = "normalized"


def _enum(e: type[enum.StrEnum]):
    return Enum(e, native_enum=False, length=20, validate_strings=True)


class Event(TimestampMixin, Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = public_id_column("evt")
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    submissions_open_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    submissions_close_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    judging_opened_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    judging_closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    voting_open_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    voting_close_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    results_published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    voting_mode: Mapped[VotingMode] = mapped_column(
        _enum(VotingMode), default=VotingMode.account, nullable=False
    )
    quadratic_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    voting_credits: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    comments_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    reviews_per_project: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    max_team_size: Mapped[int] = mapped_column(
        Integer, default=4, server_default="4", nullable=False
    )
    normalization_method: Mapped[NormalizationMethod] = mapped_column(
        _enum(NormalizationMethod), default=NormalizationMethod.zscore, nullable=False
    )
    published_ranking: Mapped[RankingBasis] = mapped_column(
        _enum(RankingBasis), default=RankingBasis.normalized, nullable=False
    )
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    tracks: Mapped[list["Track"]] = relationship(
        back_populates="event", order_by="Track.position", cascade="all, delete-orphan"
    )
    prizes: Mapped[list["Prize"]] = relationship(
        back_populates="event", order_by="Prize.position", cascade="all, delete-orphan"
    )
    roles: Mapped[list["EventRole"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )


class EventRole(Base):
    __tablename__ = "event_roles"
    __table_args__ = (UniqueConstraint("event_id", "user_id", name="uq_event_roles_event_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[Role] = mapped_column(_enum(Role), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    event: Mapped[Event] = relationship(back_populates="roles")
    user: Mapped[User] = relationship()


class JudgeInvite(Base):
    __tablename__ = "judge_invites"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    track_public_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    invited_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    accepted_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class JudgeTrack(Base):
    __tablename__ = "judge_tracks"
    __table_args__ = (UniqueConstraint("event_id", "user_id", "track_id", name="uq_judge_tracks"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"), index=True)


class Track(Base):
    __tablename__ = "tracks"
    __table_args__ = (UniqueConstraint("event_id", "name", name="uq_tracks_event_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = public_id_column("trk")
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    event: Mapped[Event] = relationship(back_populates="tracks")


class Prize(Base):
    __tablename__ = "prizes"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = public_id_column("prz")
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    amount_text: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    track_id: Mapped[int | None] = mapped_column(ForeignKey("tracks.id", ondelete="SET NULL"))
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    event: Mapped[Event] = relationship(back_populates="prizes")
    track: Mapped[Track | None] = relationship()


class RubricCriterion(Base):
    __tablename__ = "rubric_criteria"
    __table_args__ = (UniqueConstraint("event_id", "key", name="uq_rubric_event_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = public_id_column("crt")
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    min_score: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    max_score: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    event: Mapped[Event] = relationship()
