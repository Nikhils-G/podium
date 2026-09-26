"""python -m podium.seed — load fixtures + demo accounts and print the test logins."""

from podium.config import get_settings
from podium.db import get_sessionmaker
from podium.seed import run_seed
from podium.seed.printer import format_summary

if __name__ == "__main__":
    settings = get_settings()
    with get_sessionmaker()() as db:
        summary = run_seed(db, settings)
    print(format_summary(summary, settings.demo_password), flush=True)
