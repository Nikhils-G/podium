"""Every time says whose time it is: local times carry the zone, deadlines keep UTC beside them,
and the membership lines render their date as a localisable <time>."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from podium.db import get_sessionmaker
from podium.models import User
from podium.services import navigation

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "src" / "podium" / "static" / "js" / "app.js").read_text()


def test_local_times_carry_their_zone_and_deadlines_keep_utc():
    assert 'timeZoneName: "short"' in JS and "function withZone(" in JS
    assert "fmtLocal(d)" in JS and "return withZone(d.getDate()" in JS
    assert 'abs += " · " + fmtUtcTime(d)' in JS, "countdowns state both clocks"


def test_membership_lines_render_dates_as_time_elements(app, client, auth):
    org, now = auth("organizer"), datetime.now(UTC)
    client.post(
        "/api/v1/events",
        headers=org,
        json={
            "name": "Zone Night",
            "is_public": True,
            "submissions_open_at": (now - timedelta(days=3)).isoformat(),
            "submissions_close_at": (now - timedelta(days=2)).isoformat(),
            "voting_open_at": (now - timedelta(hours=1)).isoformat(),
            "voting_close_at": (now + timedelta(days=1)).isoformat(),
        },
    )
    with get_sessionmaker()() as db:
        user = db.execute(select(User).where(User.email == "organizer@podium.local")).scalar_one()
        rows = [m for m in navigation.memberships(db, user) if m.event.name == "Zone Night"]
    assert rows and rows[0].next_at is not None and "UTC" not in rows[0].next_label
    page = client.get("/", headers=org).text
    assert "Voting open until <time" in page, "the date renders through when()"
