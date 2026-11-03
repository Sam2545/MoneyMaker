from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    kalshi_env: Literal["prod", "demo"] = "prod"
    kalshi_key_id: str | None = None
    kalshi_private_key_path: Path | None = None

    polymarket_us_key_id: str | None = None
    polymarket_us_secret_key: SecretStr | None = None

    http_timeout_s: float = 10.0
    ws_ping_interval_s: float = 10.0
    reconnect_max_backoff_s: float = 30.0

    @property
    def kalshi_rest_url(self) -> str:
        if self.kalshi_env == "demo":
            return "https://external-api.demo.kalshi.co/trade-api/v2"
        return "https://external-api.kalshi.com/trade-api/v2"

    @property
    def kalshi_ws_url(self) -> str:
        if self.kalshi_env == "demo":
            return "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
        return "wss://external-api-ws.kalshi.com/trade-api/ws/v2"


@lru_cache
def get_settings() -> Settings:
    return Settings()
