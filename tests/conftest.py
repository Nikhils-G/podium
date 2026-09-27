"""Test database = a temporary SQLite file seeded from the real fixtures, so every test runs
against the same forty projects, thirty judges and awkward cases the judges will use."""

import os
import pathlib
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="podium-test-")
os.environ.update(
    {
        "PODIUM_DATA_DIR": TMP,
        "PODIUM_SECRET_KEY": "test-secret-key",
        "PODIUM_RATE_LIMIT_ENABLED": "false",
        "PODIUM_FIXTURES_PATH": str(ROOT / "fixtures" / "fixtures.json"),
        "PODIUM_DEMO_ACCOUNTS": "true",
        "PODIUM_OPEN_EVENT_CREATION": "true",  # tests create events as ordinary users
        "PODIUM_WEBHOOK_WORKER": "false",
    }
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from podium.config import get_settings  # noqa: E402
from podium.db import get_engine, get_sessionmaker  # noqa: E402
from podium.models import Base  # noqa: E402
from podium.security.sessions import demo_session_token  # noqa: E402
from podium.seed import run_seed  # noqa: E402

SLUG = "sample-hack-2026"


@pytest.fixture(scope="session", autouse=True)
def database():
    Base.metadata.create_all(get_engine())  # tests only; the app itself always migrates
    with get_sessionmaker()() as db:
        run_seed(db, get_settings())
    yield


@pytest.fixture(scope="session", autouse=True)
def audit_chain_intact(database):
    """After the whole suite: every write any test made kept the audit chain verifiable."""
    yield
    from podium.services.audit import verify_chain

    with get_sessionmaker()() as db:
        report = verify_chain(db)
    assert report.ok, f"audit chain broken at row {report.first_bad_id}"


@pytest.fixture(scope="session")
def app():
    from podium.main import app

    return app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def db():
    with get_sessionmaker()() as session:
        yield session


@pytest.fixture
def auth():
    """Headers exactly as the acceptance checker sends them: a raw Cookie header per role."""

    def _headers(role: str) -> dict[str, str]:
        return {"Cookie": f"session={demo_session_token(get_settings().secret_key, role)}"}

    return _headers
