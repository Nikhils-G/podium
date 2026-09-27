"""Secure defaults: the first account on an empty instance is its admin; nobody else is."""

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from podium.config import Settings, get_settings
from podium.models import AuditLog, Base, User
from podium.services.auth import register
from tests.conftest import SLUG, get_sessionmaker
from tests.test_forms_sweep import fresh_user


def test_first_account_on_an_empty_instance_becomes_admin():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        first = register(db, email="owner@example.test", name="Owner", password="password123")
        second = register(db, email="next@example.test", name="Next", password="password123")
        assert first.is_admin and not second.is_admin
        actions = list(db.execute(select(AuditLog.action).order_by(AuditLog.id)).scalars())
        assert actions.count("user.bootstrap_admin") == 1


def test_registering_on_a_seeded_instance_never_grants_admin(app):
    fresh_user(app, "bootstrap-check@example.test")
    with get_sessionmaker()() as db:
        user = db.execute(
            select(User).where(User.email == "bootstrap-check@example.test")
        ).scalar_one()
        assert not user.is_admin


def test_event_creation_is_admin_only_unless_opted_in(app, client, auth):
    from podium.config import get_settings as real_get_settings

    locked = real_get_settings().model_copy(update={"open_event_creation": False})
    app.dependency_overrides[real_get_settings] = lambda: locked
    try:
        body = {"name": "Locked Night", "is_public": False}
        assert client.post("/api/v1/events", headers=auth("judge_a"), json=body).status_code == 403
        r = client.post("/api/v1/events", headers=auth("organizer"), json=body)
        assert r.status_code == 201, "the demo organizer is an instance admin"
        assert client.get("/events/new", headers=auth("judge_a")).status_code == 403
    finally:
        app.dependency_overrides.pop(real_get_settings, None)


def test_defaults_are_closed(monkeypatch):
    for name in ("PODIUM_DEMO_ACCOUNTS", "PODIUM_OPEN_EVENT_CREATION"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    assert settings.demo_accounts is False and settings.open_event_creation is False
    assert get_settings().demo_accounts is True, "the test session opts in explicitly"


def test_new_tokens_are_read_only_and_expire(client, auth):
    r = client.post("/api/v1/me/tokens", headers=auth("organizer"), json={"name": "default"})
    assert r.status_code == 201
    listed = client.get("/api/v1/me/tokens", headers=auth("organizer")).json()["tokens"]
    mine = [t for t in listed if t["name"] == "default"][0]
    assert mine["scope"] == "read" and mine["expires_at"] is not None
    r = client.post(
        "/api/v1/me/tokens",
        headers=auth("organizer"),
        json={"name": "forever", "scope": "write", "expires_in_days": None},
    )
    assert r.status_code == 201
    forever = [
        t
        for t in client.get("/api/v1/me/tokens", headers=auth("organizer")).json()["tokens"]
        if t["name"] == "forever"
    ][0]
    assert forever["scope"] == "write" and forever["expires_at"] is None
    assert SLUG  # keep the fixture import meaningful
