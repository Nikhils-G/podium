"""Every form, posted through the web layer with CSRF, exactly as a browser would — including the
judge review form with its real field names and the vote control during an open window."""

import re
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from podium.config import get_settings
from podium.models import Project, Review, ReviewStatus, RubricCriterion
from podium.security.sessions import create_session, demo_session_token
from podium.services.auth import register
from tests.conftest import SLUG, get_sessionmaker


class Browser:
    """A TestClient that carries a session cookie and a CSRF cookie like a real browser."""

    def __init__(self, app, session_token: str | None = None):
        self.c = TestClient(app, follow_redirects=False)
        if session_token:
            self.c.cookies.set("session", session_token)
        self.get("/")  # primes the csrf cookie (login redirects when signed in)

    @property
    def csrf(self) -> str:
        return self.c.cookies.get("csrf")

    def get(self, path, **kw):
        return self.c.get(path, **kw)

    def post(self, path, data=None, *, htmx=False, files=None, **kw):
        data = dict(data or {})
        data.setdefault("csrf_token", self.csrf)
        headers = kw.pop("headers", {})
        if htmx:
            headers["HX-Request"] = "true"
        return self.c.post(path, data=data, files=files, headers=headers, **kw)


def demo(app, role):
    return Browser(app, demo_session_token(get_settings().secret_key, role))


def fresh_user(app, email):
    with get_sessionmaker()() as db:
        user = register(db, email=email, name=email.split("@")[0].title(), password="password123")
        token = create_session(db, user, days=1)
        db.commit()
    browser = Browser(app, token)
    browser.token = token
    return browser


# --- judge review form ----------------------------------------------------------------------------


def test_judge_review_form_draft_submit_reopen_resubmit(app):
    judge = demo(app, "judge_a")
    queue = judge.get(f"/e/{SLUG}/judge")
    assert queue.status_code == 200
    pid = re.search(rf"/e/{SLUG}/judge/review/(prj_\d+)", queue.text).group(1)
    page = judge.get(f"/e/{SLUG}/judge/review/{pid}")
    assert page.status_code == 200
    names = sorted(set(re.findall(r'name="(crt_[a-z0-9]+)"', page.text)))
    assert len(names) == 3, "three rubric criteria render as radio groups named by criterion id"
    # submitted in the fixtures → reopen first
    r = judge.post(f"/e/{SLUG}/judge/review/{pid}", {"action": "reopen"})
    assert r.status_code == 303
    # autosave a partial draft via htmx → real confirmation
    r = judge.post(f"/e/{SLUG}/judge/review/{pid}", {"action": "draft", names[0]: "2"}, htmx=True)
    assert r.status_code == 200 and "Draft saved" in r.text
    with get_sessionmaker()() as db:
        review = (
            db.execute(
                select(Review)
                .join(Project, Project.id == Review.project_id)
                .where(Project.public_id == pid, Review.event_id == 1)
            )
            .scalars()
            .first()
        )
        assert review.status == ReviewStatus.draft
        crit = db.execute(
            select(RubricCriterion).where(RubricCriterion.public_id == names[0])
        ).scalar_one()
        assert {i.criterion_id: i.value for i in review.items}[crit.id] == 2
    # submitting with a missing criterion is refused with a field error, values kept
    r = judge.post(f"/e/{SLUG}/judge/review/{pid}", {"action": "submit", names[0]: "4"})
    assert r.status_code == 422 and "before submitting" in r.text and 'value="4" checked' in r.text
    # full submit works
    r = judge.post(
        f"/e/{SLUG}/judge/review/{pid}",
        {"action": "submit", names[0]: "4", names[1]: "3", names[2]: "5", "comment": "Solid."},
    )
    assert r.status_code == 303
    page = judge.get(f"/e/{SLUG}/judge/review/{pid}")
    assert "Edit review" in page.text and "disabled" in page.text
    with get_sessionmaker()() as db:
        review = (
            db.execute(
                select(Review)
                .join(Project, Project.id == Review.project_id)
                .where(Project.public_id == pid)
            )
            .scalars()
            .first()
        )
        assert review.status == ReviewStatus.submitted and review.comment == "Solid."


# --- organizer forms on the fixture event -------------------------------------------------------


