"""Application settings loaded from .env."""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Bybit (v2)
    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    bybit_testnet: bool = False

    # Tradelocker (v1)
    tradelocker_email: str = ""
    tradelocker_password: str = ""
    tradelocker_server: str = "PLEXY"
    tradelocker_account_id: int = 0
    tradelocker_env: str = "demo"

    # Telegram
    telegram_channel: str = "@XAUUSD_FOREX4"
    telegram_api_id: int = 0
    telegram_api_hash: str = ""

    # Runtime
    rade_env: str = "paper"
    rade_log_level: str = "INFO"
    rade_data_dir: str = "./data"

    @property
    def data_dir(self) -> Path:
        return Path(self.rade_data_dir)

    @property
    def tradelocker_base_url(self) -> str:
        host = "demo.tradelocker.com" if self.tradelocker_env == "demo" else "live.tradelocker.com"
        return f"https://{host}"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
