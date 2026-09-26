"""The scoring engine on synthetic data and on the real fixture data."""

from dataclasses import dataclass

import pytest

from podium.models import Event, RubricCriterion, User
from podium.services import scoring
from podium.services.scoring import ReviewScore, normalize, raw_score


@dataclass
class Rev:
    id: int = 0


def crit(cid, weight=1.0, lo=1, hi=5):
    c = RubricCriterion(key=f"c{cid}", name=f"c{cid}", weight=weight, min_score=lo, max_score=hi)
    c.id = cid
    return c


def test_raw_score_weights_and_scales():
    criteria = [crit(1, 1.0), crit(2, 3.0), crit(3, 1.0, 0, 10)]
    assert raw_score({1: 5, 2: 5, 3: 10}, criteria) == 100.0
    assert raw_score({1: 1, 2: 1, 3: 0}, criteria) == 0.0
    # weight 3 criterion at max, others at min → 3/5 of the way
    assert raw_score({1: 1, 2: 5, 3: 0}, criteria) == pytest.approx(60.0)
    assert raw_score({}, criteria) is None
    assert raw_score({1: 3}, criteria) == pytest.approx(50.0), "unscored criteria are skipped"


def make(judge_id, project_id, raw):
    return ReviewScore(review=Rev(), judge_id=judge_id, project_id=project_id, raw=raw)


def users(*ids):
    out = {}
    for i in ids:
        u = User(email=f"j{i}@x", name=f"J{i}", password_hash="x")
        u.id = i
        out[i] = u
    return out


def test_flat_scorer_contributes_nothing():
    scores = [
        make(1, 1, 40),
        make(1, 2, 80),
        make(1, 3, 60),
        make(2, 1, 70),
        make(2, 2, 70),
        make(2, 3, 70),
    ]
    stats, mu, sigma = normalize(scores, users(1, 2))
    flat = next(s for s in stats if s.judge.id == 2)
    assert flat.flat is True and flat.n == 3
    assert all(s.z == 0.0 for s in scores if s.judge_id == 2)
    assert any(s.z != 0.0 for s in scores if s.judge_id == 1)


def test_single_review_judge_is_shrunk_not_exploded():
    scores = [make(1, 1, 40), make(1, 2, 80), make(2, 1, 60), make(2, 2, 90), make(3, 3, 95)]
    stats, mu, sigma = normalize(scores, users(1, 2, 3))
    lone = next(s for s in scores if s.judge_id == 3)
    assert abs(lone.z) < 5, "finite, moderate z for a judge with one review"
    st = next(s for s in stats if s.judge.id == 3)
    assert st.n == 1 and st.shrunk_std > 0


def test_harsh_and_generous_judges_converge_after_normalization():
    """Judge 2 scores exactly 20 points lower than judge 1 on the same three projects.
    Without shrinkage (k=0) their z-scores coincide; with the default shrinkage the gap
    shrinks but a little of the offset is kept (it may be real, not bias)."""
    pairs = ((1, 90), (2, 70), (3, 50))

    def build():
        return [make(1, p, v) for p, v in pairs] + [make(2, p, v - 20) for p, v in pairs]

    exact = build()
    normalize(exact, users(1, 2), k=0.0)
    z1 = {s.project_id: s.z for s in exact if s.judge_id == 1}
    z2 = {s.project_id: s.z for s in exact if s.judge_id == 2}
    for p in (1, 2, 3):
        assert z1[p] == pytest.approx(z2[p], abs=1e-9)
    assert z1[1] > z1[2] > z1[3]

    shrunk = build()
    _, mu, sigma = normalize(shrunk, users(1, 2))
    raw_gap = 20 / sigma
    s1 = {s.project_id: s.z for s in shrunk if s.judge_id == 1}
    s2 = {s.project_id: s.z for s in shrunk if s.judge_id == 2}
    for p in (1, 2, 3):
        assert abs(s1[p] - s2[p]) < raw_gap / 2, "at least half of the offset is removed"
    assert s1[1] > s1[2] > s1[3] and s2[1] > s2[2] > s2[3]


def test_fixture_data_end_to_end(db):
    event = db.query(Event).filter_by(public_id="evt_01").one()
    results = scoring.compute(db, event)
    assert len(results.reviews) == 126 and len(results.judges) == 30
    flat = [j for j in results.judges if j.flat]
    assert [j.judge.public_id for j in flat] == ["jdg_07"]
    ranked = [p for p in results.projects if p.rank_norm]
    assert ranked[0].rank_norm == 1 and len(ranked) == 41
    assert results.projects == sorted(
        results.projects, key=lambda p: (p.rank_norm or 10**9, -(p.n or 0), p.project.title.lower())
    )
    for p in results.projects:
        assert p.n >= 2 and 0 <= p.normalized <= 100 and p.disagreement is not None
    assert any(p.rank_delta for p in results.projects), "normalization changes some ranks"
