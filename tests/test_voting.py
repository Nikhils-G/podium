"""T3: voting modes, windows, dedupe, quadratic budgets, hidden tallies, stable ballots, abuse
handling and comments — on a fresh event created through the API."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from podium.security.sessions import create_session
from podium.services import voting
from podium.services.auth import register
from tests.conftest import get_sessionmaker


def _cookie(db, email):
    user = register(db, email=email, name=email.split("@")[0].title(), password="password123")
    token = create_session(db, user, days=1)
    db.commit()
    return {"Cookie": f"session={token}"}


@pytest.fixture(scope="module")
def world(app):
    client = TestClient(app)
    with get_sessionmaker()() as db:
        org = _cookie(db, "org-t3@example.test")
        voters = [_cookie(db, f"voter{i}-t3@example.test") for i in range(3)]
        maker = _cookie(db, "maker-t3@example.test")
    now = datetime.now(UTC)
    r = client.post(
        "/api/v1/events",
        headers=org,
        json={
            "name": "Vote Hack",
            "is_public": True,
            "submissions_open_at": (now - timedelta(days=2)).isoformat(),
            "submissions_close_at": (now + timedelta(days=1)).isoformat(),
            "voting_open_at": (now - timedelta(hours=1)).isoformat(),
            "voting_close_at": (now + timedelta(hours=1)).isoformat(),
        },
    )
    slug = r.json()["event"]["slug"]
    client.post(f"/api/v1/events/{slug}/teams", headers=maker, json={"name": "Makers"})
    pids = []
    for i in range(3):
        # one team can hold one project, so make a team per project
        h = maker if i == 0 else _cookie(get_sessionmaker()(), f"maker{i}-t3@example.test")
        if i:
            client.post(f"/api/v1/events/{slug}/teams", headers=h, json={"name": f"Makers {i}"})
        r = client.post(
            f"/api/v1/events/{slug}/projects",
            headers=h,
            json={"title": f"Entry {i}", "submit": True},
        )
        assert r.status_code == 201, r.text
        pids.append(r.json()["project"]["id"])
    return {"client": client, "slug": slug, "org": org, "voters": voters, "pids": pids, "now": now}


def _window(client, slug, org, now, *, open_now: bool):
    """Organizers must close the window before changing how people vote; tests do the same."""
    if open_now:
        opens, closes = now - timedelta(hours=1), now + timedelta(hours=1)
    else:
        opens, closes = now - timedelta(hours=3), now - timedelta(hours=2)
    r = client.patch(
        f"/api/v1/events/{slug}",
        headers=org,
        json={
            "name": "Vote Hack",
            "is_public": True,
            "submissions_open_at": (now - timedelta(days=2)).isoformat(),
            "submissions_close_at": (now + timedelta(days=1)).isoformat(),
            "voting_open_at": opens.isoformat(),
            "voting_close_at": closes.isoformat(),
        },
    )
    assert r.status_code == 200, r.text


def _switch(client, slug, org, now, **settings):
    _window(client, slug, org, now, open_now=False)
    r = client.patch(f"/api/v1/events/{slug}/voting", headers=org, json=settings)
    assert r.status_code == 200, r.text
    _window(client, slug, org, now, open_now=True)
    return r


def test_account_mode_one_vote_per_account_and_retract(world):
    c, slug, pid = world["client"], world["slug"], world["pids"][0]
    v = world["voters"][0]
    assert c.post(f"/api/v1/events/{slug}/projects/{pid}/votes").status_code == 401, (
        "account mode needs a login"
    )
    r = c.post(f"/api/v1/events/{slug}/projects/{pid}/votes", headers=v)
    assert r.status_code == 201 and r.json()["vote"]["my_votes"] == 1
    assert c.post(f"/api/v1/events/{slug}/projects/{pid}/votes", headers=v).status_code == 409
    mine = c.get(f"/api/v1/events/{slug}/votes/me", headers=v).json()["votes"]
    assert mine and all(k.startswith("prj_") for k in mine), "votes are keyed by public id"
    assert c.delete(f"/api/v1/events/{slug}/projects/{pid}/votes", headers=v).status_code == 200
    assert c.post(f"/api/v1/events/{slug}/projects/{pid}/votes", headers=v).status_code == 201


def test_tally_hidden_until_closed_and_published(world):
    c, slug = world["client"], world["slug"]
    assert c.get(f"/api/v1/events/{slug}/tally").status_code == 404
    assert c.get(f"/api/v1/events/{slug}/tally", headers=world["org"]).status_code == 200
    assert (
        c.get(f"/e/{slug}/results").status_code == 200
        and "aren't published" in c.get(f"/e/{slug}/results").text
    )
    r = c.post(f"/api/v1/events/{slug}/actions/publish_results", headers=world["org"])
    assert r.status_code == 409, "results can't be published while the vote is still running"
    assert c.get(f"/api/v1/events/{slug}/tally").status_code == 404, (
        "still voting → counts stay hidden"
    )


def test_quadratic_budget(world):
    c, slug, org = world["client"], world["slug"], world["org"]
    r = c.patch(
        f"/api/v1/events/{slug}/voting",
        headers=org,
        json={"quadratic_enabled": True, "voting_credits": 4},
    )
    assert r.status_code == 409, "votes are in and the window is open → settings are locked"
    r = _switch(c, slug, org, world["now"], quadratic_enabled=True, voting_credits=4)
    assert r.json()["quadratic_enabled"]
    v, pid = world["voters"][1], world["pids"][1]
    assert (
        c.post(f"/api/v1/events/{slug}/projects/{pid}/votes", headers=v).json()["vote"][
            "credits_spent"
        ]
        == 1
    )
    assert (
        c.post(f"/api/v1/events/{slug}/projects/{pid}/votes", headers=v).json()["vote"][
            "credits_spent"
        ]
        == 4
    )
    r = c.post(f"/api/v1/events/{slug}/projects/{pid}/votes", headers=v)
    assert r.status_code == 409 and "credits" in r.json()["error"]["message"]
    r = c.delete(f"/api/v1/events/{slug}/projects/{pid}/votes", headers=v)
    assert r.json()["vote"]["my_votes"] == 1 and r.json()["vote"]["credits_spent"] == 1
    _switch(c, slug, org, world["now"], quadratic_enabled=False)


def test_link_mode_uses_signed_anonymous_cookie(world):
    c, slug, org, pid = world["client"], world["slug"], world["org"], world["pids"][2]
    _switch(c, slug, org, world["now"], voting_mode="link")
    anon = TestClient(c.app)
    r = anon.post(f"/api/v1/events/{slug}/projects/{pid}/votes")
    assert r.status_code == 201 and "voter" in r.cookies
    assert anon.post(f"/api/v1/events/{slug}/projects/{pid}/votes").status_code == 409, (
        "same browser, one vote"
    )
    forged = TestClient(c.app)
    forged.cookies.set("voter", r.cookies["voter"][:-4] + "zzzz")
    r2 = forged.post(f"/api/v1/events/{slug}/projects/{pid}/votes")
    assert r2.status_code == 201 and r2.cookies["voter"] != r.cookies["voter"], (
        "tampered cookie is ignored"
    )
    _switch(c, slug, org, world["now"], voting_mode="account")


def test_code_mode(world):
    c, slug, org, pid = world["client"], world["slug"], world["org"], world["pids"][0]
    _switch(c, slug, org, world["now"], voting_mode="email")
    codes = c.post(
        f"/api/v1/events/{slug}/voting/codes",
        headers=org,
        json={"count": 2, "emails": ["a@x.test"]},
    ).json()["codes"]
    assert len(codes) == 2 and codes[0]["email"] == "a@x.test"
    guest = TestClient(c.app)
    assert guest.post(f"/api/v1/events/{slug}/projects/{pid}/votes").status_code == 401
    assert (
        guest.post(
            f"/api/v1/events/{slug}/voting/codes/redeem", params={"code": "NOPE-NOPE"}
        ).status_code
        == 404
    )
    assert (
        guest.post(
            f"/api/v1/events/{slug}/voting/codes/redeem", json={"code": codes[0]["code"]}
        ).status_code
        == 200
    )
    assert guest.post(f"/api/v1/events/{slug}/projects/{pid}/votes").status_code == 201
    # the same code on a second device is the same ballot: it re-redeems, it can't vote twice
    again = TestClient(c.app)
    relaxed = codes[0]["code"].replace("-", "").lower()
    assert (
        again.post(f"/api/v1/events/{slug}/voting/codes/redeem", json={"code": relaxed}).status_code
        == 200
    )
    assert again.post(f"/api/v1/events/{slug}/projects/{pid}/votes").status_code == 409
    _switch(c, slug, org, world["now"], voting_mode="account")


def test_ballot_order_is_stable_per_voter_and_differs_between_voters():
    class P:
        def __init__(self, i):
            self.id = i
            self.public_id = f"prj_{i}"

    projects = [P(i) for i in range(20)]
    a1 = [p.id for p in voting.ballot_order(projects, "usr:a", 1)]
    a2 = [p.id for p in voting.ballot_order(projects, "usr:a", 1)]
    b = [p.id for p in voting.ballot_order(projects, "usr:b", 1)]
    assert a1 == a2 and a1 != b and sorted(a1) == list(range(20))


def test_void_requires_reason_and_removes_from_tally(world):
    c, slug, org, pid = world["client"], world["slug"], world["org"], world["pids"][0]
    v = world["voters"][2]
    c.post(f"/api/v1/events/{slug}/projects/{pid}/votes", headers=v)
    with get_sessionmaker()() as db:
        from sqlalchemy import select

        from podium.models import Event, Vote

        event = db.execute(select(Event).where(Event.slug == slug)).scalar_one()
        vote = db.execute(
            select(Vote).where(
                Vote.event_id == event.id,
                Vote.voter_key
                == f"usr:{c.get('/api/v1/events/' + slug + '/votes/me', headers=v).json() and 'x'}",
            )
        ).scalar_one_or_none()
        vote = (
            db.execute(select(Vote).where(Vote.event_id == event.id).order_by(Vote.id.desc()))
            .scalars()
            .first()
        )
        vote_id = vote.id
    before = c.get(f"/api/v1/events/{slug}/tally", headers=org).json()["total_votes"]
    assert (
        c.post(
            f"/api/v1/events/{slug}/votes/{vote_id}/void", headers=org, json={"reason": ""}
        ).status_code
        == 422
    )
    assert (
        c.post(
            f"/api/v1/events/{slug}/votes/{vote_id}/void",
            headers=org,
            json={"reason": "duplicate person"},
        ).status_code
        == 200
    )
    after = c.get(f"/api/v1/events/{slug}/tally", headers=org).json()
    assert after["total_votes"] == before - 1 and after["voided"] >= 1
    assert (
        c.post(
            f"/api/v1/events/{slug}/votes/{vote_id}/void", headers=v, json={"reason": "x"}
        ).status_code
        == 403
    )


def test_voting_closed_refuses(world):
    c, slug, org, pid = world["client"], world["slug"], world["org"], world["pids"][1]
    now = world["now"]
    c.patch(
        f"/api/v1/events/{slug}",
        headers=org,
        json={
            "name": "Vote Hack",
            "is_public": True,
            "voting_open_at": (now - timedelta(hours=3)).isoformat(),
            "voting_close_at": (now - timedelta(hours=2)).isoformat(),
        },
    )
    r = c.post(f"/api/v1/events/{slug}/projects/{pid}/votes", headers=world["voters"][0])
    assert r.status_code == 403 and r.json()["error"]["code"] == "closed"
    assert c.post(f"/api/v1/events/{slug}/actions/publish_results", headers=org).status_code == 200
    assert c.get(f"/api/v1/events/{slug}/tally").status_code == 200, "closed + published → public"
    page = c.get(f"/e/{slug}/results").text
    assert "Full ranking" in page or "no reviews" in page


def test_comments_add_hide_and_limits(world):
    c, slug, org, pid = world["client"], world["slug"], world["org"], world["pids"][0]
    v = world["voters"][0]
    assert (
        c.post(f"/api/v1/events/{slug}/projects/{pid}/comments", json={"body": "hi"}).status_code
        == 401
    )
    r = c.post(
        f"/api/v1/events/{slug}/projects/{pid}/comments", headers=v, json={"body": "Love the idea."}
    )
    assert r.status_code == 201
    cid = r.json()["comment"]["id"]
    assert (
        c.post(
            f"/api/v1/events/{slug}/projects/{pid}/comments", headers=v, json={"body": "   "}
        ).status_code
        == 422
    )
    assert len(c.get(f"/api/v1/events/{slug}/projects/{pid}/comments").json()["comments"]) == 1
    assert (
        c.post(f"/api/v1/events/{slug}/projects/{pid}/comments/{cid}/hide", headers=v).status_code
        == 403
    )
    assert (
        c.post(f"/api/v1/events/{slug}/projects/{pid}/comments/{cid}/hide", headers=org).status_code
        == 200
    )
    assert c.get(f"/api/v1/events/{slug}/projects/{pid}/comments").json()["comments"] == []
    assert (
        len(c.get(f"/api/v1/events/{slug}/projects/{pid}/comments", headers=org).json()["comments"])
        == 1
    )
    c.patch(f"/api/v1/events/{slug}/voting", headers=org, json={"comments_enabled": False})
    assert (
        c.post(
            f"/api/v1/events/{slug}/projects/{pid}/comments", headers=v, json={"body": "x"}
        ).status_code
        == 403
    )


def test_team_member_cannot_vote_for_own_project(world):
    c, slug, org = world["client"], world["slug"], world["org"]
    pid = world["pids"][0]  # Entry 0 belongs to team Makers whose member is maker-t3
    _window(c, slug, org, world["now"], open_now=True)
    with get_sessionmaker()() as db:
        from sqlalchemy import select

        from podium.models import User
        from podium.security.sessions import create_session

        maker = db.execute(select(User).where(User.email == "maker-t3@example.test")).scalar_one()
        token = create_session(db, maker, days=1)
        db.commit()
    r = c.post(
        f"/api/v1/events/{slug}/projects/{pid}/votes", headers={"Cookie": f"session={token}"}
    )
    assert r.status_code == 403 and "own team" in r.json()["error"]["message"]


def test_ballot_is_shuffled_before_paging_and_fixed_per_visitor(world, db):
    from podium.models import Event
    from podium.services import projects as projects_service

    event = db.execute(select(Event).where(Event.slug == "sample-hack-2026")).scalar_one()
    pages = [
        projects_service.gallery(db, event, ballot_key="anon:x", page=n, page_size=10)
        for n in range(1, 6)
    ]
    seen = [c.public_id for pg in pages for c in pg.cards]
    assert len(seen) == 41 and len(set(seen)) == 41, "every page together is a permutation"
    again = projects_service.gallery(db, event, ballot_key="anon:x", page=1, page_size=10)
    assert [c.public_id for c in again.cards] == seen[:10], "same key, same order"
    other = projects_service.gallery(db, event, ballot_key="anon:y", page=1, page_size=10)
    assert [c.public_id for c in other.cards] != seen[:10], "another visitor, another order"
    titled = projects_service.gallery(db, event, ballot_key="anon:x", sort="title", page_size=10)
    assert [c.public_id for c in titled.cards] == seen[:10] and titled.sort == "ballot"
    # over the web, during the open window, each visitor gets a signed cookie and a fixed order
    c, slug, org = world["client"], world["slug"], world["org"]
    _window(c, slug, org, world["now"], open_now=True)
    first = TestClient(c.app)
    r = first.get(f"/e/{slug}/projects")
    assert r.status_code == 200 and "voter" in r.cookies and "Ballot order" in r.text
    assert 'id="gallery-sort"' not in r.text
    order = re.findall(r'/e/[^/]+/projects/(prj_[a-z0-9]+)"', r.text)
    r2 = first.get(f"/e/{slug}/projects?sort=title")
    assert re.findall(r'/e/[^/]+/projects/(prj_[a-z0-9]+)"', r2.text) == order
    second = TestClient(c.app)
    r3 = second.get(f"/e/{slug}/projects")
    assert r3.cookies["voter"] != first.cookies["voter"]
    _window(c, slug, org, world["now"], open_now=False)
    closed = TestClient(c.app).get(f"/e/{slug}/projects")
    assert "voter" not in closed.cookies and 'id="gallery-sort"' in closed.text
    _window(c, slug, org, world["now"], open_now=True)


def test_locked_voting_settings_are_refused_inline(world, app):
    from tests.test_forms_sweep import Browser

    slug, org = world["slug"], world["org"]
    token = org["Cookie"].split("=", 1)[1]
    browser = Browser(app, token)
    page = browser.get(f"/e/{slug}/organizer/voting")
    assert page.status_code == 200 and "locked until the window closes" in page.text
    r = browser.post(f"/e/{slug}/organizer/voting", {"voting_mode": "link", "voting_credits": "5"})
    assert r.status_code == 409 and "Community voting" in r.text, "inline, on the console page"
    assert "Close the window" in r.text
