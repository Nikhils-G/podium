"""Seeding = importing. The fixture loader is the same code path as bulk import (T4)."""

from dataclasses import dataclass, field

from sqlalchemy.orm import Session as DbSession

from podium.config import Settings
from podium.seed.demo import DemoLogin, ensure_demo_accounts
from podium.seed.fixtures import ImportReport, import_fixtures_file


@dataclass
class SeedSummary:
    report: ImportReport | None = None
    logins: list[DemoLogin] = field(default_factory=list)


def run_seed(db: DbSession, settings: Settings) -> SeedSummary:
    summary = SeedSummary()
    if settings.seed_fixtures and settings.fixtures_path.exists():
        summary.report = import_fixtures_file(
            db, settings.fixtures_path, default_password=settings.demo_password
        )
    if settings.demo_accounts:
        summary.logins = ensure_demo_accounts(db, settings)
    if summary.report is not None and summary.report.event_slug:
        # demo richness for the pairwise mode: comparisons implied by the fixture scores
        from sqlalchemy import select

        from podium.models import Event
        from podium.services import pairwise

        event = db.execute(
            select(Event).where(Event.slug == summary.report.event_slug)
        ).scalar_one()
        derived = pairwise.derive_from_scores(db, event)
        if derived:
            summary.report.counts["derived comparisons"] = derived
            db.commit()
    return summary
