"""Forms and page structure: a form that comes back with errors leads with a summary that links
to each field and says "Error:" in the title; landmarks, labels and titles name what they hold."""

from tests.conftest import SLUG
from tests.test_forms_sweep import demo


def test_a_form_with_errors_leads_with_a_linked_summary(app):
    org = demo(app, "organizer")
    r = org.post(
        "/events/new",
        {
            "name": "",
            "submissions_open_at": "2026-03-02T18:00",
            "submissions_close_at": "2026-03-01T18:00",
        },
    )
    assert r.status_code == 422
    assert "There is a problem" in r.text and 'href="#f-name"' in r.text
    assert "<title>Error: Create event" in r.text
    r = demo(app, "organizer").get("/events/new")
    assert "There is a problem" not in r.text and "<title>Create event" in r.text


def test_register_errors_are_summarised_too(app):
    from tests.test_forms_sweep import Browser

    visitor = Browser(app)
    r = visitor.post("/register", {"name": "", "email": "not-an-email", "password": "x"})
    assert r.status_code == 422 and "There is a problem" in r.text
    assert "data-error-summary" in r.text


def test_landmarks_labels_and_titles_name_what_they_hold(app):
    page = demo(app, "organizer").get(f"/e/{SLUG}/organizer/voting").text
    assert '<nav class="rail" aria-label="Organizer sections"' in page
    assert '<legend class="field__label">Who can vote</legend>' in page
    assert (
        "<title>Voting · Sample Hack 2026 · Podium</title>" in page
        or "Sample Hack 2026" in (page.split("<title>", 1)[1].split("</title>", 1)[0])
    )
    assert 'aria-describedby="confirm-body"' in page and 'id="confirm-body"' in page
    assert 'role="status" aria-live="polite"' in page, "copy and error toasts are announced"


def test_the_podium_is_read_in_rank_order():
    from pathlib import Path

    html = (
        Path(__file__).resolve().parent.parent / "src/podium/templates/public/results.html"
    ).read_text()
    assert '<ol class="podium"' in html and "{% for i in [0, 1, 2] %}" in html


def test_gallery_search_also_matches_track_names(client):
    page = client.get(f"/e/{SLUG}/projects?q=climate").text
    assert "No matching projects" not in page and 'id="gallery-count">0 projects' not in page


def test_the_ballot_without_an_open_vote_is_just_the_notice(client):
    page = client.get(f"/e/{SLUG}/vote").text
    assert (
        "This event has no community vote" in page
        or "Voting opens" in page
        or "Voting closed" in page
    )
    assert '<ol class="ballot">' not in page and "Browse the projects" in page


def test_organizer_forms_keep_what_was_typed_and_refresh_related_lists(app, client, auth):
    from datetime import UTC, datetime, timedelta

    org, now = auth("organizer"), datetime.now(UTC)
    slug = client.post(
        "/api/v1/events",
        headers=org,
        json={
            "name": "Forms Night",
            "is_public": True,
            "submissions_open_at": (now - timedelta(hours=1)).isoformat(),
            "submissions_close_at": (now + timedelta(days=1)).isoformat(),
        },
    ).json()["event"]["slug"]
    browser = demo(app, "organizer")
    r = browser.post(
        f"/e/{slug}/organizer/rubric",
        {"name": "Impact", "weight": "2", "min_score": "5", "max_score": "1"},
    )
    assert r.status_code == 422 and 'value="Impact"' in r.text and 'value="5"' in r.text
    r = browser.post(f"/e/{slug}/organizer/tracks", {"name": "Health"}, htmx=True)
    assert r.status_code == 200 and 'id="prizes" hx-swap-oob="true"' in r.text


def test_participants_and_judges_get_a_way_forward(app):
    page = demo(app, "participant").get(f"/e/{SLUG}").text
    assert f'href="/e/{SLUG}/projects/prj_01"' in page and "My project" in page
    queue = demo(app, "judge_a").get(f"/e/{SLUG}/judge").text
    import re

    assigned = set(re.findall(r"/judge/review/(prj_\d+)", queue))
    other = next(f"prj_{i:02d}" for i in range(1, 42) if f"prj_{i:02d}" not in assigned)
    r = demo(app, "judge_a").get(f"/e/{SLUG}/judge/review/{other}")
    assert r.status_code == 403 and "Back to your queue" in r.text


def test_return_to_draft_asks_first():
    from pathlib import Path

    html = (
        Path(__file__).resolve().parent.parent / "src/podium/templates/participant/submit.html"
    ).read_text()
    assert 'value="unsubmit" data-confirm="Return to draft?' in html
