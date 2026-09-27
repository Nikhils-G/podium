"""Guards added in the fix pass: local-time entry, invite rules, honest progress, prize awards,
publish guard on the results page, htmx section swaps, and the safe-defaults startup guard."""

import asyncio
import re
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


def test_webhook_test_delivery_and_coalesced_vote_events(client, auth, db):
    from podium.models import DeliveryStatus, WebhookDelivery

    org = auth("organizer")
    hook = client.post(
        f"{S}/webhooks",
        headers=org,
        json={
            "url": "https://hooks.example.test/fix",
            "events": ["ping", "vote.cast", "comment.added"],
        },
    ).json()["webhook"]
    from tests.test_forms_sweep import demo

    r = demo(client.app, "organizer").post(f"/e/{SLUG}/organizer/webhooks/{hook['id']}/test")
    assert r.status_code == 303 and "saved=test" in r.headers["location"]
    pings = (
        db.execute(select(WebhookDelivery).where(WebhookDelivery.event_type == "ping"))
        .scalars()
        .all()
    )
    assert pings and pings[-1].payload["data"]["webhook"] == hook["id"]
    assert pings[-1].status == DeliveryStatus.pending
    types = client.get(f"{S}/webhooks/types", headers=org).json()
    assert {"vote.cast", "comment.added", "ping"} <= set(
        types["types"] if isinstance(types, dict) else types
    )


def test_read_only_and_expired_tokens(client, auth, db):
    from datetime import timedelta

    from podium.models import ApiToken

    org = auth("organizer")
    r = client.post("/api/v1/me/tokens", headers=org, json={"name": "ro", "scope": "read"})
    assert r.status_code == 201
    ro = {"Authorization": f"Bearer {r.json()['token']['secret']}"}
    assert client.get("/api/v1/me", headers=ro).status_code == 200
    r = client.post(f"{S}/tracks", headers=ro, json={"name": "Nope", "description": ""})
    assert r.status_code == 403 and "read-only" in r.json()["error"]["message"]
    r = client.post("/api/v1/me/tokens", headers=org, json={"name": "short", "expires_in_days": 1})
    assert r.status_code == 200 or r.status_code == 201
    raw = r.json()["token"]["secret"]
    listed = client.get("/api/v1/me/tokens", headers=org).json()["tokens"]
    mine = [t for t in listed if t["name"] == "short"][0]
    assert mine["scope"] == "read" and mine["expires_at"] is not None, "defaults are closed"
    row = db.get(ApiToken, mine["id"])
    row.expires_at = row.expires_at - timedelta(days=2)
    db.commit()
    assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {raw}"}).status_code == 401
    assert (
        client.post(
            "/api/v1/me/tokens", headers=org, json={"name": "bad", "scope": "root"}
        ).status_code
        == 422
    )


def test_export_carries_the_record_sections_and_judge_records_carry_a_digest(client, auth):
    org = auth("organizer")
    export = client.get(f"{S}/export.json", headers=org).json()["podium"]
    for key in ("votes", "comments", "certificates", "audit"):
        assert key in export, key
    assert export["audit"] and all(len(a["row_hash"]) == 64 for a in export["audit"][:5])
    assert all("ip" not in v for v in export["votes"])
    client.post(f"{S}/actions/close_judging", headers=org)
    try:
        client.post(f"{S}/certificates/issue/judge", headers=org)
        record = client.get(f"{S}/judges/me/records", headers=auth("judge_a")).json()["records"][0]
        assert len(record["payload"]["reviews_digest"]) == 64
    finally:
        client.post(f"{S}/actions/open_judging", headers=org)


def test_signed_audit_anchor_verifies_with_the_instance_key(client, auth, app):
    from podium.services import certificates as cert_service

    r = client.get(f"{S}/audit/anchor", headers=auth("organizer"))
    assert r.status_code == 200
    anchor = r.json()
    signature = anchor.pop("signature")
    assert anchor["ok"] and len(anchor["head_hash"]) == 64
    assert cert_service.verify_signature(anchor["public_key_hex"], anchor, signature)
    assert client.get(f"{S}/audit/anchor", headers=auth("judge_a")).status_code == 403
    page = demo(app, "organizer").get(
        f"/e/{SLUG}/organizer/audit/anchor", headers={"HX-Request": "true"}
    )
    assert page.status_code == 200 and "Copy signed anchor" in page.text


def test_openapi_documents_auth_and_errors_on_every_operation(client):
    schema = client.get("/api/openapi.json").json()
    assert {"sessionCookie", "bearerToken"} <= set(schema["components"]["securitySchemes"])
    assert "ErrorResponse" in schema["components"]["schemas"]
    for path, item in schema["paths"].items():
        for method, op in item.items():
            if method not in ("get", "post", "patch", "put", "delete"):
                continue
            if op.get("security") == []:
                assert "401" not in op["responses"], (method, path)
            else:
                assert "401" in op["responses"], (method, path)
    results = schema["paths"]["/api/v1/events/{slug}/results"]["get"]["responses"]["200"]
    assert "ResultsOut" in str(results)


