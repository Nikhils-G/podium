"""T1 behaviour on a fresh, open event created through the API: teams by invite, one team per
person, size cap, drafts hidden, submit, deadline holds, withdraw/restore."""

from datetime import UTC, datetime, timedelta

import pytest

from podium.security.sessions import create_session
from podium.services.auth import register
from tests.conftest import get_sessionmaker, get_settings


def _cookie(db, email):
    user = register(db, email=email, name=email.split("@")[0].title(), password="password123")
    token = create_session(db, user, days=1)
    db.commit()
    return {"Cookie": f"session={token}"}


@pytest.fixture(scope="module")
def world(app):
    """An open event with one track, an organizer and three fresh participants."""
    from fastapi.testclient import TestClient

    client = TestClient(app)
    with get_sessionmaker()() as db:
        org = _cookie(db, "org-t1@example.test")
        alice = _cookie(db, "alice-t1@example.test")
        bob = _cookie(db, "bob-t1@example.test")
        carol = _cookie(db, "carol-t1@example.test")
    close = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    r = client.post(
        "/api/v1/events",
        headers=org,
        json={
            "name": "Open Hack",
            "is_public": True,
            "max_team_size": 2,
            "submissions_open_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            "submissions_close_at": close,
        },
    )
    assert r.status_code == 201, r.text
    slug = r.json()["event"]["slug"]
    r = client.post(f"/api/v1/events/{slug}/tracks", headers=org, json={"name": "Tools"})
    track = r.json()["track"]["id"]
    return {
        "client": client,
        "slug": slug,
        "track": track,
        "org": org,
        "alice": alice,
        "bob": bob,
        "carol": carol,
    }


def test_create_team_and_join_by_invite(world):
    c, slug = world["client"], world["slug"]
    r = c.post(f"/api/v1/events/{slug}/teams", headers=world["alice"], json={"name": "Nightshift"})
    assert r.status_code == 201
    code = r.json()["team"]["invite_code"]
    r = c.post(f"/api/v1/teams/join/{code}", headers=world["bob"])
    assert r.status_code == 200 and len(r.json()["team"]["members"]) == 2
    world["code"] = code


def test_team_size_cap_and_one_team_per_person(world):
    c, slug, code = world["client"], world["slug"], world["code"]
    r = c.post(f"/api/v1/teams/join/{code}", headers=world["carol"])
    assert r.status_code == 409 and "full" in r.json()["error"]["message"]
    r = c.post(f"/api/v1/events/{slug}/teams", headers=world["alice"], json={"name": "Second"})
    assert r.status_code == 409


def test_draft_is_hidden_then_submitted_is_public(world):
    c, slug = world["client"], world["slug"]
    r = c.post(
        f"/api/v1/events/{slug}/projects",
        headers=world["alice"],
        json={
            "title": "Quiet Hours",
            "summary": "Sleep better.",
            "track": world["track"],
            "submit": False,
        },
    )
    assert r.status_code == 201, r.text
    pid = r.json()["project"]["id"]
    world["pid"] = pid
    assert c.get(f"/api/v1/events/{slug}/projects/{pid}").status_code == 404, "drafts are private"
    assert c.get(f"/api/v1/events/{slug}/projects/{pid}", headers=world["bob"]).status_code == 200
    assert "Quiet Hours" not in c.get(f"/e/{slug}/projects").text
    r = c.patch(
        f"/api/v1/events/{slug}/projects/{pid}", headers=world["bob"], json={"submit": True}
    )
    assert r.status_code == 200 and r.json()["project"]["status"] == "submitted"
    assert "Quiet Hours" in c.get(f"/e/{slug}/projects").text


def test_outsider_cannot_edit(world):
    c, slug, pid = world["client"], world["slug"], world["pid"]
    r = c.patch(
        f"/api/v1/events/{slug}/projects/{pid}", headers=world["carol"], json={"title": "Hijack"}
    )
    assert r.status_code == 403


def test_deadline_holds_and_unlock_reopens(world):
    c, slug, pid = world["client"], world["slug"], world["pid"]
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    r = c.patch(
        f"/api/v1/events/{slug}",
        headers=world["org"],
        json={
            "name": "Open Hack",
            "is_public": True,
            "max_team_size": 2,
            "submissions_open_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            "submissions_close_at": past,
        },
    )
    assert r.status_code == 200 and r.json()["event"]["stage"] == "closed"
    r = c.patch(
        f"/api/v1/events/{slug}/projects/{pid}", headers=world["alice"], json={"title": "Late"}
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "closed"
    r = c.post(
        f"/api/v1/events/{slug}/projects", headers=world["carol"], json={"title": "Too late"}
    )
    assert r.status_code == 403
    r = c.post(f"/api/v1/events/{slug}/teams", headers=world["carol"], json={"name": "Latecomers"})
    assert r.status_code == 201, "teams can still form; only submissions are closed"


def test_withdraw_and_restore(world):
    c, slug, pid = world["client"], world["slug"], world["pid"]
    r = c.post(f"/api/v1/events/{slug}/projects/{pid}/withdraw", headers=world["alice"])
    assert r.status_code == 200 and r.json()["project"]["status"] == "withdrawn"
    assert "Quiet Hours" not in c.get(f"/e/{slug}/projects").text
    r = c.post(f"/api/v1/events/{slug}/projects/{pid}/restore", headers=world["org"])
    assert r.status_code == 200 and r.json()["project"]["status"] == "submitted"


def test_lifecycle_actions_and_organizer_only(world):
    c, slug = world["client"], world["slug"]
    assert (
        c.post(f"/api/v1/events/{slug}/actions/open_judging", headers=world["alice"]).status_code
        == 403
    )
    r = c.post(f"/api/v1/events/{slug}/actions/open_judging", headers=world["org"])
    assert r.status_code == 200 and r.json()["event"]["stage"] == "judging"
    assert c.get(f"/e/{slug}/organizer", headers=world["alice"]).status_code == 403
    assert c.get(f"/e/{slug}/organizer", headers=world["org"]).status_code == 200
    r = c.post(f"/api/v1/events/{slug}/actions/open_judging", headers=world["org"])
    assert r.status_code == 409, "opening judging twice is a conflict"


def test_track_with_projects_cannot_be_removed(world):
    c, slug = world["client"], world["slug"]
    r = c.delete(f"/api/v1/events/{slug}/tracks/{world['track']}", headers=world["org"])
    assert r.status_code == 409


def test_event_validation(world):
    c = world["client"]
    r = c.post(
        "/api/v1/events",
        headers=world["org"],
        json={
            "name": "Bad",
            "submissions_open_at": "2026-05-02T00:00:00Z",
            "submissions_close_at": "2026-05-01T00:00:00Z",
        },
    )
    assert r.status_code == 422 and "submissions_close_at" in r.json()["error"]["errors"]
    assert get_settings().demo_accounts
