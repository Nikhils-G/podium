"""Integrations: one column, every event type explained, no silent "all events" subscription."""

from datetime import UTC, datetime, timedelta

from markupsafe import escape

from podium.services.webhooks import EVENT_DESCRIPTIONS, EVENT_TYPES
from tests.conftest import SLUG
from tests.test_forms_sweep import demo

BASE = f"/e/{SLUG}/organizer"
API = f"/api/v1/events/{SLUG}"


def _hooks(client, auth):
    return client.get(f"{API}/webhooks", headers=auth("organizer")).json()["webhooks"]


def test_every_event_type_has_a_description():
    assert set(EVENT_DESCRIPTIONS) == set(EVENT_TYPES)
    assert all(text.strip().endswith(".") for text in EVENT_DESCRIPTIONS.values())


def test_page_is_one_column_and_explains_each_event(app):
    html = demo(app, "organizer").get(f"{BASE}/integrations").text
    for anchor in (
        'id="webhooks"',
        'id="add-webhook"',
        'id="deliveries"',
        'id="embed"',
        'id="api"',
    ):
        assert anchor in html
    assert "data-check-all" in html and "data-check-none" in html
    for event_type in EVENT_TYPES:
        assert f'value="{event_type}"' in html
    for text in EVENT_DESCRIPTIONS.values():
        assert str(escape(text)) in html
    assert "stack--aside" not in html and "checklist" not in html  # nothing scrolls inside the page


def test_clearing_every_event_is_refused_and_keeps_what_was_typed(app, client, auth):
    before = len(_hooks(client, auth))
    r = demo(app, "organizer").post(f"{BASE}/webhooks", {"url": "https://hooks.example.test/none"})
    assert r.status_code == 422
    assert "Choose at least one event type." in r.text
    assert 'value="https://hooks.example.test/none"' in r.text
    assert 'value="ping" checked' not in r.text  # the cleared boxes stay cleared
    assert len(_hooks(client, auth)) == before


def test_the_hook_subscribes_to_exactly_the_ticked_events(app, client, auth):
    url = "https://hooks.example.test/two-events"
    r = demo(app, "organizer").post(
        f"{BASE}/webhooks", {"url": url, "events": ["vote.cast", "ping"]}
    )
    assert r.status_code == 200 and "Signing secret" in r.text
    hook = next(h for h in _hooks(client, auth) if h["url"] == url)
    assert set(hook["events"]) == {"vote.cast", "ping"}
    types = client.get(f"{API}/webhooks/types").json()
    assert set(types["descriptions"]) == set(types["types"])


def test_pause_resume_and_delete_say_what_happened(app, client, auth):
    org = demo(app, "organizer")
    url = "https://hooks.example.test/lifecycle"
    org.post(f"{BASE}/webhooks", {"url": url, "events": ["ping"]})
    hook = next(h for h in _hooks(client, auth) if h["url"] == url)
    r = org.post(f"{BASE}/webhooks/{hook['id']}/toggle")
    assert r.status_code == 303 and "saved=paused" in r.headers["location"]
    assert "Webhook paused." in org.get(r.headers["location"]).text
    r = org.post(f"{BASE}/webhooks/{hook['id']}/toggle")
    assert "saved=resumed" in r.headers["location"]
    r = org.post(f"{BASE}/webhooks/{hook['id']}/delete")
    assert "saved=deleted" in r.headers["location"]
    assert "Webhook deleted." in org.get(r.headers["location"]).text


def test_a_new_event_teaches_how_deliveries_appear(app):
    org = demo(app, "organizer")
    now = datetime.now(UTC)
    r = org.post(
        "/events/new",
        {
            "name": "Integrations Night",
            "description": "",
            "max_team_size": "3",
            "submissions_open_at": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
            "submissions_close_at": (now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M"),
        },
    )
    assert r.status_code == 303
    slug = r.headers["location"].split("/e/")[1].split("/")[0]
    html = org.get(f"/e/{slug}/organizer/integrations").text
    assert "Nothing delivered yet" in html and "No webhooks yet." in html
