"""T4: bearer tokens, webhooks (signature, retry schedule), signed certificates, embed, docs,
import/export round trip."""

import hashlib
import hmac
import json

from sqlalchemy import select

from podium.config import get_settings
from podium.models import DeliveryStatus, Event, WebhookDelivery
from podium.services import certificates as cert_service
from podium.services import importexport, webhooks
from tests.conftest import SLUG, get_sessionmaker

S = f"/api/v1/events/{SLUG}"


def test_bearer_token_round_trip(client, auth):
    r = client.post("/api/v1/me/tokens", headers=auth("organizer"), json={"name": "ci"})
    assert r.status_code == 201
    secret = r.json()["token"]["secret"]
    assert secret.startswith("pdm_")
    bearer = {"Authorization": f"Bearer {secret}"}
    assert (
        client.get("/api/v1/me", headers=bearer).json()["user"]["email"] == "organizer@podium.local"
    )
    assert client.get(f"{S}/exports/scores.csv", headers=bearer).status_code == 200
    token_id = r.json()["token"]["id"]
    assert (
        client.delete(f"/api/v1/me/tokens/{token_id}", headers=auth("organizer")).status_code == 204
    )
    assert client.get("/api/v1/me", headers=bearer).status_code == 401
    assert client.get("/api/v1/me", headers={"Authorization": "Bearer pdm_nope"}).status_code == 401


def test_webhook_emit_sign_and_retry(client, auth):
    r = client.post(
        f"{S}/webhooks",
        headers=auth("organizer"),
        json={"url": "https://hooks.example.test/podium", "events": ["results.published"]},
    )
    assert r.status_code == 201
    secret = r.json()["webhook"]["secret"]
    # results can only be published once judging is closed (integrity guard)
    assert client.post(f"{S}/actions/publish_results", headers=auth("organizer")).status_code == 409
    assert client.post(f"{S}/actions/close_judging", headers=auth("organizer")).status_code == 200
    assert client.post(f"{S}/actions/publish_results", headers=auth("organizer")).status_code == 200
    sent = []

    def fake_sender(url, body, headers):
        sent.append((url, body, headers))
        return (500, "HTTP 500")

    with get_sessionmaker()() as db:
        attempted = webhooks.deliver_pending(db, sender=fake_sender)
        assert attempted >= 1
        url, body, headers = sent[-1]
        assert url == "https://hooks.example.test/podium"
        assert headers["X-Podium-Event"] == "results.published"
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert headers["X-Podium-Signature"] == expected
        assert json.loads(body)["type"] == "results.published"
        delivery = (
            db.execute(select(WebhookDelivery).order_by(WebhookDelivery.id.desc()))
            .scalars()
            .first()
        )
        assert delivery.status == DeliveryStatus.pending and delivery.attempts == 1
        assert delivery.next_attempt_at is not None and delivery.last_error == "HTTP 500"
        # force the retry due now, succeed
        from podium.models import utcnow

        delivery.next_attempt_at = utcnow()
        db.commit()
        webhooks.deliver_pending(db, sender=lambda u, b, h: (200, ""))
        db.refresh(delivery)
        assert delivery.status == DeliveryStatus.delivered and delivery.delivered_at is not None
    r = client.get(f"{S}/webhooks/deliveries", headers=auth("organizer"))
    assert r.json()["deliveries"][0]["status"] == "delivered"
    client.post(f"{S}/actions/unpublish_results", headers=auth("organizer"))
    client.post(f"{S}/actions/open_judging", headers=auth("organizer"))
    assert client.get(f"{S}/webhooks", headers=auth("judge_a")).status_code == 403


def test_certificates_issue_verify_revoke_and_tamper(client, auth):
    # other tests may have issued records already; the invariant is one record per judge, ever
    assert (
        client.post(f"{S}/certificates/issue/judge", headers=auth("organizer")).status_code == 409
    )
    client.post(f"{S}/actions/close_judging", headers=auth("organizer"))
    r = client.post(f"{S}/certificates/issue/judge", headers=auth("organizer"))
    assert r.status_code == 200 and 0 <= r.json()["issued"] <= 30
    records = client.get(f"{S}/certificates", headers=auth("organizer"), params={"kind": "judge"})
    judge_records = [c for c in records.json()["certificates"] if c["kind"] == "judge"]
    assert len({c["payload"]["recipient"]["id"] for c in judge_records}) == len(judge_records) == 30
    assert (
        client.post(f"{S}/certificates/issue/judge", headers=auth("organizer")).json()["issued"]
        == 0
    )
    mine = client.get(f"{S}/judges/me/records", headers=auth("judge_a")).json()["records"]
    assert len(mine) == 1 and mine[0]["payload"]["reviews_submitted"] == 11
    serial = mine[0]["serial"]
    v = client.get(f"/api/v1/verify/{serial}").json()
    assert v["status"] == "valid"
    assert client.get(f"/certificates/{serial}").status_code == 200
    assert client.get(f"/verify/{serial}").status_code == 200
    assert client.get(f"/verify/{serial}.svg").headers["content-type"].startswith("image/svg+xml")
    assert client.get("/api/v1/verify/PDM-NOPE").json()["status"] == "not_found"
    key = client.get("/.well-known/podium-signing-key.json").json()["public_key_hex"]
    record = client.get(f"/certificates/{serial}.json").json()
    assert cert_service.verify_signature(key, record["payload"], record["signature"])
    tampered = dict(record["payload"], reviews_submitted=99)
    assert not cert_service.verify_signature(key, tampered, record["signature"])
    assert (
        client.post(f"{S}/certificates/{serial}/revoke", headers=auth("organizer")).status_code
        == 200
    )
    assert client.get(f"/api/v1/verify/{serial}").json()["status"] == "revoked"
    assert (
        client.post(f"{S}/certificates/issue/winner", headers=auth("organizer")).status_code == 409
    )
    client.post(f"{S}/actions/open_judging", headers=auth("organizer"))


