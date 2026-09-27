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
    assert r.status_code == 403, "submissions closed in the test above; only organizers now"
    r = c.post(f"/api/v1/events/{slug}/projects/{pid}/withdraw", headers=world["org"])
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


@pytest.fixture(scope="module")
def locked(app):
    """A scratch event with its own organizer and a one-member team with a submitted project."""
    from fastapi.testclient import TestClient

    client = TestClient(app)
    with get_sessionmaker()() as db:
        org = _cookie(db, "org-lock@example.test")
        dana = _cookie(db, "dana-lock@example.test")
    window = {
        "name": "Lock Hack",
        "is_public": True,
        "submissions_open_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "submissions_close_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
    }
    r = client.post("/api/v1/events", headers=org, json=window)
    assert r.status_code == 201, r.text
    slug = r.json()["event"]["slug"]
    r = client.post(f"/api/v1/events/{slug}/teams", headers=dana, json={"name": "Locksmiths"})
    assert r.status_code == 201, r.text
    r = client.post(
        f"/api/v1/events/{slug}/projects",
        headers=dana,
        json={"title": "Deadbolt", "summary": "Stays shut.", "submit": True},
    )
    assert r.status_code == 201, r.text
    csrf = client.get("/login").cookies.get("csrf")
    return {
        "client": client,
        "slug": slug,
        "pid": r.json()["project"]["id"],
        "org": org,
        "dana": dana,
        "window": window,
        "csrf": csrf,
    }


def _web_post(world, who, path):
    """An HTML form post (the project page's buttons and the dashboard's "Withdraw duplicate")."""
    return world["client"].post(
        path,
        headers={
            "X-CSRF-Token": world["csrf"],
            "Cookie": world[who]["Cookie"] + f"; csrf={world['csrf']}",
        },
        follow_redirects=False,
    )


def test_team_withdraws_and_restores_while_submissions_are_open(locked):
    c, base = locked["client"], f"/api/v1/events/{locked['slug']}/projects/{locked['pid']}"
    r = c.post(f"{base}/withdraw", headers=locked["dana"])
    assert r.status_code == 200 and r.json()["project"]["status"] == "withdrawn"
    r = c.post(f"{base}/restore", headers=locked["dana"])
    assert r.status_code == 200 and r.json()["project"]["status"] == "submitted"


def test_after_the_deadline_only_an_organizer_withdraws_or_restores(locked):
    c, slug, pid = locked["client"], locked["slug"], locked["pid"]
    base = f"/api/v1/events/{slug}/projects/{pid}"
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    r = c.patch(
        f"/api/v1/events/{slug}",
        headers=locked["org"],
        json={**locked["window"], "submissions_close_at": past},
    )
    assert r.status_code == 200 and r.json()["event"]["stage"] == "closed"
    r = c.post(f"{base}/withdraw", headers=locked["dana"])
    assert r.status_code == 403 and r.json()["error"]["code"] == "closed"
    r = _web_post(locked, "dana", f"/e/{slug}/projects/{pid}/withdraw")
    assert r.status_code == 403 and "only an organizer can withdraw" in r.text
    r = c.post(f"{base}/withdraw", headers=locked["org"])
    assert r.status_code == 200 and r.json()["project"]["status"] == "withdrawn"
    r = c.post(f"{base}/restore", headers=locked["dana"])
    assert r.status_code == 403, "a team can't undo an organizer's withdrawal after the deadline"
    r = c.post(f"{base}/restore", headers=locked["org"])
    assert r.status_code == 200 and r.json()["project"]["status"] == "submitted"


def test_nobody_withdraws_once_results_are_published(locked):
    c, slug, pid = locked["client"], locked["slug"], locked["pid"]
    for action in ("open_judging", "close_judging", "publish_results"):
        r = c.post(f"/api/v1/events/{slug}/actions/{action}", headers=locked["org"])
        assert r.status_code == 200, (action, r.text)
    for who in ("org", "dana"):
        r = c.post(f"/api/v1/events/{slug}/projects/{pid}/withdraw", headers=locked[who])
        assert r.status_code == 409, who
        assert r.json()["error"]["message"].startswith("Results are published"), who
    r = _web_post(locked, "org", f"/e/{slug}/projects/{pid}/withdraw")
    assert r.status_code == 409 and "Unpublish them before withdrawing" in r.text
