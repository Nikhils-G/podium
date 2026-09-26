from sqlalchemy import func, select

from podium.config import get_settings
from podium.models import Event, Project, Review, ScoreItem, Session, User
from podium.seed import run_seed


def test_fixtures_loaded_completely(db):
    event = db.execute(select(Event).where(Event.public_id == "evt_01")).scalar_one()
    count = lambda model: db.execute(  # noqa: E731
        select(func.count()).select_from(model).where(model.event_id == event.id)
    ).scalar()
    assert count(Project) == 41
    assert count(Review) == 126
    assert (
        db.execute(
            select(func.count())
            .select_from(ScoreItem)
            .join(Review, Review.id == ScoreItem.review_id)
            .where(Review.event_id == event.id)
        ).scalar()
        == 378
    )
    assert event.slug == "sample-hack-2026"
    assert event.submissions_close_at.isoformat() == "2026-03-01T18:00:00+00:00"


def test_duplicate_submission_is_flagged(db):
    dup = db.execute(select(Project).where(Project.public_id == "prj_41")).scalar_one()
    assert dup.duplicate_of is not None and dup.duplicate_of.public_id == "prj_07"


def test_seed_is_idempotent(db):
    before = {
        t: db.execute(select(func.count()).select_from(t)).scalar()
        for t in (User, Project, Review, ScoreItem, Session)
    }
    summary = run_seed(db, get_settings())
    after = {t: db.execute(select(func.count()).select_from(t)).scalar() for t in before}
    assert before == after
    assert summary.report is not None and summary.report.counts == {}
