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
    poll_interval_seconds: int = 60
    quote_batch_size: int = 400
    quote_workers: int = 6

    # Daily bars / discovery
    bar_backfill_days: int = 320
    bar_incremental_days: int = 12
    bar_fetch_workers: int = 8
    discovery_top_n: int = 30

    # Feature flags
    # Holdings (cost / P&L) is kept in code but hidden by default: the product
    # is positioned as watch + alert + research, not a portfolio ledger.
    feature_holdings: bool = False
    # AI trade plans cost money and the backtest shows the current candidate
    # signal has negative expectancy, so plan generation is off by default.
    # Turn it on after a signal source passes the backtest acceptance line.
    feature_ai_plans: bool = False

    # Monitoring / alerts
    monitor_enabled: bool = False
    monitor_timezone: str = "Asia/Shanghai"
    alert_confirm_samples: int = 2
    alert_stop_confirm_samples: int = 1
    alert_cooldown_seconds: int = 1800
    alert_rearm_pct: float = 0.5
    alert_trailing_drawdown_pct: float = 8.0
    alert_price_change_pct: float = 5.0

    # Watchlist intraday signals. All deterministic (no AI) and evaluated on the
    # same batched quote as the stop-loss rules; only the watchlist pays for the
    # extra daily-bar read.
    alert_watch_stats_enabled: bool = True
    alert_breakout_enabled: bool = True
    alert_breakout_volume_ratio: float = 2.0
    alert_breakout_tolerance_pct: float = 0.5
    alert_breakout_max_per_day: int = 4
    alert_intraday_enabled: bool = True
    alert_intraday_strength_pct: float = 1.0
    alert_intraday_volume_ratio: float = 1.2
    alert_intraday_max_per_day: int = 4
    alert_buy_signal_enabled: bool = True
    alert_buy_score: int = 6
    alert_buy_rsi_max: float = 70.0
    alert_buy_resistance_min_pct: float = 2.0
    alert_buy_max_per_day: int = 3
    alert_pullback_enabled: bool = True
    alert_pullback_tolerance_pct: float = 1.0
    alert_pullback_volume_max: float = 1.0
    alert_pullback_max_per_day: int = 3

    # AI
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    deepseek_timeout_seconds: float = 60.0
    ai_daily_budget_cny: float = 5.0
    ai_input_price_cny: float = 2.0   # 元 / 百万 tokens（按实际价格调整）
    ai_output_price_cny: float = 8.0
    ai_prompt_version: str = "plan-v1"
    ai_review_prompt_version: str = "review-v1"
    ai_max_plans: int = 10

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
