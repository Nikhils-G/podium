import enum
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from podium.models.base import Base, TimestampMixin, UTCDateTime, public_id_column
from podium.models.events import Event, Track
from podium.models.teams import Team


class ProjectStatus(enum.StrEnum):
    draft = "draft"
    submitted = "submitted"
    withdrawn = "withdrawn"


class Project(TimestampMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (Index("ix_projects_event_status", "event_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = public_id_column("prj")
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    track_id: Mapped[int | None] = mapped_column(ForeignKey("tracks.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(140), nullable=False)
    summary: Mapped[str] = mapped_column(String(280), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    repo_url: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    demo_url: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    video_url: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    status: Mapped[ProjectStatus] = mapped_column(
        Enum(ProjectStatus, native_enum=False, length=20, validate_strings=True),
        default=ProjectStatus.draft,
        nullable=False,
    )
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    withdrawn_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    unlocked_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    duplicate_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL")
    )

    event: Mapped[Event] = relationship()
    team: Mapped[Team] = relationship()
    track: Mapped[Track | None] = relationship()
    duplicate_of: Mapped["Project | None"] = relationship(remote_side=[id])
