"""The audit chain holds under concurrent writers, and every recorded action reads as a sentence."""

import pathlib
import re
import tempfile
import threading

from sqlalchemy.orm import sessionmaker

from podium.db import make_engine
from podium.models import AuditLog, Base
from podium.services import audit, events

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "podium"


def test_concurrent_appends_never_fork_the_chain():
    # Its own file database with the production pragmas (WAL, busy_timeout), not the shared one.
    path = pathlib.Path(tempfile.mkdtemp(prefix="podium-chain-")) / "chain.db"
    engine = make_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    start, errors = threading.Barrier(8), []

    def writer(n: int) -> None:
        try:
            with Session() as db:
                start.wait()
                for i in range(25):
                    audit.record(db, "review.draft_saved", "project", f"prj_{n}_{i}")
                    db.commit()
        except Exception as exc:  # surfaced below; a thread's exception is otherwise lost
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with Session() as db:
        report = audit.verify_chain(db)
    engine.dispose()
    assert (report.ok, report.entries, report.first_bad_id) == (True, 200, None)


def _recorded_actions() -> set[str]:
    literal = re.compile(r'audit(?:_service)?\.record\(\s*\w+\s*,\s*"([^"]+)"')
    found = {m for p in SRC.rglob("*.py") for m in literal.findall(p.read_text())}
    return found | {f"event.{a}" for a in events.ACTIONS} | {"event.imported"}


def test_every_recorded_action_has_a_readable_label():
    actions = _recorded_actions()
    assert {"user.login", "event.publish_results", "event.imported"} <= actions
    for action in sorted(actions):
        entry = AuditLog(action=action, entity_type="event", entity_id="evt_01", meta={})
        generic = f"{action.replace('.', ' ').replace('_', ' ').capitalize()} (evt_01)"
        assert audit.describe(entry) != generic, action


def test_lifecycle_and_import_labels_read_well():
    entry = AuditLog(action="event.close_judging", entity_type="event", entity_id="evt_01")
    assert audit.describe(entry) == "Closed judging"
    entry.action = "event.publish_results"
    assert audit.describe(entry) == "Published the results"
    entry.action = "event.imported"
    entry.meta = {
        "source": "fixtures.json",
        "sha256": "ab" * 32,
        "counts": {"projects": 41, "judges": 30, "reviews": 126},
    }
    assert audit.describe(entry) == "Imported 41 projects, 30 judges and 126 reviews"
    entry.meta = {"counts": {"criteria": 1, "events": 1}}
    assert audit.describe(entry) == "Imported 1 criterion and 1 event"
    entry.meta = {"source": "upload.json"}
    assert audit.describe(entry) == "Imported an event file"
    entry.action, entry.entity_id = "organizer.added", "usr_01"
    entry.meta = {"email": "ada@example.com", "via": "import"}
    assert audit.describe(entry) == "Added organizer usr_01 (by importing an event file)"
    entry.meta = {"email": "ada@example.com"}
    assert audit.describe(entry) == "Added organizer usr_01"


def test_long_entity_ids_are_clipped_to_the_column(db):
    # A failed sign-in stores the typed email; Postgres rejects anything over String(64).
    # Rolled back, so the shared database is left untouched.
    row = audit.record(db, "user.login_failed", "user", "x" * 300 + "@example.com")
    try:
        assert len(row.entity_id) == 64
    finally:
        db.rollback()
