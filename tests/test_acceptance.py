"""The seven acceptance checks from tools/run.py, reproduced in-process so a regression on any
checked route fails the test suite before it fails the judges' run."""

import json

from tests.conftest import ROOT, SLUG

FIXTURE = json.loads((ROOT / "fixtures" / "fixtures.json").read_text())
PROBE = {"title": "dogfood-late-submission-probe", "summary": "probe"}


def test_t1_gallery_is_public(client):
    assert client.get(f"/e/{SLUG}/projects").status_code == 200


def test_t1_gallery_shows_fixture_projects(client):
    body = client.get(f"/e/{SLUG}/projects").text.lower()
    titles = [p["title"].lower() for p in FIXTURE["projects"][:3]]
    assert all(t in body for t in titles), "first three fixture titles must be on page one"


def test_t1_closed_event_refuses_submissions(client, auth):
    r = client.post(f"/api/v1/events/{SLUG}/projects", headers=auth("participant"), json=PROBE)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "closed"


def test_t1_closed_event_refuses_even_a_malformed_body(client, auth):
    r = client.post(f"/api/v1/events/{SLUG}/projects", headers=auth("participant"), json={"x": 1})
    assert r.status_code == 403, "the window check must run before body validation (never 422)"


def test_t1_anonymous_cannot_submit(client):
    r = client.post(f"/api/v1/events/{SLUG}/projects", json=PROBE)
    assert r.status_code == 401


def test_t2_judge_sees_own_scores(client, auth):
    r = client.get(f"/api/v1/events/{SLUG}/judges/me/reviews", headers=auth("judge_a"))
    assert r.status_code == 200


def test_t2_judge_cannot_see_peer_scores(client, auth):
    r = client.get(f"/api/v1/events/{SLUG}/judges/jdg_24/reviews", headers=auth("judge_b"))
    assert r.status_code in (401, 403)


def test_t2_participant_blocked(client, auth):
    r = client.get(f"/api/v1/events/{SLUG}/judges/me/reviews", headers=auth("participant"))
    assert r.status_code in (401, 403)


def test_t2_csv_export_works(client, auth):
    r = client.get(f"/api/v1/events/{SLUG}/exports/scores.csv", headers=auth("organizer"))
    assert r.status_code == 200
    assert "," in r.text.splitlines()[0]


def test_signing_out_of_a_demo_account_never_breaks_the_checker(client, auth):
    """Demo tokens are shared and deterministic; a judge clicking "Sign out" must not revoke the
    session the committed .dogfood.toml depends on."""
    csrf = client.get("/login").cookies.get("csrf")
    r = client.post(
        "/logout",
        headers={
            **auth("organizer"),
            "X-CSRF-Token": csrf,
            "Cookie": auth("organizer")["Cookie"] + f"; csrf={csrf}",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert (
        client.get(
            f"/api/v1/events/{SLUG}/exports/scores.csv", headers=auth("organizer")
        ).status_code
        == 200
    )
