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
