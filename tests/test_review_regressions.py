"""Regression tests for the defects found in the second judge review (27 Sep 2026).
Each one failed on 4d02753; they are kept as written by the reviewer, adapted to our fixtures."""

from sqlalchemy import select

from podium.models import Event
from tests.conftest import SLUG, get_sessionmaker
from tests.test_forms_sweep import demo

S = f"/api/v1/events/{SLUG}"


def _event():
    with get_sessionmaker()() as db:
        return db.execute(select(Event).where(Event.slug == SLUG)).scalar_one()


def test_saving_the_voting_window_does_not_move_the_submission_deadline(app):
    org = demo(app, "organizer")
    before = _event().submissions_close_at
    r = org.post(
        f"/e/{SLUG}/organizer/voting/window",
        {
            "tz_offset_minutes": "330",  # what app.js sends from an IST browser
            "voting_open_at": "2030-01-01T10:00",
            "voting_close_at": "2030-01-01T12:00",
        },
    )
    assert r.status_code == 303
    after = _event().submissions_close_at
    assert after == before, f"deadline moved from {before} to {after}"


def test_prize_based_winner_certificate_page_renders(app, client, auth):
    org = auth("organizer")
    prize = client.post(f"{S}/prizes", headers=org, json={"name": "Regression prize"})
    assert prize.status_code in (200, 201), prize.text
    pid = prize.json()["prize"]["id"]
    client.post(f"{S}/actions/close_judging", headers=org)
    try:
        r = client.post(f"{S}/prizes/{pid}/award", headers=org, json={"project": "prj_07"})
        assert r.status_code == 200, r.text
        assert client.post(f"{S}/actions/publish_results", headers=org).status_code == 200
        assert client.post(f"{S}/certificates/issue/winner", headers=org).status_code == 200
        certs = client.get(f"{S}/certificates", headers=org).json()["certificates"]
        serial = [c for c in certs if c["kind"] == "winner"][0]["serial"]
        page = client.get(f"/certificates/{serial}")
        assert page.status_code == 200, f"winner certificate page returned {page.status_code}"
    finally:
        client.post(f"{S}/actions/unpublish_results", headers=org)
        client.post(f"{S}/actions/open_judging", headers=org)


def test_next_step_after_closing_judging_is_publish_not_reopen(app, client, auth):
    org = auth("organizer")
    client.post(f"{S}/actions/close_judging", headers=org)
    try:
        page = demo(app, "organizer").get(f"/e/{SLUG}/organizer").text
        step = page.split("Next step", 1)[-1].split("More actions", 1)[0]
        assert "Open judging" not in step, "dashboard recommends reopening judging after a close"
        assert "Publish results" in step and "Judging closed" in page
    finally:
        client.post(f"{S}/actions/open_judging", headers=org)
