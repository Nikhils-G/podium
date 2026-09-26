import enum
from datetime import datetime

from sqlalchemy import JSON, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from podium.models.base import Base, UTCDateTime, utcnow


class CertificateKind(enum.StrEnum):
    participation = "participation"
    judge = "judge"
    winner = "winner"


class Certificate(Base):
    __tablename__ = "certificates"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    kind: Mapped[CertificateKind] = mapped_column(
        Enum(CertificateKind, native_enum=False, length=20, validate_strings=True), nullable=False
    )
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"))
    serial: Mapped[str] = mapped_column(String(40), unique=True, index=True, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class InstanceSetting(Base):
    __tablename__ = "instance_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
