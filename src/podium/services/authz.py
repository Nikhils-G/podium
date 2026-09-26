"""The single place that decides who may see whose judging data."""

from sqlalchemy import Select, select
from sqlalchemy.orm import Session as DbSession

from podium.errors import Forbidden
from podium.models import Event, Review, User
from podium.security.deps import is_organizer


def reviews_visible_to(db: DbSession, event: Event, user: User) -> Select:
    """Reviews in this event the user may read: their own, or all of them for organizers/admins."""
    query = select(Review).where(Review.event_id == event.id)
    if is_organizer(db, event, user):
        return query
    return query.where(Review.judge_id == user.id)


def assert_can_view_judge_reviews(db: DbSession, event: Event, actor: User, judge: User) -> None:
    """A judge may only read their own reviews. Organizers and admins may read anyone's."""
    if actor.id == judge.id or is_organizer(db, event, actor):
        return
    raise Forbidden("Judges can only see their own reviews.")
