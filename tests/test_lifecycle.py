from datetime import UTC, datetime, timedelta

from podium.models import Event
from podium.services.events import Stage, stage_of

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def make(**kwargs) -> Event:
    event = Event(slug="x", name="X", is_public=True)
    for key, value in kwargs.items():
        setattr(event, key, value)
    return event


def test_draft_when_not_public():
    assert stage_of(make(is_public=False), NOW) == Stage.draft


def test_upcoming_before_open():
    assert stage_of(make(submissions_open_at=NOW + timedelta(days=1)), NOW) == Stage.upcoming


def test_open_inside_window():
    e = make(
        submissions_open_at=NOW - timedelta(days=1), submissions_close_at=NOW + timedelta(days=1)
    )
    assert stage_of(e, NOW) == Stage.open


def test_closed_exactly_at_deadline():
    e = make(submissions_close_at=NOW)
    assert stage_of(e, NOW) == Stage.closed, "closed at >= close, not after"


def test_judging_overrides_closed():
    e = make(
        submissions_close_at=NOW - timedelta(days=1), judging_opened_at=NOW - timedelta(hours=1)
    )
    assert stage_of(e, NOW) == Stage.judging


def test_voting_window_wins_over_judging():
    e = make(
        judging_opened_at=NOW - timedelta(days=1),
        voting_open_at=NOW - timedelta(hours=1),
        voting_close_at=NOW + timedelta(hours=1),
    )
    assert stage_of(e, NOW) == Stage.voting


def test_published_and_archived_win():
    assert stage_of(make(results_published_at=NOW), NOW) == Stage.published
    assert stage_of(make(results_published_at=NOW, archived_at=NOW), NOW) == Stage.archived