def test_next_step_follows_the_stage_and_never_undoes_a_close(client, auth, db):
    org = auth("organizer")
    event = db.execute(select(Event).where(Event.slug == SLUG)).scalar_one()
    step = dashboard.next_step(db, event)
    assert step.label == "Close judging", "every fixture assignment is done"
    client.post(f"{S}/actions/close_judging", headers=org)
    try:
        db.expire_all()
        step = dashboard.next_step(db, event)
        assert step.label == "Publish results" and step.action == "publish_results"
        keys = [k for k, _, _ in dashboard.more_actions(event)]
        assert "open_judging" in keys and "close_judging" not in keys
    finally:
        client.post(f"{S}/actions/open_judging", headers=org)
    db.expire_all()
    assert dashboard.next_step(db, event).label == "Close judging"
    assert "open_judging" not in [k for k, _, _ in dashboard.more_actions(event)]


def test_voting_window_save_rejects_and_confirms_early_close(app, client, auth):
    """Closing the window early while votes are in needs an explicit tick; the settings form
    needs one to reopen submissions once judging has started."""
    org = demo(app, "organizer")
    r = org.post(
        f"/e/{SLUG}/organizer/settings",
        {
            "name": "Sample Hack 2026",
            "description": "x",
            "max_team_size": "4",
            "is_public": "on",
            "submissions_open_at": "2026-01-30T00:00",
            "submissions_close_at": "2099-03-01T18:00",
            "voting_open_at": "",
            "voting_close_at": "",
        },
    )
    assert r.status_code == 422 and "reopens submissions" in r.text, (
        "judging is open in the fixtures"
    )
    r = org.post(
        f"/e/{SLUG}/organizer/settings",
        {
            "name": "Sample Hack 2026",
            "description": "x",
            "max_team_size": "4",
            "is_public": "on",
            "submissions_open_at": "2026-01-30T00:00",
            "submissions_close_at": "2026-03-01T18:00",
            "voting_open_at": "",
            "voting_close_at": "",
        },
    )
    assert r.status_code == 303, "an unchanged deadline needs no confirmation"


def test_boosted_navigation_never_gets_a_bare_partial(app):
    org = demo(app, "organizer")
    boosted = {"HX-Request": "true", "HX-Boosted": "true"}
    page = org.get(f"/e/{SLUG}/organizer/progress", headers=boosted)
    assert page.status_code == 200 and "<html" in page.text and 'id="progress-body"' in page.text
    partial = org.get(
        f"/e/{SLUG}/organizer/progress",
        headers={"HX-Request": "true", "HX-Target": "progress-body"},
    )
    assert partial.status_code == 200 and "<html" not in partial.text and "kpis" in partial.text
    judge = demo(app, "judge_a")
    assert "<html" in judge.get(f"/e/{SLUG}/judge/compare", headers=boosted).text


def test_gallery_filter_swap_refreshes_the_track_chips(client):
    r = client.get(
        f"/e/{SLUG}/projects",
        params={"track": "trk_03"},
        headers={"HX-Request": "true", "HX-Target": "gallery-results"},
    )
    assert r.status_code == 200 and "<html" not in r.text
    assert 'id="gallery-tracks"' in r.text and 'hx-swap-oob="true"' in r.text
    assert 'id="gallery-count"' in r.text
    chips = r.text.split('id="gallery-tracks"', 1)[1]
    assert chips.count("is-selected") == 1
    selected = re.search(r'<a class="chip[^"]*is-selected[^"]*"[^>]*>', chips).group(0)
    assert "track=trk_03" in selected, selected


def test_archived_event_console_is_read_only(client, auth, app):
    org = auth("organizer")
    now = datetime.now(UTC).isoformat()
    slug = client.post(
        "/api/v1/events",
        headers=org,
        json={"name": "Archive Me", "is_public": True, "submissions_open_at": now},
    ).json()["event"]["slug"]
    assert client.post(f"/api/v1/events/{slug}/actions/archive", headers=org).status_code == 200
    page = demo(app, "organizer").get(f"/e/{slug}/organizer/settings")
    assert page.status_code == 200 and "archived and read-only" in page.text
    assert "<fieldset" in page.text and "disabled" in page.text.split("<fieldset", 1)[1][:80]
    r = demo(app, "organizer").post(
        f"/e/{slug}/organizer/settings",
        {
            "name": "Renamed",
            "description": "",
            "max_team_size": "4",
            "is_public": "on",
            "submissions_open_at": "",
            "submissions_close_at": "",
            "voting_open_at": "",
            "voting_close_at": "",
        },
    )
    assert r.status_code == 409 and "Archived events" in r.text and "Event settings" in r.text


