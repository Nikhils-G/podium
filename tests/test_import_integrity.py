"""Imports touch only their own event: ids that belong to another event are refused before
anything is written, creating an event by import follows the event-creation rule, and applied
imports are audited."""

import json
import uuid

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from podium.config import get_settings
from podium.models import AuditLog, Base, Event, Project, Team
from podium.security.sessions import create_session
from podium.seed import run_seed
from podium.services.auth import register
from tests.conftest import SLUG, get_sessionmaker
from tests.test_forms_sweep import demo


def _probe(event_id: str, team: str, project: str, name: str) -> dict:
    return {
        "event": {"id": event_id, "name": name, "submissions_close": "2026-04-01T18:00:00Z"},
        "tracks": [],
        "judges": [],
        "teams": [{"id": team, "name": "OVERWRITTEN team", "members": ["stranger@x.test"]}],
        "projects": [
            {
                "id": project,
                "team": team,
                "title": "OVERWRITTEN",
                "submitted_at": "2026-03-30T12:00:00Z",
            }
        ],
        "scores": [],
        "podium": {"projects": [{"id": project, "status": "withdrawn"}]},
    }


def _untouched():
    with get_sessionmaker()() as db:
        project = db.execute(select(Project).where(Project.public_id == "prj_01")).scalar_one()
        team = db.execute(select(Team).where(Team.public_id == "tm_01")).scalar_one()
        probe = db.execute(
            select(Event).where(Event.public_id == "evt_probe_foreign")
        ).scalar_one_or_none()
        return project.title, project.status.value, team.name, probe


def test_ids_of_another_event_are_refused_on_apply(client, auth):
    before = _untouched()
    data = _probe("evt_probe_foreign", "tm_01", "prj_01", "Probe Foreign")
    r = client.post("/api/v1/events/import?dry_run=false", headers=auth("organizer"), json=data)
    assert r.status_code == 409, r.text
    assert "prj_01" in r.text and "tm_01" in r.text
    after = _untouched()
    assert after == before and after[0] == "Glass Signal" and after[3] is None


def test_ids_of_another_event_are_refused_on_dry_run(client, auth):
    data = _probe("evt_probe_foreign", "tm_01", "prj_01", "Probe Foreign")
    r = client.post("/api/v1/events/import?dry_run=true", headers=auth("organizer"), json=data)
    assert r.status_code == 409, r.text


def test_the_data_page_refuses_ids_of_another_event(app, client, auth):
    tag = uuid.uuid4().hex[:8]
    scratch = _probe(f"evt_{tag}", f"tm_{tag}", f"prj_{tag}", f"Scratch Import {tag}")
    r = client.post("/api/v1/events/import?dry_run=false", headers=auth("organizer"), json=scratch)
    assert r.status_code == 200, r.text
    # a Sample Hack file that reaches into the scratch event's team
    reach = _probe("evt_01", f"tm_{tag}", f"prj_{tag}b", "Sample Hack 2026")
    r = demo(app, "organizer").post(
        f"/e/{SLUG}/organizer/data/import",
        {"mode": "dry_run"},
        files={"file": ("reach.json", json.dumps(reach).encode(), "application/json")},
    )
    assert r.status_code == 409 and f"tm_{tag}" in r.text


def test_creating_an_event_by_import_follows_the_admin_rule(app, client, auth):
    tag = uuid.uuid4().hex[:8]
    data = _probe(f"evt_{tag}", f"tm_{tag}", f"prj_{tag}", f"Locked Import {tag}")
    locked = get_settings().model_copy(update={"open_event_creation": False})
    app.dependency_overrides[get_settings] = lambda: locked
    try:
        r = client.post(
            "/api/v1/events/import?dry_run=false", headers=auth("participant"), json=data
        )
        assert r.status_code == 403, r.text
        r = client.post("/api/v1/events/import?dry_run=true", headers=auth("organizer"), json=data)
        assert r.status_code == 200, "the demo organizer is an instance admin"
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_the_import_body_is_capped(client, auth):
    r = client.post(
        "/api/v1/events/import",
        headers=auth("organizer") | {"Content-Type": "application/json"},
        content=b" " * (20 * 1024 * 1024 + 1),
    )
    assert r.status_code == 413


def test_an_applied_import_is_audited(client):
    tag = uuid.uuid4().hex[:8]
    with get_sessionmaker()() as db:
        user = register(db, email=f"imp_{tag}@x.test", name="Importer", password="password123")
        token = create_session(db, user, days=1)
        db.commit()
    data = _probe(f"evt_{tag}", f"tm_{tag}", f"prj_{tag}", f"Audited Import {tag}")
    r = client.post(
        "/api/v1/events/import?dry_run=false",
        headers={"Cookie": f"session={token}"},
        json=data,
    )
    assert r.status_code == 200, r.text
    with get_sessionmaker()() as db:
        event = db.execute(select(Event).where(Event.public_id == f"evt_{tag}")).scalar_one()
        rows = {
            a.action: a
            for a in db.execute(select(AuditLog).where(AuditLog.event_id == event.id)).scalars()
        }
    assert rows["event.imported"].actor_id == user.id
    assert rows["event.imported"].meta["counts"]["projects"] == 1
    assert len(rows["event.imported"].meta["sha256"]) == 64
    assert rows["organizer.added"].meta["via"] == "import"


def test_seeding_twice_records_one_import():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        run_seed(db, get_settings())
        run_seed(db, get_settings())
        count = db.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "event.imported")
        ).scalar()
    engine.dispose()
    assert count == 1
