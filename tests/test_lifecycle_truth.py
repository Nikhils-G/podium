"""Lifecycle truth: quick deadline moves never shorten a deadline, results are published only
when there is something final to publish, every lifecycle action confirms itself, warnings
outlive the phase only as long as they matter, and assignments freeze once judging closes."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from podium.db import get_sessionmaker
from podium.models import Event
from podium.services import dashboard
from tests.conftest import SLUG
from tests.test_forms_sweep import demo

S = f"/api/v1/events/{SLUG}"


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _new_event(client, org, name, **dates) -> str:
    body = {"name": name, "is_public": True}
    body.update({key: value.isoformat() for key, value in dates.items()})
    r = client.post("/api/v1/events", headers=org, json=body)
    assert r.status_code == 201, r.text
    return r.json()["event"]["slug"]


def _event(slug) -> Event:
    with get_sessionmaker()() as db:
        return db.execute(select(Event).where(Event.slug == slug)).scalar_one()


def _timeline(slug) -> dict:
    with get_sessionmaker()() as db:
        event = db.execute(select(Event).where(Event.slug == slug)).scalar_one()
        return {phase.key: phase for phase in dashboard.timeline(db, event)}


def test_extend_moves_a_future_deadline_later_never_earlier(app, client, auth):
    org, now = auth("organizer"), _now()
    close = now + timedelta(days=3)
    slug = _new_event(
        client,
        org,
        "Extend Night",
        submissions_open_at=now - timedelta(hours=1),
        submissions_close_at=close,
    )
    r = demo(app, "organizer").post(
        f"/e/{slug}/organizer/dates", {"field": "submissions_close_at", "preset": "+15m"}
    )
    assert r.status_code == 303
    assert _event(slug).submissions_close_at == close + timedelta(minutes=15), (
        "Extend 15 min adds 15 minutes to the deadline, not to the current time"
    )
    extend = [a for a in _timeline(slug)["submissions"].actions if a.preset == "+60m"][0]
    later = close + timedelta(minutes=75)
    assert f"{later.day} {later:%b %Y, %H:%M} UTC" in extend.consequence, extend.consequence


def test_reopening_a_past_deadline_counts_from_now(app, client, auth):
    org, now = auth("organizer"), _now()
    slug = _new_event(
        client,
        org,
        "Reopen Night",
        submissions_open_at=now - timedelta(days=2),
        submissions_close_at=now - timedelta(days=1),
    )
    demo(app, "organizer").post(
        f"/e/{slug}/organizer/dates", {"field": "submissions_close_at", "preset": "+60m"}
    )
    closes = _event(slug).submissions_close_at
    assert now + timedelta(minutes=59) <= closes <= _now() + timedelta(minutes=61)


def test_extending_voting_counts_from_the_current_close(app, client, auth):
    org, now = auth("organizer"), _now()
    close = now + timedelta(days=2)
    slug = _new_event(
        client,
        org,
        "Vote Extend Night",
        submissions_open_at=now - timedelta(days=3),
        submissions_close_at=now - timedelta(days=2),
        voting_open_at=now - timedelta(hours=1),
        voting_close_at=close,
    )
    demo(app, "organizer").post(
        f"/e/{slug}/organizer/dates", {"field": "voting_close_at", "preset": "+60m"}
    )
    assert _event(slug).voting_close_at == close + timedelta(hours=1)


def test_results_are_published_only_when_there_is_something_final(app, client, auth):
    org, now = auth("organizer"), _now()
    slug = _new_event(
        client,
        org,
        "Publish Guard Night",
        submissions_open_at=now - timedelta(hours=1),
        submissions_close_at=now + timedelta(days=2),
    )
    api = f"/api/v1/events/{slug}/actions"
    r = client.post(f"{api}/publish_results", headers=org)
    assert r.status_code == 409 and "nothing to publish" in r.json()["error"]["message"].lower()
    publish = _timeline(slug)["results"].actions[0]
    assert not publish.enabled and publish.kind != "primary", "a new event can't publish results"
    assert client.post(f"{api}/open_judging", headers=org).status_code == 200
    r = client.post(f"{api}/publish_results", headers=org)
    assert r.status_code == 409 and "close judging" in r.json()["error"]["message"].lower()
    assert client.post(f"{api}/close_judging", headers=org).status_code == 200
    # a community vote that is still open blocks publishing too
    r = client.patch(
        f"/api/v1/events/{slug}",
        headers=org,
        json={
            "name": "Publish Guard Night",
            "voting_open_at": (now - timedelta(hours=1)).isoformat(),
            "voting_close_at": (now + timedelta(hours=1)).isoformat(),
        },
    )
    assert r.status_code == 200, r.text
    r = client.post(f"{api}/publish_results", headers=org)
    assert r.status_code == 409 and "voting is open" in r.json()["error"]["message"].lower()
    r = client.patch(
        f"/api/v1/events/{slug}",
        headers=org,
        json={
            "name": "Publish Guard Night",
            "voting_open_at": (now - timedelta(hours=2)).isoformat(),
            "voting_close_at": (now - timedelta(hours=1)).isoformat(),
        },
    )
    assert r.status_code == 200, r.text
    assert client.post(f"{api}/publish_results", headers=org).status_code == 200


def test_a_closed_community_vote_alone_can_be_published(client, auth):
    org, now = auth("organizer"), _now()
    slug = _new_event(
        client,
        org,
        "Vote Only Night",
        submissions_open_at=now - timedelta(days=3),
        submissions_close_at=now - timedelta(days=2),
        voting_open_at=now - timedelta(days=1),
        voting_close_at=now - timedelta(hours=1),
    )
    assert client.post(
        f"/api/v1/events/{slug}/actions/publish_results", headers=org
    ).status_code == (200)


def test_every_lifecycle_action_confirms_itself(app, client, auth):
    org, now = auth("organizer"), _now()
    slug = _new_event(
        client,
        org,
        "Notice Night",
        submissions_open_at=now - timedelta(days=2),
        submissions_close_at=now - timedelta(days=1),
    )
    browser = demo(app, "organizer")
    expected = {
        "open_judging": "Judging opened",
        "close_judging": "Judging closed",
        "publish_results": "Results published",
        "unpublish_results": "Results unpublished",
    }
    for action, notice in expected.items():
        r = browser.post(f"/e/{slug}/organizer/actions/{action}")
        assert r.status_code == 303 and r.headers["location"].endswith(f"?saved={action}")
        assert notice in browser.get(r.headers["location"]).text, action
    page = browser.get(f"/e/{slug}/organizer?saved=publish_results").text
    assert f'href="/e/{slug}/results"' in page, "the notice links to the public results"
    r = browser.post(
        f"/e/{slug}/organizer/dates", {"field": "submissions_close_at", "preset": "+60m"}
    )
    page = browser.get(r.headers["location"]).text
    assert "Submissions now close" in page, "the notice names the new deadline"


def test_duplicate_warning_lasts_until_results_are_published(client, auth, db):
    org = auth("organizer")
    event = db.execute(select(Event).where(Event.slug == SLUG)).scalar_one()
    step = dashboard.next_step(db, event)
    assert "fewer than 3" in step.note, step.note
    assert client.post(f"{S}/actions/close_judging", headers=org).status_code == 200
    try:
        db.refresh(event)
        titles = [item.title for item in dashboard.overview(db, event).attention]
        assert "Possible duplicate submission" in titles, titles
        assert any("were scored on fewer than" in title for title in titles), titles
        step = dashboard.next_step(db, event)
        assert "possible duplicate" in step.note, step.note
    finally:
        client.post(f"{S}/actions/open_judging", headers=org)


def test_assignments_freeze_once_judging_closes(client, auth):
    org = auth("organizer")
    assert client.post(f"{S}/actions/close_judging", headers=org).status_code == 200
    try:
        r = client.post(
            f"{S}/assignments", headers=org, json={"judge": "jdg_26", "projects": ["prj_02"]}
        )
        assert r.status_code == 403 and "judging is closed" in r.json()["error"]["message"].lower()
        r = client.post(f"{S}/assignments/auto/apply", headers=org, json={"reviews_per_project": 4})
        assert r.status_code == 403
    finally:
        client.post(f"{S}/actions/open_judging", headers=org)


def test_event_complete_once_certificates_exist(client, auth, monkeypatch):
    org, now = auth("organizer"), _now()
    slug = _new_event(
        client,
        org,
        "Complete Night",
        submissions_open_at=now - timedelta(days=2),
        submissions_close_at=now - timedelta(days=1),
    )
    api = f"/api/v1/events/{slug}/actions"
    for action in ("open_judging", "close_judging", "publish_results"):
        assert client.post(f"{api}/{action}", headers=org).status_code == 200, action
    monkeypatch.setattr(dashboard, "certificate_counts", lambda db, event: (128, 3))
    with get_sessionmaker()() as db:
        event = db.execute(select(Event).where(Event.slug == slug)).scalar_one()
        step = dashboard.next_step(db, event)
        phases = {phase.key: phase for phase in dashboard.timeline(db, event)}
    assert step.label.startswith("Event complete") and "128" in step.label, step.label
    assert phases["certificates"].summary == "128 issued · 3 revoked"


def test_assignments_page_shows_the_freeze(app, client, auth):
    org = auth("organizer")
    assert client.post(f"{S}/actions/close_judging", headers=org).status_code == 200
    try:
        page = demo(app, "organizer").get(f"/e/{SLUG}/organizer/assignments").text
        assert "Judging is closed, so assignments can" in page
        assert page.count('<fieldset class="stack" disabled>') == 2
    finally:
        client.post(f"{S}/actions/open_judging", headers=org)