def test_webhook_4xx_is_final_and_not_retried(db):
    from podium.models import DeliveryStatus, Event, WebhookDelivery
    from podium.services import webhooks

    event = db.execute(select(Event).where(Event.slug == SLUG)).scalar_one()
    organizer = db.execute(select(User).where(User.email == "organizer@podium.local")).scalar_one()
    hook = webhooks.create_hook(db, event, organizer, "https://hooks.example.test/final", ["ping"])
    webhooks.emit(db, event, "ping", {"message": "x"}, only_hook=hook)
    db.commit()
    webhooks.deliver_pending(db, sender=lambda u, b, h: (405, "HTTP 405"))
    delivery = (
        db.execute(
            select(WebhookDelivery)
            .where(WebhookDelivery.webhook_id == hook.id)
            .order_by(WebhookDelivery.id.desc())
        )
        .scalars()
        .first()
    )
    assert delivery.status == DeliveryStatus.failed and delivery.attempts == 1
    assert "not retried" in delivery.last_error and delivery.next_attempt_at is None
    webhooks.emit(db, event, "ping", {"message": "y"}, only_hook=hook)
    db.commit()
    webhooks.deliver_pending(db, sender=lambda u, b, h: (503, "HTTP 503"))
    retry = (
        db.execute(
            select(WebhookDelivery)
            .where(WebhookDelivery.webhook_id == hook.id)
            .order_by(WebhookDelivery.id.desc())
        )
        .scalars()
        .first()
    )
    assert retry.status == DeliveryStatus.pending and retry.next_attempt_at is not None


def test_openapi_marks_public_operations_and_omits_impossible_errors(client):
    schema = client.get("/api/openapi.json").json()
    tally = schema["paths"]["/api/v1/events/{slug}/tally"]["get"]
    assert tally["security"] == [] and "401" not in tally["responses"]
    assert "409" not in tally["responses"], "a GET never conflicts"
    export = schema["paths"]["/api/v1/events/{slug}/export.json"]["get"]
    assert "security" not in export and "401" in export["responses"]
    invite = schema["paths"]["/api/v1/events/{slug}/judges/invites"]["post"]
    assert {"401", "403", "409", "422"} <= set(invite["responses"])


def test_dashboard_offers_withdrawing_a_flagged_duplicate_and_a_phone_menu(app):
    page = demo(app, "organizer").get(f"/e/{SLUG}/organizer")
    assert page.status_code == 200
    assert "Withdraw duplicate" in page.text and "/projects/prj_41/withdraw" in page.text
    assert 'class="menu rail-menu"' in page.text and "Dashboard" in page.text


def test_printable_code_cards(app):
    org = demo(app, "organizer")
    r = org.post(
        f"/e/{SLUG}/organizer/voting/codes/print",
        {"codes": "ABCD-EFGH\tsomeone@example.test\nJKLM-NPQR"},
    )
    assert r.status_code == 200 and r.text.count("print-card__code") == 2
    assert "<svg" in r.text and "ABCD-EFGH" in r.text and "someone@example.test" in r.text
    assert "JKLM-NPQR" in r.text and f"/e/{SLUG}/vote/code" in r.text


def test_unpublishing_results_revokes_winner_certificates(client, auth):
    org = auth("organizer")
    prize = client.post(f"{S}/prizes", headers=org, json={"name": "Revoke prize"}).json()["prize"]
    client.post(f"{S}/actions/close_judging", headers=org)
    try:
        client.post(f"{S}/prizes/{prize['id']}/award", headers=org, json={"project": "prj_07"})
        assert client.post(f"{S}/actions/publish_results", headers=org).status_code == 200
        client.post(f"{S}/certificates/issue/winner", headers=org)
        winners = [
            c
            for c in client.get(f"{S}/certificates", headers=org).json()["certificates"]
            if c["kind"] == "winner" and c["revoked_at"] is None
        ]
        assert winners
        assert client.post(f"{S}/actions/unpublish_results", headers=org).status_code == 200
        after = [
            c
            for c in client.get(f"{S}/certificates", headers=org).json()["certificates"]
            if c["kind"] == "winner"
        ]
        assert after and all(c["revoked_at"] is not None for c in after)
        assert client.get(f"/api/v1/verify/{after[0]['serial']}").json()["status"] == "revoked"
    finally:
        client.post(f"{S}/actions/unpublish_results", headers=org)
        client.post(f"{S}/actions/open_judging", headers=org)
        client.delete(f"{S}/prizes/{prize['id']}", headers=org)
