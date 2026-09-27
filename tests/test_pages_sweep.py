"""Every web page, for every role, must render without a server error. This is the test that
would have caught the six 500s and the broken vote/judge pages before a judge did."""

import re

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select

from podium.config import get_settings
from podium.models import Team
from podium.security.sessions import demo_session_token
from tests.conftest import SLUG, get_sessionmaker

ROLES = [None, "participant", "judge_a", "judge_b", "organizer", "admin"]
ALLOWED = {200, 303, 401, 403, 404, 405, 409}


def _auth(role: str) -> dict[str, str]:
    return {"Cookie": f"session={demo_session_token(get_settings().secret_key, role)}"}


@pytest.fixture(scope="module")
def params(app):
    """Real values for every path parameter that appears in a GET route."""
    from fastapi.testclient import TestClient

    client = TestClient(app)
    org = _auth("organizer")
    with get_sessionmaker()() as db:
        code = db.execute(select(Team.invite_code).where(Team.public_id == "tm_01")).scalar_one()
    invite = client.post(
        f"/api/v1/events/{SLUG}/judges/invites",
        headers=org,
        json={"email": "sweep-judge@example.test", "tracks": []},
    ).json()["invite"]["link"]
    token = invite.rsplit("/", 1)[-1]
    client.post(f"/api/v1/events/{SLUG}/actions/close_judging", headers=org)
    client.post(f"/api/v1/events/{SLUG}/certificates/issue/judge", headers=org)
    client.post(f"/api/v1/events/{SLUG}/actions/open_judging", headers=org)
    serial = client.get(
        f"/api/v1/events/{SLUG}/judges/me/records", headers=_auth("judge_a")
    ).json()["records"][0]["serial"]
    return {
        "slug": SLUG,
        "pid": "prj_07",
        "code": code,
        "token": token,
        "serial": serial,
        "criterion_id": "crt_none",
        "judge_id": "jdg_24",
        "hook_id": "whk_none",
        "track_id": "trk_01",
        "prize_id": "prz_none",
        "assignment_id": "1",
        "delivery_id": "1",
        "vote_id": "1",
        "cid": "cmt_none",
        "user_id": "usr_none",
        "token_id": "1",
        "role": "organizer",
        "name": "scores",
        "action": "publish_event",
    }


def web_get_routes(app):
    """Every HTML GET route. FastAPI 0.141 nests included routers, so `app.routes` holds router
    wrappers, not APIRoutes; walk the effective routes the way the app itself resolves them."""
    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:  # older FastAPI keeps routes flat
        routes = [r for r in app.routes if isinstance(r, APIRoute)]
    else:
        routes = list(iter_route_contexts(app.routes))
    out = []
    for route in routes:
        path = getattr(route, "path_format", None) or getattr(route, "path", "")
        if "GET" in (getattr(route, "methods", None) or ()) and not path.startswith("/api/v1"):
            out.append(path)
    return sorted(set(out))


def test_the_sweep_really_covers_the_app(app):
    """This sweep once iterated zero routes and passed; never again."""
    routes = web_get_routes(app)
    assert len(routes) > 40
    assert "/e/{slug}/organizer/integrations" in routes and "/api/docs" in routes


def fill(path: str, values: dict) -> str:
    return re.sub(r"{(\w+)}", lambda m: str(values.get(m.group(1), "x")), path)


@pytest.mark.parametrize("role", ROLES)
def test_every_page_renders_for(role, app, client, auth, params):
    headers = auth(role) if role else {}
    failures = []
    for path in web_get_routes(app):
        url = fill(path, params)
        for extra in ({}, {"HX-Request": "true"}):
            r = client.get(url, headers={**headers, **extra}, follow_redirects=False)
            if r.status_code not in ALLOWED:
                failures.append(
                    f"{role or 'visitor'} GET {url} {extra and 'htmx' or ''} → {r.status_code}"
                )
            if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
                for marker in ("UndefinedError", "Traceback", "jinja2.exceptions"):
                    assert marker not in r.text, f"{url} leaked {marker}"
                ids = re.findall(r'\sid="([^"]+)"', r.text)
                duplicates = sorted({i for i in ids if ids.count(i) > 1})
                if duplicates:
                    failures.append(f"{role or 'visitor'} GET {url} duplicate ids {duplicates}")
    assert not failures, "\n".join(failures)


def test_wrong_slug_and_project_give_404_pages(client):
    r = client.get("/e/no-such-event")
    assert r.status_code == 404 and "find that page" in r.text.lower() or r.status_code == 404
    r = client.get(f"/e/{SLUG}/projects/prj_nope")
    assert r.status_code == 404
