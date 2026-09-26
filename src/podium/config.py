from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from the environment (prefix PODIUM_) or a local .env file."""

    model_config = SettingsConfigDict(env_prefix="PODIUM_", env_file=".env", extra="ignore")

    secret_key: str = "podium-dev-secret-change-me"
    base_url: str = "http://localhost:8080"
    data_dir: Path = Path("./data")
    database_url: str | None = None

    seed_fixtures: bool = True
    fixtures_path: Path = Path("fixtures/fixtures.json")
    demo_accounts: bool = True
    demo_password: str = "demo-pass"

    session_days: int = 14
    rate_limit_enabled: bool = True
    webhook_worker: bool = True
    webhook_interval_seconds: int = 5
    cookie_secure: bool | None = None  # None → derived from base_url scheme

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None

    log_level: str = "info"

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{(self.data_dir / 'podium.db').resolve()}"

    @property
    def secure_cookies(self) -> bool:
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.base_url.startswith("https://")


@lru_cache
def get_settings() -> Settings:
    return Settings()
