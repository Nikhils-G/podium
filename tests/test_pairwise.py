from sqlalchemy import select

from podium.models import Event
from podium.services import pairwise
from podium.services.pairwise import bradley_terry, spearman
from tests.conftest import SLUG, get_sessionmaker

S = f"/api/v1/events/{SLUG}"


def test_bradley_terry_recovers_a_clear_ordering():
    items = [1, 2, 3, 4]
    comparisons = []
    for a in items:
        for b in items:
            if a < b:
                comparisons += [(a, b)] * 3 + [(b, a)]  # lower id wins 3 of 4
    strengths = bradley_terry(items, comparisons)
    assert strengths[1] > strengths[2] > strengths[3] > strengths[4]
    assert abs(sum(strengths.values())) < 1e-6, "normalised to geometric mean 1"
    lonely = bradley_terry([1, 2, 9], [(1, 2)])
    assert lonely[9] == 0.0 or abs(lonely[9]) < 0.5, "an uncompared item sits at the reference"


def test_spearman():
    assert spearman({1: 1, 2: 2, 3: 3}, {1: 1, 2: 2, 3: 3}) == 1.0
    assert spearman({1: 1, 2: 2, 3: 3}, {1: 3, 2: 2, 3: 1}) == -1.0
    assert spearman({1: 1, 2: 2}, {1: 1, 2: 2}) is None


def test_fixture_seed_derives_comparisons_and_ranks(db):
    event = db.execute(select(Event).where(Event.slug == SLUG)).scalar_one()
    r = pairwise.results(db, event)
    assert r.derived_comparisons > 200 and r.judge_comparisons == 0
    assert len(r.rows) == 41 and r.rows[0].rank == 1
    assert r.rho is not None and 0.5 < r.rho <= 1.0, (
        "pairwise agrees broadly with normalized scores"
    )


def test_judge_compare_flow(client, auth):
    state = client.get(f"{S}/judges/me/compare", headers=auth("judge_a")).json()
    assert state["total"] == 55 and state["pair"]  # 11 projects → 55 pairs
    before = state["done"]  # derived comparisons from the fixture scores already cover some pairs
    assert before < 55
    a, b = state["pair"][0]["project"], state["pair"][1]["project"]
    r = client.post(f"{S}/comparisons", headers=auth("judge_a"), json={"a": a, "b": b, "winner": b})
    assert r.status_code == 201
    assert (
        client.get(f"{S}/judges/me/compare", headers=auth("judge_a")).json()["done"] == before + 1
    )
    r = client.post(
        f"{S}/comparisons",
        headers=auth("judge_a"),
        json={"a": a, "b": b, "skip_reason": "conflict"},
    )
    assert r.status_code == 201 and r.json()["comparison"]["skip_reason"] == "conflict"
    r = client.post(
        f"{S}/comparisons", headers=auth("judge_a"), json={"a": a, "b": "prj_01", "winner": a}
    )
    assert r.status_code == 403, "prj_01 isn't assigned to judge_a"
    r = client.post(
        f"{S}/comparisons", headers=auth("judge_a"), json={"a": a, "b": b, "winner": "prj_01"}
    )
    assert r.status_code == 422
    assert (
        client.post(
            f"{S}/comparisons", headers=auth("participant"), json={"a": a, "b": b, "winner": a}
        ).status_code
        == 403
    )
    page = client.get(f"/e/{SLUG}/judge/compare", headers=auth("judge_a"))
    assert page.status_code == 200 and "Which is stronger" in page.text
    assert client.get(f"{S}/results/pairwise").status_code == 404
    assert (
        client.get(f"{S}/results/pairwise", headers=auth("organizer")).json()["judge_comparisons"]
        >= 2
    )
    with get_sessionmaker()() as db:
        event = db.execute(select(Event).where(Event.slug == SLUG)).scalar_one()
        from podium.models import User

        judge = db.execute(select(User).where(User.public_id == "jdg_24")).scalar_one()
        assert pairwise.undo_last(db, event, judge) is True
