"""Findings from the final review: lifecycle rules hold on every path (quick date moves, the
settings API, imports, assignment removal), imports never crash on colliding names or ids, and
the API document names the error codes the server really sends."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from podium.db import get_sessionmaker
from podium.models import Event
from podium.services import dashboard
from tests.conftest import SLUG
from tests.test_forms_sweep import demo

S = f"/api/v1/events/{SLUG}"


def _now():
    return datetime.now(UTC).replace(microsecond=0)


def _event(client, org, name, **dates):
    body = {"name": name, "is_public": True, **{k: v.isoformat() for k, v in dates.items()}}
    r = client.post("/api/v1/events", headers=org, json=body)
    assert r.status_code == 201, r.text
    return r.json()["event"]["slug"]


def test_submissions_never_reopen_after_judging_starts_or_results_publish(app, client, auth):
    org, now = auth("organizer"), _now()
    slug = _event(
        client,
        org,
        "Reopen Guard Night",
        submissions_open_at=now - timedelta(days=2),
        submissions_close_at=now - timedelta(days=1),
    )
    api = f"/api/v1/events/{slug}/actions"
    assert client.post(f"{api}/open_judging", headers=org).status_code == 200
    browser = demo(app, "organizer")
    r = browser.post(
        f"/e/{slug}/organizer/dates", {"field": "submissions_close_at", "preset": "+60m"}
    )
    assert r.status_code == 409 and "Judging has started" in r.text
    for action in ("close_judging", "publish_results"):
        assert client.post(f"{api}/{action}", headers=org).status_code == 200
    r = client.patch(
        f"/api/v1/events/{slug}",
        headers=org,
        json={"submissions_close_at": (now + timedelta(days=1)).isoformat()},
    )
    assert r.status_code == 409 and "published" in r.json()["error"]["message"]


def test_a_scheduled_event_is_not_offered_a_reopen(client, auth):
    org, now = auth("organizer"), _now()
    slug = _event(
        client,
        org,
        "Scheduled Night",
        submissions_open_at=now + timedelta(days=1),
        submissions_close_at=now + timedelta(days=2),
    )
    with get_sessionmaker()() as db:
        event = db.execute(select(Event).where(Event.slug == slug)).scalar_one()
        phases = {p.key: p for p in dashboard.timeline(db, event)}
    assert not [a for a in phases["submissions"].actions if a.label.startswith("Reopen")]


def test_assignments_cannot_be_removed_once_judging_closes(client, auth):
    org = auth("organizer")
    judges = client.get(f"{S}/assignments", headers=org).json()["judges"]
    rows = [a for j in judges for a in j["assignments"]]
    pending = [a for a in rows if a["status"] != "done"]
    target = (pending or rows)[0]
    assert client.post(f"{S}/actions/close_judging", headers=org).status_code == 200
    try:
        r = client.delete(f"{S}/assignments/{target['id']}", headers=org)
        assert r.status_code in (403, 409), r.text
    finally:
        client.post(f"{S}/actions/open_judging", headers=org)


def test_imports_respect_the_lifecycle_and_never_crash(client, auth):
    org, now = auth("organizer"), _now()
    # 1) a file may not publish results while judging is still open
    stamp = now.isoformat()
    bad = {
        "event": {
            "id": "evt_lifecycle_probe",
            "name": "Lifecycle Probe",
            "submissions_close": stamp,
        },
        "tracks": [],
        "judges": [],
        "teams": [],
        "projects": [],
        "scores": [],
        "podium": {"event": {"judging_opened_at": stamp, "results_published_at": stamp}},
    }
    r = client.post("/api/v1/events/import?dry_run=true", headers=org, json=bad)
    assert r.status_code == 409 and "results" in r.json()["error"]["message"].lower(), r.text
    # 2) an event whose name matches an existing slug gets its own slug instead of a 500
    same_name = {
        "event": {
            "id": "evt_same_name_probe",
            "name": "Sample Hack 2026",
            "submissions_close": stamp,
        },
        "tracks": [],
        "judges": [
            {
                "id": "jdg_24",
                "name": "Other Person",
                "email": "other.person@example.test",
                "tracks": [],
            }
        ],
        "teams": [],
        "projects": [],
        "scores": [],
    }
    r = client.post("/api/v1/events/import?dry_run=true", headers=org, json=same_name)
    assert r.status_code == 200, r.text
    assert r.json()["event"] != SLUG, "a new event never takes an existing slug"


def test_import_into_an_event_with_published_results_is_refused(client, auth):
    org = auth("organizer")
    export = client.get(f"{S}/export.json", headers=org).json()
    assert client.post(f"{S}/actions/close_judging", headers=org).status_code == 200
    try:
        assert client.post(f"{S}/actions/publish_results", headers=org).status_code == 200
        r = client.post("/api/v1/events/import?dry_run=true", headers=org, json=export)
        assert r.status_code == 409 and "published" in r.json()["error"]["message"], r.text
    finally:
        client.post(f"{S}/actions/unpublish_results", headers=org)
        client.post(f"{S}/actions/open_judging", headers=org)


def test_the_api_document_names_the_error_codes_the_server_sends(client):
    spec = client.get("/api/openapi.json").json()
    codes = spec["components"]["schemas"]["ErrorResponse"]["properties"]["error"]["properties"][
        "code"
    ]["enum"]
    assert "server_error" in codes and "payload_too_large" in codes and "internal" not in codes
    responses = spec["paths"]["/api/v1/events/import"]["post"]["responses"]
    assert "413" in responses


def test_closed_judging_keeps_its_scores_and_its_judges(client, auth):
    org = auth("organizer")
    export = client.get(f"{S}/export.json", headers=org).json()
    export["scores"][0]["criteria"] = {k: 5 for k in export["scores"][0]["criteria"]}
    assert client.post(f"{S}/actions/close_judging", headers=org).status_code == 200
    try:
        r = client.post("/api/v1/events/import?dry_run=true", headers=org, json=export)
        assert r.status_code == 409 and "Judging is closed" in r.json()["error"]["message"]
        r = client.delete(f"{S}/judges/jdg_05", headers=org)
        assert r.status_code == 403, r.text
    finally:
        client.post(f"{S}/actions/open_judging", headers=org)
