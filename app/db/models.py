"""SQLAlchemy models for the TradeAI data model.

Field layout follows ``价格预警.md`` section 8. Timestamps are stored as naive
local Beijing time: every consumer (UI, scheduler, notifications) is single
timezone, so UTC conversion would only add mistakes.

Design notes
------------
* ``stock.code`` is a plain indexed string, not a foreign key. The stock master
  list refresh must never block a user from adding a watchlist item, and
  AkShare occasionally misses newly listed or renamed codes.
* Internal relations (holding → transactions, alert rule → event → notification)
  do use foreign keys with ``ON DELETE CASCADE`` where the child cannot exist
  alone.
* ``status`` columns use short strings with documented values instead of native
  enums: SQLite cannot alter an enum, and adding a state should not need a
  migration.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _now() -> datetime:
    return datetime.now()


class Stock(Base):
    """股票基础信息（AkShare 股票列表盘后同步）。"""

    __tablename__ = "stock"

    code: Mapped[str] = mapped_column(String(6), primary_key=True)
    name: Mapped[str] = mapped_column(String(32), index=True)
    exchange: Mapped[str] = mapped_column(String(4))  # sh / sz
    board: Mapped[str] = mapped_column(String(16))  # 沪市主板 / 深市主板
    industry: Mapped[str | None] = mapped_column(String(32), default=None)
    list_date: Mapped[date | None] = mapped_column(Date, default=None)
    is_st: Mapped[bool] = mapped_column(Boolean, default=False)
    is_delisting_risk: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="normal")  # normal/suspended/delisted
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class DailyBar(Base):
    """前复权日线，主键 (code, trade_date)。"""

    __tablename__ = "daily_bar"

    code: Mapped[str] = mapped_column(String(6), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    pre_close: Mapped[float | None] = mapped_column(Float, default=None)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    turnover_rate: Mapped[float | None] = mapped_column(Float, default=None)
    adj_factor: Mapped[float] = mapped_column(Float, default=1.0)

    __table_args__ = (Index("ix_daily_bar_code_date", "code", "trade_date"),)


class Quote(Base):
    """实时快照；只保留最近 30 天，避免数据库膨胀。"""

    __tablename__ = "quote"

    code: Mapped[str] = mapped_column(String(6), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, primary_key=True)
    price: Mapped[float] = mapped_column(Float)
    pre_close: Mapped[float] = mapped_column(Float, default=0.0)
    open: Mapped[float] = mapped_column(Float, default=0.0)
    high: Mapped[float] = mapped_column(Float, default=0.0)
    low: Mapped[float] = mapped_column(Float, default=0.0)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    pct_chg: Mapped[float] = mapped_column(Float, default=0.0)
    turnover_rate: Mapped[float | None] = mapped_column(Float, default=None)
    limit_up: Mapped[float | None] = mapped_column(Float, default=None)
    limit_down: Mapped[float | None] = mapped_column(Float, default=None)
    is_suspended: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(16), default="tencent")


class IndicatorDaily(Base):
    """每日技术指标快照，供预警与候选池复用。

    Written once after the close by ``app/services/indicators.py``; discovery
    and the stock page read it instead of recomputing 200 bars per stock.
    """

    __tablename__ = "indicator_daily"

    code: Mapped[str] = mapped_column(String(6), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    close: Mapped[float | None] = mapped_column(Float, default=None)
    ma5: Mapped[float | None] = mapped_column(Float, default=None)
    ma10: Mapped[float | None] = mapped_column(Float, default=None)
    ma20: Mapped[float | None] = mapped_column(Float, default=None)
    ma30: Mapped[float | None] = mapped_column(Float, default=None)
    ma60: Mapped[float | None] = mapped_column(Float, default=None)
    ma20_slope3: Mapped[float | None] = mapped_column(Float, default=None)
    vol_ma5: Mapped[float | None] = mapped_column(Float, default=None)
    vol_ma20: Mapped[float | None] = mapped_column(Float, default=None)
    volume_ratio: Mapped[float | None] = mapped_column(Float, default=None)
    macd_dif: Mapped[float | None] = mapped_column(Float, default=None)
    macd_dea: Mapped[float | None] = mapped_column(Float, default=None)
    macd_hist: Mapped[float | None] = mapped_column(Float, default=None)
    rsi6: Mapped[float | None] = mapped_column(Float, default=None)
    rsi12: Mapped[float | None] = mapped_column(Float, default=None)
    rsi24: Mapped[float | None] = mapped_column(Float, default=None)
    boll_upper: Mapped[float | None] = mapped_column(Float, default=None)
    boll_mid: Mapped[float | None] = mapped_column(Float, default=None)
    boll_lower: Mapped[float | None] = mapped_column(Float, default=None)
    atr14: Mapped[float | None] = mapped_column(Float, default=None)
    atr_pct: Mapped[float | None] = mapped_column(Float, default=None)
    high20: Mapped[float | None] = mapped_column(Float, default=None)
    high20_prev: Mapped[float | None] = mapped_column(Float, default=None)
    low20: Mapped[float | None] = mapped_column(Float, default=None)
    high60_prev: Mapped[float | None] = mapped_column(Float, default=None)
    resistance_dist: Mapped[float | None] = mapped_column(Float, default=None)
    range20_pct: Mapped[float | None] = mapped_column(Float, default=None)
    support: Mapped[float | None] = mapped_column(Float, default=None)
    resistance: Mapped[float | None] = mapped_column(Float, default=None)
    drawdown20: Mapped[float | None] = mapped_column(Float, default=None)
    close_position: Mapped[float | None] = mapped_column(Float, default=None)
    upper_shadow_pct: Mapped[float | None] = mapped_column(Float, default=None)
    lower_shadow_pct: Mapped[float | None] = mapped_column(Float, default=None)
    ret5: Mapped[float | None] = mapped_column(Float, default=None)
    ret20: Mapped[float | None] = mapped_column(Float, default=None)
    up_streak: Mapped[float | None] = mapped_column(Float, default=None)
    score: Mapped[float | None] = mapped_column(Float, default=None)
    tags: Mapped[list[str] | None] = mapped_column(JSON, default=None)

    __table_args__ = (Index("ix_indicator_daily_date", "trade_date"),)


class WeeklyBar(Base):
    """周线（按自然周聚合的日线）与周线均线。

    The weekly chart is what separates "a strong month" from "a strong week";
    discovery only needs the latest weeks, but the table keeps a short history
    so the trend can be read without touching 5 years of daily bars.
    """

    __tablename__ = "weekly_bar"

    code: Mapped[str] = mapped_column(String(6), primary_key=True)
    week_end: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[float | None] = mapped_column(Float, default=None)
    high: Mapped[float | None] = mapped_column(Float, default=None)
    low: Mapped[float | None] = mapped_column(Float, default=None)
    close: Mapped[float | None] = mapped_column(Float, default=None)
    volume: Mapped[float | None] = mapped_column(Float, default=None)
    amount: Mapped[float | None] = mapped_column(Float, default=None)
    pct_chg: Mapped[float | None] = mapped_column(Float, default=None)
    ma5: Mapped[float | None] = mapped_column(Float, default=None)
    ma10: Mapped[float | None] = mapped_column(Float, default=None)
    ma20: Mapped[float | None] = mapped_column(Float, default=None)
    ma30: Mapped[float | None] = mapped_column(Float, default=None)
    weeks: Mapped[int | None] = mapped_column(Integer, default=None)

    __table_args__ = (Index("ix_weekly_bar_week", "week_end"),)


class Candidate(Base):
    """候选池：规则初筛结果，等待 AI 分析或人工确认。"""

    __tablename__ = "candidate"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    code: Mapped[str] = mapped_column(String(6), index=True)
    name: Mapped[str] = mapped_column(String(32))
    strategy: Mapped[str] = mapped_column(String(16), default="indicator", index=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    rank: Mapped[int] = mapped_column(Integer, default=0)
    rule_hits: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    indicator_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/ai_analyzed/confirmed/rejected
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (UniqueConstraint("trade_date", "code", name="uq_candidate_date_code"),)


class Watchlist(Base):
    """自选股；一个代码只保留一条活跃记录，归档用 status 标记。"""

    __tablename__ = "watchlist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(6), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(16), default="manual")  # ai/rule/manual
    plan_id: Mapped[int | None] = mapped_column(
        ForeignKey("trade_plan.id", ondelete="SET NULL"), default=None
    )
    added_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/archived
    note: Mapped[str | None] = mapped_column(Text, default=None)
    group_name: Mapped[str] = mapped_column(String(32), default="默认")
    tags: Mapped[list[str] | None] = mapped_column(JSON, default=None)

    plan: Mapped["TradePlan | None"] = relationship(foreign_keys=[plan_id], lazy="joined")


class Holding(Base):
    """持仓；一个代码一条记录，买卖通过 holding_transaction 记录。"""

    __tablename__ = "holding"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(6), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(32))
    cost_price: Mapped[float] = mapped_column(Float, default=0.0)
    shares: Mapped[int] = mapped_column(Integer, default=0)
    available_shares: Mapped[int] = mapped_column(Integer, default=0)
    open_date: Mapped[date | None] = mapped_column(Date, default=None)
    plan_id: Mapped[int | None] = mapped_column(
        ForeignKey("trade_plan.id", ondelete="SET NULL"), default=None
    )
    current_stop: Mapped[float | None] = mapped_column(Float, default=None)
    trailing_high: Mapped[float | None] = mapped_column(Float, default=None)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open/closed
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    realized_pnl: Mapped[float | None] = mapped_column(Float, default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)

    transactions: Mapped[list["HoldingTransaction"]] = relationship(
        back_populates="holding",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="HoldingTransaction.trade_date",
    )


class HoldingTransaction(Base):
    """加仓 / 减仓 / 清仓流水。"""

    __tablename__ = "holding_transaction"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    holding_id: Mapped[int] = mapped_column(
        ForeignKey("holding.id", ondelete="CASCADE"), index=True
    )
    trade_date: Mapped[date] = mapped_column(Date)
    action: Mapped[str] = mapped_column(String(8))  # buy/sell
    price: Mapped[float] = mapped_column(Float)
    shares: Mapped[int] = mapped_column(Integer)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    holding: Mapped[Holding] = relationship(back_populates="transactions")


class TradePlan(Base):
    """交易计划：AI 或人工产出，确认后才生效。"""

    __tablename__ = "trade_plan"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_no: Mapped[str | None] = mapped_column(String(32), unique=True, default=None)
    code: Mapped[str] = mapped_column(String(6), index=True)
    name: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(16), default="manual")  # ai/manual
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    thesis: Mapped[str | None] = mapped_column(Text, default=None)
    fundamental_view: Mapped[str | None] = mapped_column(Text, default=None)
    technical_view: Mapped[str | None] = mapped_column(Text, default=None)
    catalysts: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    risks: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    buy_zone_low: Mapped[float | None] = mapped_column(Float, default=None)
    buy_zone_high: Mapped[float | None] = mapped_column(Float, default=None)
    stop_loss: Mapped[float | None] = mapped_column(Float, default=None)
    take_profit_1: Mapped[float | None] = mapped_column(Float, default=None)
    take_profit_2: Mapped[float | None] = mapped_column(Float, default=None)
    position_pct: Mapped[float | None] = mapped_column(Float, default=None)
    confidence: Mapped[float | None] = mapped_column(Float, default=None)
    rating: Mapped[str | None] = mapped_column(String(8), default=None)  # 看多/中性/观察
    risk_score: Mapped[float | None] = mapped_column(Float, default=None)
    tags: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    holding_horizon: Mapped[str | None] = mapped_column(String(32), default=None)
    invalid_conditions: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    market_regime: Mapped[str | None] = mapped_column(String(16), default=None)
    model: Mapped[str | None] = mapped_column(String(64), default=None)
    prompt_version: Mapped[str | None] = mapped_column(String(16), default=None)
    raw_response: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft/confirmed/rejected/expired
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    valid_until: Mapped[date | None] = mapped_column(Date, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (Index("ix_trade_plan_code_status", "code", "status"),)


class AlertRule(Base):
    """预警规则，状态机参数随规则保存。"""

    __tablename__ = "alert_rule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_type: Mapped[str] = mapped_column(String(8))  # watch/holding
    target_id: Mapped[int] = mapped_column(Integer)
    rule_type: Mapped[str] = mapped_column(String(32))  # stop_loss/take_profit_1/ma_break/volume_spike...
    params: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    confirm_samples: Mapped[int] = mapped_column(Integer, default=2)
    cooldown_sec: Mapped[int] = mapped_column(Integer, default=1800)
    rearm_threshold: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (
        Index("ix_alert_rule_target", "target_type", "target_id"),
        UniqueConstraint("target_type", "target_id", "rule_type", name="uq_alert_rule_target_type"),
    )


class AlertEvent(Base):
    """预警事件；dedup_key 保证同一信号不重复推送。"""

    __tablename__ = "alert_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("alert_rule.id", ondelete="SET NULL"), default=None
    )
    code: Mapped[str] = mapped_column(String(6), index=True)
    trigger_ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    trigger_price: Mapped[float | None] = mapped_column(Float, default=None)
    condition_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/sent/failed/suppressed
    dedup_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class NotificationLog(Base):
    """每条通知的发送结果，便于排查与重试。"""

    __tablename__ = "notification_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("alert_event.id", ondelete="CASCADE"), default=None, index=True
    )
    channel: Mapped[str] = mapped_column(String(16))  # feishu/email
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    response: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/success/failed
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class AICallLog(Base):
    """DeepSeek 调用记录，用于成本控制与复盘。"""

    __tablename__ = "ai_call_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str | None] = mapped_column(String(6), default=None, index=True)
    purpose: Mapped[str] = mapped_column(String(32))  # plan/review/screen
    model: Mapped[str | None] = mapped_column(String(64), default=None)
    prompt_version: Mapped[str | None] = mapped_column(String(16), default=None)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="success")  # success/failed
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ReviewReport(Base):
    """每日复盘报告，每个交易日一份。"""

    __tablename__ = "review_report"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, unique=True, index=True)
    market_summary: Mapped[str | None] = mapped_column(Text, default=None)
    watch_summary: Mapped[str | None] = mapped_column(Text, default=None)
    holding_summary: Mapped[str | None] = mapped_column(Text, default=None)
    candidates_summary: Mapped[str | None] = mapped_column(Text, default=None)
    ai_narrative: Mapped[str | None] = mapped_column(Text, default=None)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class StockReport(Base):
    """AI 个股报告卡：把交易计划之外的研究结论单独存一份。

    同一只股票、同一交易日、同一 prompt 版本只保留一张，重复生成会覆盖。
    结论/评分/关键价位由模型给出，但经过系统校验与夹取，页面同时展示原始摘要。
    """

    __tablename__ = "stock_report"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(6), index=True)
    name: Mapped[str] = mapped_column(String(32))
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    conclusion: Mapped[str] = mapped_column(String(8))  # 看多/中性/回避
    score: Mapped[int] = mapped_column(Integer, default=0)  # 0-100
    trend: Mapped[str] = mapped_column(String(8))  # 上升/震荡/下降
    trend_note: Mapped[str | None] = mapped_column(String(160), default=None)
    support: Mapped[float | None] = mapped_column(Float, default=None)
    resistance: Mapped[float | None] = mapped_column(Float, default=None)
    invalidation: Mapped[float | None] = mapped_column(Float, default=None)
    summary: Mapped[str | None] = mapped_column(Text, default=None)
    risks: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    catalysts: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    checklist: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    model: Mapped[str | None] = mapped_column(String(64), default=None)
    prompt_version: Mapped[str | None] = mapped_column(String(16), default=None)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (
        UniqueConstraint("trade_date", "code", "prompt_version", name="uq_report_date_code_ver"),
    )


class MarketRegime(Base):
    """市场风格判定，每个交易日一条。"""

    __tablename__ = "market_regime"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    regime: Mapped[str] = mapped_column(String(16))  # offense/neutral/defense
    score: Mapped[float] = mapped_column(Float, default=0.0)
    indicators: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class MarketDaily(Base):
    """每日市场级快照：指数、成交额、涨跌停、炸板率、情绪阶段。

    ``source`` 区分两种来源：``live`` 是收盘后用全市场快照算的精确值，
    ``bars`` 是从日线反推的近似值（历史回填用，没有盘中炸板数据）。
    回测按情绪阶段分组时看这一行，而不是每天重算。
    """

    __tablename__ = "market_daily"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    sh_close: Mapped[float | None] = mapped_column(Float, default=None)
    sh_pct: Mapped[float | None] = mapped_column(Float, default=None)
    sz_close: Mapped[float | None] = mapped_column(Float, default=None)
    sz_pct: Mapped[float | None] = mapped_column(Float, default=None)
    cyb_close: Mapped[float | None] = mapped_column(Float, default=None)
    cyb_pct: Mapped[float | None] = mapped_column(Float, default=None)
    zz1000_close: Mapped[float | None] = mapped_column(Float, default=None)
    zz1000_pct: Mapped[float | None] = mapped_column(Float, default=None)
    amount_yi: Mapped[float | None] = mapped_column(Float, default=None)
    total: Mapped[int | None] = mapped_column(Integer, default=None)
    up: Mapped[int | None] = mapped_column(Integer, default=None)
    down: Mapped[int | None] = mapped_column(Integer, default=None)
    flat: Mapped[int | None] = mapped_column(Integer, default=None)
    limit_up: Mapped[int | None] = mapped_column(Integer, default=None)
    limit_down: Mapped[int | None] = mapped_column(Integer, default=None)
    break_rate: Mapped[float | None] = mapped_column(Float, default=None)
    max_streak: Mapped[int | None] = mapped_column(Integer, default=None)
    prev_premium: Mapped[float | None] = mapped_column(Float, default=None)
    breadth: Mapped[float | None] = mapped_column(Float, default=None)
    median_pct: Mapped[float | None] = mapped_column(Float, default=None)
    phase: Mapped[str | None] = mapped_column(String(16), default=None)
    temperature: Mapped[float | None] = mapped_column(Float, default=None)
    source: Mapped[str] = mapped_column(String(8), default="live")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class SectorFlowSample(Base):
    """板块资金分时采样：每 5 分钟一次，用于画「流入前5/流出前5」累计曲线。"""

    __tablename__ = "sector_flow_sample"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    sector: Mapped[str] = mapped_column(String(32))
    net_inflow: Mapped[float | None] = mapped_column(Float, default=None)  # 亿元
    pct_chg: Mapped[float | None] = mapped_column(Float, default=None)

    __table_args__ = (UniqueConstraint("ts", "sector", name="uq_sector_sample_ts"),)


class SignalLog(Base):
    """候选/预警的结果跟踪：T+1 / T+3 / T+5、最大浮盈浮亏、止损目标命中。

    这是回答「推荐的票到底值不值得买」的唯一客观依据；回测是事前的，
    这里是事后的，两者互补。
    """

    __tablename__ = "signal_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    code: Mapped[str] = mapped_column(String(6), index=True)
    name: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(16), default="candidate")
    phase: Mapped[str | None] = mapped_column(String(16), default=None)
    score: Mapped[float | None] = mapped_column(Float, default=None)
    rr: Mapped[float | None] = mapped_column(Float, default=None)
    entry: Mapped[float | None] = mapped_column(Float, default=None)
    stop: Mapped[float | None] = mapped_column(Float, default=None)
    target1: Mapped[float | None] = mapped_column(Float, default=None)
    risk_pct: Mapped[float | None] = mapped_column(Float, default=None)
    status: Mapped[str] = mapped_column(String(16), default="open")
    t1_ret: Mapped[float | None] = mapped_column(Float, default=None)
    t3_ret: Mapped[float | None] = mapped_column(Float, default=None)
    t5_ret: Mapped[float | None] = mapped_column(Float, default=None)
    mfe: Mapped[float | None] = mapped_column(Float, default=None)
    mae: Mapped[float | None] = mapped_column(Float, default=None)
    r_multiple: Mapped[float | None] = mapped_column(Float, default=None)
    exit_date: Mapped[date | None] = mapped_column(Date, default=None)
    exit_price: Mapped[float | None] = mapped_column(Float, default=None)
    note: Mapped[str | None] = mapped_column(String(64), default=None)
    tracked_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    __table_args__ = (
        UniqueConstraint("trade_date", "code", "source", name="uq_signal_code_day"),
        Index("ix_signal_status", "status"),
    )


class FundamentalReview(Base):
    """AI 基本面/题材闸门：只对自选股跑，做排除法（veto），不参与打分。

    同一只股票同一天只保留一条；加入自选时触发，也可手动刷新。
    """

    __tablename__ = "fundamental_review"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(6), index=True)
    name: Mapped[str] = mapped_column(String(32), default="")
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    verdict: Mapped[str] = mapped_column(String(8), default="watch")  # pass/veto/watch
    score: Mapped[int] = mapped_column(Integer, default=0)  # 0-100
    tags: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    reasons: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    risks: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    model: Mapped[str] = mapped_column(String(64), default="")
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (UniqueConstraint("code", "trade_date", name="uq_fundamental_code_day"),)


class TradeCalendar(Base):
    """交易日历缓存；盘前同步一次，盘中判断只查本地。"""

    __tablename__ = "trade_calendar"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(32), default=None)  # holiday / weekend
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class JobRun(Base):
    """定时任务执行记录，用于单实例锁与故障排查。"""

    __tablename__ = "job_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_name: Mapped[str] = mapped_column(String(64))
    trade_date: Mapped[date] = mapped_column(Date)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/running/success/failed/skipped
    error: Mapped[str | None] = mapped_column(Text, default=None)
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    __table_args__ = (
        UniqueConstraint("job_name", "trade_date", "scheduled_at", name="uq_job_run_slot"),
    )


__all__ = [
    "AICallLog",
    "AlertEvent",
    "AlertRule",
    "Base",
    "Candidate",
    "DailyBar",
    "FundamentalReview",
    "Holding",
    "HoldingTransaction",
    "IndicatorDaily",
    "JobRun",
    "MarketRegime",
    "MarketDaily",
    "NotificationLog",
    "Quote",
    "ReviewReport",
    "SectorFlowSample",
    "SignalLog",
    "Stock",
    "TradePlan",
    "TradeCalendar",
    "Watchlist",
    "WeeklyBar",
]
