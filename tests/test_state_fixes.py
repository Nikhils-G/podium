"""State fixes: removing an assignment or a judge with a draft review, leaving a team that
owns two projects, and the results 'tied' flag on the raw ranking basis."""

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from podium.models import Event, EventRole, Project, RankingBasis, Review, Role, Team, User
from podium.security.sessions import create_session
from podium.services.auth import register
from tests.conftest import ROOT, SLUG, get_sessionmaker


def _cookie(db, email):
    user = register(db, email=email, name=email.split("@")[0].title(), password="password123")
    token = create_session(db, user, days=1)
    db.commit()
    return user, {"Cookie": f"session={token}"}


def _event(client, org, name):
    now = datetime.now(UTC)
    r = client.post(
        "/api/v1/events",
        headers=org,
        json={
            "name": name,
            "is_public": True,
            "submissions_open_at": (now - timedelta(days=1)).isoformat(),
            "submissions_close_at": (now + timedelta(days=1)).isoformat(),
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["event"]["slug"]


def _judge_with_draft(client, tag):
    """A scratch event where one judge has saved a partial draft (assignment in_progress)."""
    with get_sessionmaker()() as db:
        _, org = _cookie(db, f"org-{tag}@example.test")
        judge, judge_h = _cookie(db, f"judge-{tag}@example.test")
        _, maker = _cookie(db, f"maker-{tag}@example.test")
        judge_public_id = judge.public_id
    slug = _event(client, org, f"Draft Hack {tag}")
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
    return S, org, judge_public_id, assignment["id"], review_id


def _review_gone(review_id):
    with get_sessionmaker()() as db:
        return db.execute(select(Review).where(Review.public_id == review_id)).first() is None


def test_removing_an_assignment_discards_the_draft_review(client):
    S, org, _, assignment_id, review_id = _judge_with_draft(client, "state")
    r = client.delete(f"{S}/assignments/{assignment_id}", headers=org)
    assert r.status_code == 204, r.text
    assert _review_gone(review_id)


def test_removing_a_judge_discards_their_draft_review(client):
    S, org, judge_public_id, _, review_id = _judge_with_draft(client, "state-rm-judge")
    r = client.delete(f"{S}/judges/{judge_public_id}", headers=org)
    assert r.status_code == 204, r.text
    assert _review_gone(review_id)


def test_last_member_leaving_deletes_every_draft_project(client):
    with get_sessionmaker()() as db:
        _, org = _cookie(db, "org-state-leave@example.test")
        _, maker = _cookie(db, "maker-state-leave@example.test")
    slug = _event(client, org, "Two Drafts Hack")
    S = f"/api/v1/events/{slug}"
    r = client.post(f"{S}/teams", headers=maker, json={"name": "Two Drafts"})
    assert r.status_code == 201, r.text
    r = client.post(f"{S}/projects", headers=maker, json={"title": "Draft one", "submit": False})
    assert r.status_code == 201, r.text
    with get_sessionmaker()() as db:
        first = db.execute(select(Project).where(Project.public_id == r.json()["project"]["id"]))
        first = first.scalar_one()
        team_id = first.team_id
        # imports can give a team a second project; the API never does
        db.add(Project(event_id=first.event_id, team_id=team_id, title="Draft two"))
        db.commit()
    r = client.post(f"{S}/teams/leave", headers=maker)
    assert r.status_code == 204, r.text
    with get_sessionmaker()() as db:
        assert db.get(Team, team_id) is None
        assert db.execute(select(Project).where(Project.team_id == team_id)).first() is None


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
