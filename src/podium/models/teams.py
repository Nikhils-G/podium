import enum
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from podium.models.base import Base, TimestampMixin, UTCDateTime, public_id_column, utcnow
from podium.models.events import Event
from podium.models.users import User


class MemberRole(enum.StrEnum):
    lead = "lead"
    member = "member"


class Team(TimestampMixin, Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = public_id_column("tm")
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    invite_code: Mapped[str] = mapped_column(String(40), unique=True, index=True, nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    event: Mapped[Event] = relationship()
    members: Mapped[list["TeamMember"]] = relationship(
        back_populates="team", cascade="all, delete-orphan", order_by="TeamMember.joined_at"
    )


class TeamMember(Base):
    __tablename__ = "team_members"
    __table_args__ = (UniqueConstraint("team_id", "user_id", name="uq_team_members_team_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[MemberRole] = mapped_column(
        Enum(MemberRole, native_enum=False, length=20, validate_strings=True),
        default=MemberRole.member,
        nullable=False,
    )
    joined_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    team: Mapped[Team] = relationship(back_populates="members")
    user: Mapped[User] = relationship()
