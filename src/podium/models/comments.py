from datetime import datetime

from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from podium.models.base import Base, UTCDateTime, public_id_column, utcnow
from podium.models.projects import Project
from podium.models.users import User


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = public_id_column("cmt")
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    hidden_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    hidden_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    project: Mapped[Project] = relationship()
    user: Mapped[User] = relationship(foreign_keys=[user_id])
