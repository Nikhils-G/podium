"""Guards added in the fix pass: local-time entry, invite rules, honest progress, prize awards,
publish guard on the results page, htmx section swaps, and the safe-defaults startup guard."""

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from podium.models import ComparisonSource, Event, PairwiseComparison, User
from podium.services import dashboard, pairwise
from podium.services.events import validate_event
from tests.conftest import SLUG
from tests.test_forms_sweep import demo, fresh_user

S = f"/api/v1/events/{SLUG}"


def test_dates_typed_in_local_time_are_stored_as_utc():
    base = {
        "name": "Local",
        "submissions_open_at": "2026-03-01T18:00",
        "submissions_close_at": "2026-03-02T18:00",
    }
    clean, errors = validate_event({**base, "tz_offset_minutes": "330"})  # IST
    assert not errors and clean["submissions_open_at"] == datetime(2026, 3, 1, 12, 30, tzinfo=UTC)
    clean, _ = validate_event(base)  # no JavaScript → the label says UTC and we mean it
    assert clean["submissions_open_at"] == datetime(2026, 3, 1, 18, 0, tzinfo=UTC)
    clean, _ = validate_event({**base, "tz_offset_minutes": "99999"})  # nonsense is clamped
    assert clean["submissions_open_at"] == datetime(2026, 3, 1, 4, 0, tzinfo=UTC)
    clean, _ = validate_event({**base, "tz_offset_minutes": "abc"})
    assert clean["submissions_open_at"] == datetime(2026, 3, 1, 18, 0, tzinfo=UTC)


def test_invites_one_pending_per_email_regenerate_revoke_and_email_match(client, auth, app):
    org = auth("organizer")
    r = client.post(
        f"{S}/judges/invites", headers=org, json={"email": "Guard.Judge@example.test", "tracks": []}
    )
    assert r.status_code == 201
    first = r.json()["invite"]
    assert (
        client.post(
            f"{S}/judges/invites",
            headers=org,
            json={"email": "guard.judge@example.test", "tracks": []},
        ).status_code
        == 409
    )
    r = client.post(f"{S}/judges/invites/{first['id']}/regenerate", headers=org)
    assert r.status_code == 200 and r.json()["invite"]["link"] != first["link"]
    old_path = "/judge-invite/" + first["link"].rsplit("/", 1)[-1]
    new_path = "/judge-invite/" + r.json()["invite"]["link"].rsplit("/", 1)[-1]
    assert client.get(old_path).status_code == 404, "the previous link stops working"
    # wrong account: the page explains instead of offering Accept, and POST is refused
    stranger = fresh_user(app, "guard-stranger@example.test")
    page = stranger.get(new_path)
    assert page.status_code == 200 and "signed in as guard-stranger@example.test" in page.text
    assert "Accept and start judging" not in page.text
    assert stranger.post(new_path).status_code == 403
    invited = fresh_user(app, "guard.judge@example.test")
    page = invited.get(new_path)
    assert "Accept and start judging" in page.text
    assert invited.post(new_path).status_code == 303
    # revoke another pending invite
    r = client.post(
        f"{S}/judges/invites",
        headers=org,
        json={"email": "guard-revoke@example.test", "tracks": []},
    )
    invite = r.json()["invite"]
    assert client.delete(f"{S}/judges/invites/{invite['id']}", headers=org).status_code == 204
    assert client.get("/judge-invite/" + invite["link"].rsplit("/", 1)[-1]).status_code == 404
    assert (
        client.delete(f"{S}/judges/invites/{invite['id']}", headers=auth("judge_a")).status_code
        == 403
    )


def test_compare_progress_counts_only_what_the_judge_did(db):
    event = db.execute(select(Event).where(Event.slug == SLUG)).scalar_one()
    judge = db.execute(select(User).where(User.public_id == "jdg_24")).scalar_one()
    rows = (
        db.execute(
            select(PairwiseComparison).where(
                PairwiseComparison.event_id == event.id, PairwiseComparison.judge_id == judge.id
            )
        )
        .scalars()
        .all()
    )
    derived = [r for r in rows if r.source == ComparisonSource.derived]
    judged_pairs = {
        (min(r.project_a_id, r.project_b_id), max(r.project_a_id, r.project_b_id))
        for r in rows
        if r.source == ComparisonSource.judge
    }
    assert derived, "the demo event seeds derived comparisons for this judge"
    state = pairwise.next_pair(db, event, judge)
    assert state.done == len(judged_pairs) and state.done < len(derived)
    results = pairwise.results(db, event)
    assert (
        results.derived_comparisons > 0
        and results.comparisons == results.judge_comparisons + results.derived_comparisons
    )