def test_participation_certificates_cover_members_once(client, auth):
    r = client.post(f"{S}/certificates/issue/participation", headers=auth("organizer"))
    assert r.status_code == 200 and 0 <= r.json()["issued"] <= 91
    certs = client.get(f"{S}/certificates", headers=auth("organizer")).json()["certificates"]
    participation = [c for c in certs if c["kind"] == "participation"]
    assert len({c["payload"]["recipient"]["id"] for c in participation}) == len(participation) == 91
    assert (
        client.post(f"{S}/certificates/issue/participation", headers=auth("organizer")).json()[
            "issued"
        ]
        == 0
    )


def test_embed_is_frameable_and_gallery_api_has_cors(client):
    r = client.get(f"/e/{SLUG}/embed")
    assert r.status_code == 200 and "frame-ancestors *" in r.headers["content-security-policy"]
    assert "X-Frame-Options" not in r.headers
    assert "Glass Signal" in r.text
    normal = client.get(f"/e/{SLUG}/projects")
    assert "frame-ancestors 'none'" in normal.headers["content-security-policy"]
    api = client.get(f"{S}/projects")
    assert api.headers.get("access-control-allow-origin") == "*"


def test_api_docs_and_openapi_are_served_locally(client):
    r = client.get("/api/docs/console")
    assert r.status_code == 200 and "/static/vendor/swagger-ui/swagger-ui-bundle.js" in r.text
    assert "cdn" not in r.text.lower()
    assert "API reference" in client.get("/api/docs").text
    spec = client.get("/api/openapi.json").json()
    assert (
        spec["info"]["title"] == "Podium"
        and "/api/v1/events/{slug}/judges/me/reviews" in spec["paths"]
    )
    assert client.get("/static/vendor/swagger-ui/swagger-ui-bundle.js").status_code == 200


def test_export_import_round_trip(client, auth):
    r = client.get(f"{S}/export.json", headers=auth("organizer"))
    assert r.status_code == 200
    data = r.json()
    assert len(data["projects"]) == 41 and len(data["scores"]) == 126 and data["podium"]["rubric"]
    dry = client.post(
        "/api/v1/events/import?dry_run=true", headers=auth("organizer"), json=data
    ).json()
    assert dry["dry_run"] is True and dry["counts"] == {}, (
        "everything already exists → nothing to add"
    )
    with get_sessionmaker()() as db:
        event = db.execute(select(Event).where(Event.slug == SLUG)).scalar_one()
        again = importexport.export_event(db, event)
    for key in ("event", "tracks", "judges", "teams", "projects", "scores"):
        assert again[key] == data[key]
    assert (
        client.post("/api/v1/events/import", headers=auth("judge_a"), json=data).status_code == 403
    )
    bad = client.post(
        "/api/v1/events/import?dry_run=true", headers=auth("organizer"), json={"nope": 1}
    )
    assert bad.status_code == 422


def test_import_creates_a_new_event_for_its_organizer(client, auth):
    data = {
        "event": {
            "id": "evt_new",
            "name": "Imported Hack",
            "submissions_close": "2026-04-01T18:00:00Z",
        },
        "tracks": [{"id": "trk_x", "name": "X"}],
        "judges": [],
        "teams": [{"id": "tm_x", "name": "T", "members": ["x@x.test"]}],
        "projects": [
            {
                "id": "prj_x",
                "team": "tm_x",
                "track": "trk_x",
                "title": "Imported project",
                "summary": "s",
                "repo_url": "https://example.org/x",
                "submitted_at": "2026-03-30T12:00:00Z",
            }
        ],
        "scores": [],
    }
    r = client.post("/api/v1/events/import?dry_run=false", headers=auth("participant"), json=data)
    assert r.status_code == 200 and r.json()["event"] == "imported-hack"
    assert (
        client.get("/api/v1/events/imported-hack", headers=auth("participant")).json()["event"][
            "stage"
        ]
        == "closed"
    )
    assert "Imported project" in client.get("/e/imported-hack/projects").text
    assert (
        client.get("/e/imported-hack/organizer", headers=auth("participant")).status_code == 200
    ), "importer organizes it"
    assert get_settings().demo_accounts
