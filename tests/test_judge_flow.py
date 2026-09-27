"""Judge flow: reopening a review asks first and warns until it is resubmitted, every judge action
confirms itself, validation errors come with a summary that links to each criterion, the pages
say who sees the scores, and keyboard shortcuts and polling stay out of the way."""

import html
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.conftest import SLUG
from tests.test_forms_sweep import demo, fresh_user

STATIC = Path(__file__).resolve().parent.parent / "src" / "podium" / "static"
TEMPLATES = Path(__file__).resolve().parent.parent / "src" / "podium" / "templates"


def _checked(page: str) -> dict[str, str]:
    """criterion id → the value checked on the review page."""
    return dict(re.findall(r'name="(crt_[a-z0-9]+)" value="(\d+)" checked', page))


def test_reopening_asks_first_and_warns_until_resubmitted(app):
    judge = demo(app, "judge_a")
    queue = judge.get(f"/e/{SLUG}/judge").text
    assert "Only you and the organizers see your scores" in queue
    pids = sorted(set(re.findall(rf"/e/{SLUG}/judge/review/(prj_\d+)", queue)))
    pid = pids[-1]
    page = judge.get(f"/e/{SLUG}/judge/review/{pid}").text
    original = _checked(page)
    assert len(original) == 3, "the fixture review is submitted with all three criteria"
    assert 'form="reopen-form"' in page and 'data-confirm="Reopen this review?' in page
    assert "Other judges never do." in page
    comment = html.unescape(re.search(r'name="comment"[^>]*>(.*?)</textarea>', page, re.S).group(1))
    r = judge.post(f"/e/{SLUG}/judge/review/{pid}", {"action": "reopen"})
    try:
        assert r.status_code == 303 and r.headers["location"].endswith("?saved=reopened")
        page = judge.get(r.headers["location"]).text
        assert "Reopened: this review" in page, "a reopened review says it no longer counts"
        names = sorted(original)
        r = judge.post(
            f"/e/{SLUG}/judge/review/{pid}", {"action": "submit", names[0]: original[names[0]]}
        )
        assert r.status_code == 422
        assert "There is a problem" in r.text and "data-error-summary" in r.text
        assert f'href="#crit-{names[1]}"' in r.text and f'id="crit-{names[1]}"' in r.text
        assert "Error: Review" in r.text, "the page title says there is an error"
    finally:
        r = judge.post(
            f"/e/{SLUG}/judge/review/{pid}", {"action": "submit", **original, "comment": comment}
        )
    assert r.status_code == 303 and r.headers["location"].endswith("?saved=submitted")
    page = judge.get(r.headers["location"]).text
    assert "Review submitted" in page and "Reopened: this review" not in page


def test_accepting_an_invite_confirms_it(app, client, auth):
    org = auth("organizer")
    now = datetime.now(UTC)
    slug = client.post(
        "/api/v1/events",
        headers=org,
        json={
            "name": "Invite Notice Night",
            "is_public": True,
            "submissions_open_at": (now - timedelta(days=2)).isoformat(),
            "submissions_close_at": (now - timedelta(days=1)).isoformat(),
        },
    ).json()["event"]["slug"]
    r = client.post(
        f"/api/v1/events/{slug}/judges/invites",
        headers=org,
        json={"email": "notice-judge@example.test", "tracks": []},
    )
    token = r.json()["invite"]["link"].rsplit("/", 1)[-1]
    anonymous = client.get(f"/judge-invite/{token}").text
    assert "email=notice-judge%40example.test" in anonymous, "sign-in links carry the address"
    judge = fresh_user(app, "notice-judge@example.test")
    r = judge.post(f"/judge-invite/{token}")
    assert r.status_code == 303 and r.headers["location"].endswith("?saved=joined")
    assert "You&#39;re judging Invite Notice Night" in judge.get(r.headers["location"]).text


def test_sign_in_and_register_prefill_an_invited_address(client):
    page = client.get("/register?email=invited%40example.test").text
    assert 'value="invited@example.test"' in page
    page = client.get("/login?email=invited%40example.test").text
    assert 'value="invited@example.test"' in page


def test_compare_controls_are_named_and_labelled(app):
    page = demo(app, "judge_a").get(f"/e/{SLUG}/judge/compare").text
    assert "Optional. Compare as many pairs as you like" in page
    if 'data-compare-pick="left"' in page:
        assert 'id="pick-left"' in page and 'id="pick-right"' in page
        assert 'Pick this one<span class="visually-hidden">:' in page
        assert 'for="skip-reason"' in page and "Skip this pair" in page
        assert 'hx-sync="this:drop"' in page


def test_shortcuts_polling_and_the_unsaved_guard_stay_out_of_the_way():
    js = (STATIC / "js" / "app.js").read_text()
    assert "e.altKey || e.ctrlKey || e.metaKey || e.repeat" in js, "modifiers never score"
    assert 'e.target.closest("[data-review-form]")' in js, "review keys act only in the form"
    assert "function isDirty()" in js and 'elt.tagName === "A"' in js, "the guard asks on links"
    assert "data-retry-save" in js and 'addEventListener("online", retrySave)' in js
    assert "form.dataset.submitting" in js, "a form being sent can't be sent twice"
    progress = (TEMPLATES / "organizer" / "progress.html").read_text()
    assert 'hx-trigger="every 10s" data-poll' in progress and "visibilityState" not in progress
