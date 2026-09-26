import secrets
from datetime import UTC, datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o/1/l/i, easy to read aloud


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_public_id(prefix: str, length: int = 8) -> str:
    return f"{prefix}_{''.join(secrets.choice(_ALPHABET) for _ in range(length))}"


class UTCDateTime(TypeDecorator):
    """Store naive UTC in the database; always hand back timezone-aware UTC in Python."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime given; Podium only accepts timezone-aware datetimes")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


def public_id_column(prefix: str) -> Mapped[str]:
    return mapped_column(
        String(40), unique=True, index=True, nullable=False, default=lambda: new_public_id(prefix)
    )