def test_progress_headline_is_projects_at_target(db):
    event = db.execute(select(Event).where(Event.slug == SLUG)).scalar_one()
    p = dashboard.progress(db, event)
    assert p.projects_total == 41 and p.projects_reviewed == p.projects_total - len(p.below_target)
    assert 0 < p.projects_reviewed < p.projects_total, "fixtures leave some projects under target"


def test_prize_awards_drive_public_results_certificates_and_export(client, auth, app):
    org = auth("organizer")
    prize = client.post(
        f"{S}/prizes",
        headers=org,
        json={"name": "Guard prize", "amount": "$1", "description": "", "track": ""},
    ).json()["prize"]
    pid = prize["id"]
    try:
        assert (
            client.post(
                f"{S}/prizes/{pid}/award", headers=org, json={"project": "prj_nope"}
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"{S}/prizes/{pid}/award", headers=auth("judge_a"), json={"project": "prj_07"}
            ).status_code
            == 403
        )
        r = client.post(f"{S}/prizes/{pid}/award", headers=org, json={"project": "prj_07"})
        assert r.status_code == 200 and r.json()["prize"]["project"] == "prj_07"
        assert [p for p in client.get(S, headers=org).json()["event"]["prizes"] if p["id"] == pid][
            0
        ]["project"] == "prj_07"
        results_page = demo(app, "organizer").get(f"/e/{SLUG}/organizer/results")
        assert "Guard prize" in results_page.text and "Change award" in results_page.text
        assert "Close judging first" in results_page.text, "judging is open in the fixtures"
        assert client.post(f"{S}/actions/close_judging", headers=org).status_code == 200
        assert (
            "Close judging first"
            not in demo(app, "organizer").get(f"/e/{SLUG}/organizer/results").text
        )
        assert client.post(f"{S}/actions/publish_results", headers=org).status_code == 200
        public = client.get(f"/e/{SLUG}/results").text
        assert "Guard prize" in public and "Glass Signal" in public
        assert "Awarded to" in client.get(f"/e/{SLUG}").text
        r = client.post(f"{S}/certificates/issue/winner", headers=org)
        assert r.status_code == 200 and r.json()["issued"] >= 1
        certs = client.get(f"{S}/certificates", headers=org).json()["certificates"]
        winners = [c for c in certs if c["kind"] == "winner"]
        assert winners and all(c["payload"]["prize"]["name"] == "Guard prize" for c in winners)
        export = client.get(f"{S}/export.json", headers=org).json()
        assert [p for p in export["podium"]["prizes"] if p["id"] == pid][0]["project"] == "prj_07"
        r = client.post(
            "/api/v1/events/import", headers=org, params={"dry_run": "true"}, json=export
        )
        assert r.status_code == 200, r.text
    finally:
        client.post(f"{S}/actions/unpublish_results", headers=org)
        client.post(f"{S}/actions/open_judging", headers=org)
        client.delete(f"{S}/prizes/{pid}", headers=org)


def test_tracks_section_swaps_in_place_over_htmx(app):
    org = demo(app, "organizer")
    r = org.post(
        f"/e/{SLUG}/organizer/tracks", {"name": "Swap track", "description": ""}, htmx=True
    )
    assert (
        r.status_code == 200 and r.text.lstrip().startswith("<section") and "Swap track" in r.text
    )
    assert 'id="tracks"' in r.text and "<html" not in r.text
    r = org.post(f"/e/{SLUG}/organizer/tracks", {"name": "", "description": ""}, htmx=True)
    assert r.status_code == 422 and 'id="tracks"' in r.text and "field__error" in r.text
    import re

    tid = [t for t in re.findall(r"/tracks/(trk_[a-z0-9]+)/delete", r.text)][-1]
    r = org.post(f"/e/{SLUG}/organizer/tracks/{tid}/delete", htmx=True)
    assert r.status_code == 200 and "Swap track" not in r.text


def test_new_event_attention_names_the_missing_pieces(client, auth, db):
    org = auth("organizer")
    now = datetime.now(UTC).isoformat()
    slug = client.post(
        "/api/v1/events",
        headers=org,
        json={"name": "Attention Night", "is_public": True, "submissions_open_at": now},
    ).json()["event"]["slug"]
    event = db.execute(select(Event).where(Event.slug == slug)).scalar_one()
    titles = {a.title for a in dashboard.overview(db, event).attention}
    assert "No rubric yet" in titles and "No judges have accepted yet" in titles


def test_startup_refuses_default_secret_behind_https(monkeypatch):
    from podium import main
    from podium.config import get_settings

    unsafe = get_settings().model_copy(
        update={"secret_key": "podium-dev-secret-change-me", "base_url": "https://podium.example"}
    )
    monkeypatch.setattr(main, "get_settings", lambda: unsafe)
    with pytest.raises(RuntimeError):
        asyncio.run(main.lifespan(main.app).__aenter__())
