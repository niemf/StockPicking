"""Application configuration loaded from .env / environment variables."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Application
    app_name: str = "TradeAI"
    app_env: str = "dev"
    debug: bool = True
    host: str = "127.0.0.1"
    port: int = 8000
    timezone: str = "Asia/Shanghai"

    # Database (reserved for the next stage)
    database_url: str = "sqlite:///./data/tradeai.db"

    # Quote providers
    quote_primary: str = "tencent"
    quote_timeout_seconds: float = 8.0
    quote_cache_seconds: int = 10
    poll_interval_seconds: int = 120
    quote_batch_size: int = 400
    quote_workers: int = 6

    # Daily bars / discovery
    bar_backfill_days: int = 320
    bar_incremental_days: int = 12
    bar_fetch_workers: int = 8
    discovery_top_n: int = 30

    # Monitoring / alerts
    monitor_enabled: bool = False
    monitor_timezone: str = "Asia/Shanghai"
    alert_confirm_samples: int = 2
    alert_stop_confirm_samples: int = 1
    alert_cooldown_seconds: int = 1800
    alert_rearm_pct: float = 0.5
    alert_trailing_drawdown_pct: float = 8.0
    alert_price_change_pct: float = 5.0

    # AI
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    ai_daily_budget_cny: float = 5.0

    # Notification
    feishu_webhook: str = ""
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_to: str = ""
    smtp_use_ssl: bool = True

    @property
    def is_dev(self) -> bool:
        return self.app_env.lower() in {"dev", "development", "local"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
