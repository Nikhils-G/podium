"""State fixes: removing an assignment with a draft review, leaving a team that owns two
projects, and the results 'tied' flag on the raw ranking basis."""

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from podium.models import Event, EventRole, RankingBasis, Review, Role, User
from podium.security.sessions import create_session
from podium.services.auth import register
from tests.conftest import ROOT, SLUG, get_sessionmaker


def _cookie(db, email):
    user = register(db, email=email, name=email.split("@")[0].title(), password="password123")
    token = create_session(db, user, days=1)
    db.commit()
    return user, {"Cookie": f"session={token}"}


def test_removing_an_assignment_discards_the_draft_review(client):
    now = datetime.now(UTC)
    with get_sessionmaker()() as db:
        _, org = _cookie(db, "org-state@example.test")
        judge, judge_h = _cookie(db, "judge-state@example.test")
        _, maker = _cookie(db, "maker-state@example.test")
        judge_public_id = judge.public_id
    r = client.post(
        "/api/v1/events",
        headers=org,
        json={
            "name": "Draft Removal Hack",
            "is_public": True,
            "submissions_open_at": (now - timedelta(days=1)).isoformat(),
            "submissions_close_at": (now + timedelta(days=1)).isoformat(),
        },
    )
    assert r.status_code == 201, r.text
    slug = r.json()["event"]["slug"]
    S = f"/api/v1/events/{slug}"
    with get_sessionmaker()() as db:
        event = db.execute(select(Event).where(Event.slug == slug)).scalar_one()
        user = db.execute(select(User).where(User.public_id == judge_public_id)).scalar_one()
        db.add(EventRole(event_id=event.id, user_id=user.id, role=Role.judge))
        db.commit()
    assert client.post(f"{S}/teams", headers=maker, json={"name": "Drafters"}).status_code == 201
    r = client.post(f"{S}/projects", headers=maker, json={"title": "Half Scored", "submit": True})
    assert r.status_code == 201, r.text
    pid = r.json()["project"]["id"]
    r = client.post(f"{S}/rubric", headers=org, json={"name": "Craft"})
    assert r.status_code == 201, r.text
    crit = r.json()["criterion"]["id"]
    r = client.post(
        f"{S}/assignments", headers=org, json={"judge": judge_public_id, "projects": [pid]}
    )
    assert r.status_code == 201, r.text
    assert client.post(f"{S}/actions/open_judging", headers=org).status_code == 200
    r = client.put(f"{S}/reviews/{pid}", headers=judge_h, json={"scores": {crit: 3}})
    assert r.status_code == 200, r.text
    review_id = r.json()["review"]["id"]
    judges = client.get(f"{S}/assignments", headers=org).json()["judges"]
    assignment = [a for j in judges if j["id"] == judge_public_id for a in j["assignments"]][0]
    assert assignment["status"] == "in_progress"
    r = client.delete(f"{S}/assignments/{assignment['id']}", headers=org)
    assert r.status_code == 204, r.text
    with get_sessionmaker()() as db:
        assert db.execute(select(Review).where(Review.public_id == review_id)).first() is None


def test_leaving_a_team_with_two_projects_is_refused_not_a_crash(client):
    fixtures = json.loads((ROOT / "fixtures" / "fixtures.json").read_text())
    email = [t for t in fixtures["teams"] if t["id"] == "tm_07"][0]["members"][1]
    assert len([p for p in fixtures["projects"] if p["team"] == "tm_07"]) == 2
    with get_sessionmaker()() as db:
        user = db.execute(select(User).where(User.email == email)).scalar_one()
        token = create_session(db, user, days=1)
        db.commit()
    r = client.post(f"/api/v1/events/{SLUG}/teams/leave", headers={"Cookie": f"session={token}"})
    assert r.status_code == 409, r.text


def test_results_tied_follows_the_raw_basis(client, auth):
    with get_sessionmaker()() as db:
        event = db.execute(select(Event).where(Event.slug == SLUG)).scalar_one()
        before = event.published_ranking
        event.published_ranking = RankingBasis.raw
        db.commit()
    try:
        body = client.get(f"/api/v1/events/{SLUG}/results", headers=auth("organizer")).json()
        assert body["basis"] == "raw"
        ranks = [p["rank_raw"] for p in body["projects"]]
        for p in body["projects"]:
            shared = p["rank_raw"] is not None and ranks.count(p["rank_raw"]) > 1
            assert p["tied"] == shared, p
    finally:
        with get_sessionmaker()() as db:
            event = db.execute(select(Event).where(Event.slug == SLUG)).scalar_one()
            event.published_ranking = before
            db.commit()
