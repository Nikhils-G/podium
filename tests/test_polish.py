"""Round-4 polish: forms that ask before destroying, notices after every change, honest dates."""

import re

from tests.conftest import SLUG
from tests.test_forms_sweep import demo

ORG = f"/e/{SLUG}/organizer"


def test_printable_codes_work_without_emails(app):
    r = demo(app, "organizer").post(f"{ORG}/voting/codes", {"count": "2", "emails": ""})
    assert r.status_code == 200
    assert r.text.count('action="/e/sample-hack-2026/organizer/voting/codes/print"') == 1
    assert r.text.count("<textarea") == r.text.count("</textarea>")
    visible = re.search(r'aria-label="New voting codes">([^<]*)</textarea>', r.text).group(1)
    assert len(re.findall(r"[A-Z2-9]{4}-[A-Z2-9]{4}", visible)) == 2


def test_time_zone_note_keeps_its_own_sentence(app):
    org = demo(app, "organizer")
    voting = org.get(f"{ORG}/voting").text
    assert (
        "<span data-tz-note>Times are in UTC.</span> Leave both empty to keep voting off." in voting
    )
    settings = org.get(f"{ORG}/settings").text
    assert "enforced server-side to the minute" in settings  # once, on the deadline field
    assert settings.count("enforced server-side to the minute") == 1


def test_destructive_actions_ask_first(app):
    org = demo(app, "organizer")
    org.post(f"{ORG}/prizes", {"name": "Polish prize", "amount": "", "track": ""})
    settings = org.get(f"{ORG}/settings").text
    assert re.search(r'/tracks/trk_[a-z0-9]+/delete"[^>]*data-confirm="Remove the', settings)
    assert re.search(r'/prizes/prz_[a-z0-9]+/delete"[^>]*data-confirm="Remove the', settings)
    assignments = org.get(f"{ORG}/assignments").text
    assert "queue? A draft review they started is discarded." in assignments


def test_changes_are_confirmed_with_a_notice(app, client, auth, monkeypatch):
    from podium.services import certificates

    org = demo(app, "organizer")
    certs = client.get(f"/api/v1/events/{SLUG}/certificates", headers=auth("organizer")).json()
    live = [c for c in certs["certificates"] if not c.get("revoked_at")]
    if live:  # the shared event's certificates belong to other tests: exercise the route only
        monkeypatch.setattr(certificates, "revoke", lambda *args, **kwargs: None)
        r = org.post(f"{ORG}/certificates/{live[0]['serial']}/revoke")
        assert r.status_code == 303 and r.headers["location"].endswith("?saved=revoked")
    assert "Certificate revoked." in org.get(f"{ORG}/certificates?saved=revoked").text
    page = org.get(f"{ORG}/voting?saved=voided").text
    assert "Vote voided." in page
    assert "Assignment removed." in org.get(f"{ORG}/assignments?saved=unassigned").text


def test_dates_use_the_one_time_format(app):
    org = demo(app, "organizer")
    dashboard = org.get(ORG).text
    assert re.search(r"<time datetime=\"[^\"]+\" data-local data-short", dashboard)
    judging = org.get(f"/e/{SLUG}/judging").text
    assert "Signed audit anchor" in judging and "if false" not in judging


def test_field_ids_are_unique_on_forms_with_several_names(app):
    org = demo(app, "organizer")
    for path in (f"{ORG}/settings", "/account"):
        ids = re.findall(r'\sid="([^"]+)"', org.get(path).text)
        assert len(ids) == len(set(ids)), path
    account = org.get("/account").text
    assert 'id="f-token-name"' in account and 'id="f-name"' in account


def test_confirmation_dialog_is_centred_and_destructive_actions_are_red(client):
    """A global margin reset once pinned every confirmation to the top-left corner."""
    css = client.get("/static/css/app.css").text
    assert ".confirm { margin: auto; }" in css
    script = client.get("/static/js/app.js").text
    assert "Remove|Delete|Revoke|Void|Withdraw|Leave|Archive|Unpublish" in script
