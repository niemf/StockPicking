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