def test_organizer_forms_never_500_and_show_their_secrets(app):
    org = demo(app, "organizer")
    base = f"/e/{SLUG}/organizer"
    # invite → link shown once
    r = org.post(f"{base}/judges/invite", {"email": "forms-judge@example.test"})
    assert r.status_code == 200 and "/judge-invite/" in r.text and "Copy link" in r.text
    # duplicate pending invite refused
    r = org.post(f"{base}/judges/invite", {"email": "forms-judge@example.test"})
    assert r.status_code in (409, 200) and ("already" in r.text or "pending" in r.text.lower())
    # auto-assign preview (htmx partial) and apply
    r = org.post(
        f"{base}/assignments/auto/preview", {"reviews_per_project": "4", "seed": "1"}, htmx=True
    )
    assert r.status_code == 200 and "assignment" in r.text.lower()
    r = org.post(f"{base}/assignments/auto/apply", {"reviews_per_project": "4", "seed": "1"})
    assert r.status_code == 303 and "applied=" in r.headers["location"]
    # voting codes shown once
    r = org.post(f"{base}/voting/codes", {"count": "2", "emails": ""})
    assert r.status_code == 200 and len(re.findall(r"[A-Z2-9]{4}-[A-Z2-9]{4}", r.text)) >= 2
    # webhook secret shown once
    r = org.post(
        f"{base}/webhooks", {"url": "https://hooks.example.test/x", "events": "results.published"}
    )
    assert r.status_code == 200 and "Signing secret" in r.text
    # rubric weight update + settings save + track add/remove + prize add/remove
    page = org.get(f"{base}/rubric")
    crit = re.search(r'id="row-(crt_[a-z0-9]+)"', page.text).group(1)
    assert (
        org.post(
            f"{base}/rubric/{crit}", {"name": "Functionality", "description": "", "weight": "1.5"}
        ).status_code
        == 303
    )
    assert org.post(f"{base}/rubric", {"name": "Late", "weight": "1"}).status_code == 409, (
        "structure locked while judging is open"
    )
    settings_page = org.get(f"{base}/settings")
    assert settings_page.status_code == 200 and "Organizers" in settings_page.text
    r = org.post(
        f"{base}/settings",
        {
            "name": "Sample Hack 2026",
            "description": "41 projects across 8 tracks.",
            "submissions_open_at": "2026-01-30T00:00",
            "submissions_close_at": "2026-03-01T18:00",
            "voting_open_at": "",
            "voting_close_at": "",
            "max_team_size": "4",
            "is_public": "on",
        },
    )
    assert r.status_code == 303
    r = org.post(f"{base}/tracks", {"name": "Sweep track", "description": ""})
    assert r.status_code in (200, 303)
    assert org.post(f"{base}/tracks/trk_01/delete").status_code == 409, (
        "a track with projects stays"
    )
    tracks = org.get(f"{base}/settings").text
    new_track = re.findall(r"/tracks/(trk_[a-z0-9]+)/delete", tracks)[-1]
    assert org.post(f"{base}/tracks/{new_track}/delete").status_code in (200, 303)
    assert org.post(
        f"{base}/prizes", {"name": "Sweep prize", "amount": "$1", "description": "", "track": ""}
    ).status_code in (200, 303)
    # organizers: adding a participant is refused with a rendered message
    r = org.post(f"{base}/organizers", {"email": "priya1@example.org"})
    assert r.status_code == 409 and "participant" in r.text
    # results settings + audit verify partial
    assert (
        org.post(
            f"{base}/results/settings", {"method": "zscore", "basis": "normalized"}
        ).status_code
        == 303
    )
    r = org.get(f"{base}/audit/verify", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Chain verified" in r.text
    # certificates issue (judge records need judging closed)
    r = org.post(f"{base}/certificates/issue/participation")
    assert r.status_code == 200 and "issued" in r.text
    # import dry run keeps working through the form (multipart)
    export = org.get(f"/api/v1/events/{SLUG}/export.json").content
    r = org.post(
        f"{base}/data/import",
        {"mode": "dry_run"},
        files={"file": ("export.json", export, "application/json")},
    )
    assert r.status_code == 200 and "Dry run" in r.text and "Apply this import" in r.text
    token = re.search(r'name="token" value="([^"]+)"', r.text).group(1)
    r = org.post(f"{base}/data/import", {"mode": "apply_saved", "token": token})
    assert r.status_code == 200 and "Import applied" in r.text
    r = org.post(f"{base}/data/import", {"mode": "apply_saved", "token": token})
    assert r.status_code == 410, "a used dry run can't be applied twice"


# --- participant journey + vote control on a fresh open event ---


def test_participant_journey_and_vote_button_through_the_web(app):
    org = demo(app, "organizer")
    now = datetime.now(UTC)
    r = org.post(
        "/events/new",
        {
            "name": "Forms Night",
            "description": "",
            "is_public": "on",
            "max_team_size": "3",
            "submissions_open_at": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
            "submissions_close_at": (now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M"),
            "voting_open_at": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
            "voting_close_at": (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
        },
    )
    assert r.status_code == 303
    slug = r.headers["location"].split("/e/")[1].split("/")[0]
    assert org.post(
        f"/e/{slug}/organizer/tracks", {"name": "Main", "description": ""}
    ).status_code in (200, 303)
    alice = fresh_user(app, "alice-forms@example.test")
    r = alice.post(f"/e/{slug}/team", {"name": "Owls"})
    assert r.status_code == 303
    team_page = alice.get(f"/e/{slug}/team").text
    code = re.search(r"/join/(join_[a-z0-9]+)", team_page).group(1)
    bob = fresh_user(app, "bob-forms@example.test")
    assert bob.get(f"/join/{code}").status_code == 200
    assert bob.post(f"/join/{code}").status_code == 303
    # draft is invisible, submit makes it public, Enter-safe default action is a save
    r = alice.post(
        f"/e/{slug}/submit",
        {
            "title": "Owl Lens",
            "summary": "Watches builds.",
            "description": "",
            "repo_url": "https://example.org/owl",
            "demo_url": "",
            "video_url": "",
            "track": "",
            "action": "save",
        },
    )
    assert r.status_code == 303 and r.headers["location"].endswith("?saved=draft")
    pid = r.headers["location"].rsplit("/", 1)[-1].split("?")[0]
    assert "Owl Lens" not in alice.get(f"/e/{slug}/projects").text
    draft_page = alice.get(f"/e/{slug}/projects/{pid}?saved=draft").text
    assert "Draft saved" in draft_page and "Draft — not submitted" in draft_page
    assert "Submit project" in draft_page and 'name="body"' not in draft_page, (
        "no comments on drafts"
    )
    assert "Your draft" in alice.get(f"/e/{slug}/submit").text
    # the Submit button on the draft page submits as-is
    r = alice.post(f"/e/{slug}/projects/{pid}/submit")
    assert r.status_code == 303 and r.headers["location"].endswith("?saved=submitted")
    assert "Project submitted" in alice.get(f"/e/{slug}/projects/{pid}?saved=submitted").text
    r = alice.post(
        f"/e/{slug}/submit",
        {
            "title": "Owl Lens",
            "summary": "Watches builds.",
            "description": "",
            "repo_url": "https://example.org/owl",
            "demo_url": "",
            "video_url": "",
            "track": "",
            "action": "unsubmit",
        },
    )
    assert r.status_code == 303 and r.headers["location"].endswith("?saved=unsubmitted")
    r = alice.post(
        f"/e/{slug}/submit",
        {
            "title": "Owl Lens",
            "summary": "Watches builds.",
            "description": "",
            "repo_url": "https://example.org/owl",
            "demo_url": "",
            "video_url": "",
            "track": "",
            "action": "submit",
        },
    )
    assert r.status_code == 303
    assert "Owl Lens" in alice.get(f"/e/{slug}/projects").text
    # a voter from outside the team sees and uses the vote button; the team member does not get one
    carol = fresh_user(app, "carol-forms@example.test")
    page = carol.get(f"/e/{slug}/projects/{pid}")
    assert page.status_code == 200 and "Vote for this project" in page.text
    r = carol.post(f"/e/{slug}/projects/{pid}/vote", {}, htmx=True)
    assert r.status_code == 200 and "You voted" in r.text
    assert (
        "You voted" in carol.get(f"/e/{slug}/projects").text
        or "Voted" in carol.get(f"/e/{slug}/projects").text
    )
    r = carol.post(f"/e/{slug}/projects/{pid}/unvote", {}, htmx=True)
    assert r.status_code == 200 and "Vote for this project" in r.text
    r = alice.post(f"/e/{slug}/projects/{pid}/vote", {}, htmx=True)
    assert r.status_code == 403 and "own team" in r.text
    # comment via form, then withdraw + restore
    r = carol.post(f"/e/{slug}/projects/{pid}/comments", {"body": "Nice."}, htmx=True)
    assert r.status_code == 200 and "Nice." in r.text
    assert alice.post(f"/e/{slug}/projects/{pid}/withdraw").status_code == 303
    assert alice.post(f"/e/{slug}/projects/{pid}/restore").status_code == 303


def test_login_lands_each_role_on_its_console(app):
    now = datetime.now(UTC)
    org = fresh_user(app, "landing-org@example.test")
    r = org.post(
        "/events/new",
        {
            "name": "Landing Night",
            "description": "",
            "is_public": "on",
            "max_team_size": "3",
            "submissions_open_at": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
            "submissions_close_at": (now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M"),
            "voting_open_at": "",
            "voting_close_at": "",
        },
    )
    assert r.status_code == 303
    slug = r.headers["location"].split("/e/")[1].split("/")[0]
    invite = org.c.post(
        f"/api/v1/events/{slug}/judges/invites",
        json={"email": "landing-judge@example.test", "tracks": []},
    ).json()["invite"]["link"]
    judge = fresh_user(app, "landing-judge@example.test")
    assert judge.post("/judge-invite/" + invite.rsplit("/", 1)[-1]).status_code == 303
    part = fresh_user(app, "landing-part@example.test")
    assert part.c.post(f"/api/v1/events/{slug}/teams", json={"name": "Landers"}).status_code == 201
    for email, expect in (
        ("landing-org@example.test", f"/e/{slug}/organizer"),
        ("landing-judge@example.test", f"/e/{slug}/judge"),
        ("landing-part@example.test", f"/e/{slug}"),
    ):
        b = Browser(app)
        r = b.post("/login", {"email": email, "password": "password123"})
        assert r.status_code == 303 and r.headers["location"] == expect, (
            email,
            r.headers.get("location"),
        )
        assert b.get(expect).status_code == 200
