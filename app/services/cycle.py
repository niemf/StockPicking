"""市场情绪周期（择时层）。

技术指标只回答「这一笔怎么买」，情绪周期回答「今天该不该做、做多重」。
公开的短线体系（炒股养家四段式、淘股吧冰点判据）用的都是同一组观测量：

    涨停家数 / 跌停家数 / 炸板率 / 连板高度 / 昨日涨停今日溢价 / 上涨家数占比

其中「昨日涨停今日溢价」最重要——它衡量昨天打板的人今天赚不赚钱，
比涨停家数更早反映拐点。本模块只做确定性计算，不调用 AI。

阶段与动作（阈值见 ``app/analysis/cycle_profile.yaml`` 或默认常量）：

    冰点 → 试仓（低仓位，只做首板/超跌反包）
    修复 → 正常做（趋势回踩、突破回踩）
    发酵 → 积极做（主流题材、二波）
    高潮 → 不追高（只有首阴低吸，RR 要求提高）
    退潮 → 空仓为主（RR 要求最高）
"""

from __future__ import annotations

import statistics
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

from loguru import logger
from sqlalchemy.orm import Session

from app.providers.base import Quote
from app.services.limit_board import get_limit_pool
from app.services.quotes import QuoteService
from app.services.sentiment import compute as compute_sentiment
from app.services.universe import open_dates

CACHE_TTL_SECONDS = 180

PHASES = ("ice", "repair", "ferment", "climax", "ebb")

PHASE_META = {
    "ice": {
        "label": "冰点",
        "position_cap": 40,
        "rr_floor": 2.0,
        "advice": "亏钱效应极致：只做首板/超跌反包，单笔风险减半，禁止接力。",
    },
    "repair": {
        "label": "修复",
        "position_cap": 60,
        "rr_floor": 2.0,
        "advice": "分歧轮动：做趋势回踩、平台突破回踩，不追一致性。",
    },
    "ferment": {
        "label": "发酵",
        "position_cap": 80,
        "rr_floor": 2.0,
        "advice": "主升段：主流题材可正常参与，让利润奔跑。",
    },
    "climax": {
        "label": "高潮",
        "position_cap": 40,
        "rr_floor": 3.0,
        "advice": "一致性过强：只做首阴低吸，不追高、不加仓，随时准备走。",
    },
    "ebb": {
        "label": "退潮",
        "position_cap": 20,
        "rr_floor": 3.5,
        "advice": "亏钱效应扩散：空仓为主，最多保留观察仓，禁止新开重仓。",
    },
}

DEFAULT_THRESHOLDS = {
    "ice_limit_up": 25,
    "ebb_premium": -2.5,
    "ebb_break_rate": 0.50,
    "ebb_limit_up_drop": 0.5,
    "climax_limit_up": 90,
    "climax_streak": 6,
    "climax_premium": 2.0,
    "ferment_limit_up": 50,
    "ferment_premium": 1.0,
    "ferment_break_rate": 0.35,
}

_cache_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "date": None, "data": None}


# ------------------------------------------------------------------ result
@dataclass(frozen=True)
class CycleState:
    phase: str
    label: str
    temperature: float
    position_cap: int
    rr_floor: float
    advice: str
    metrics: dict = field(default_factory=dict)
    trade_date: str | None = None
    available: bool = True
    reason: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------ metrics
def build_metrics(
    quotes: dict[str, Quote],
    *,
    prev_limit_codes: set[str] | None = None,
    max_streak: int | None = None,
    limit_up_prev: int | None = None,
    limit_down_prev: int | None = None,
) -> dict:
    """纯计算：把一个全市场快照折成情绪周期需要的六个数字。"""
    base = compute_sentiment(quotes)
    if not base.get("available"):
        return {"available": False, "reason": base.get("reason", "no_quotes")}

    valid = [quote for quote in quotes.values() if quote.price > 0 and quote.pre_close > 0]
    touched = [
        quote
        for quote in valid
        if quote.limit_up > 0 and quote.high > 0 and quote.high >= quote.limit_up * 0.999
    ]
    sealed = [
        quote
        for quote in touched
        if quote.price >= quote.limit_up * 0.999
    ]
    break_rate = (1 - len(sealed) / len(touched)) if touched else None

    prev_premium = None
    if prev_limit_codes:
        changes = [
            quote.pct_chg
            for code, quote in quotes.items()
            if code in prev_limit_codes and quote.price > 0 and quote.pre_close > 0
        ]
        if changes:
            prev_premium = round(statistics.fmean(changes), 2)

    return {
        "available": True,
        "total": base["total"],
        "limit_up": base["limit_up"],
        "limit_down": base["limit_down"],
        "limit_up_prev": limit_up_prev,
        "limit_down_prev": limit_down_prev,
        "touched": len(touched),
        "break_rate": round(break_rate, 3) if break_rate is not None else None,
        "max_streak": max_streak,
        "prev_premium": prev_premium,
        "prev_limit_count": len(prev_limit_codes or ()),
        "breadth": base["breadth"],
        "median_pct": base["median_pct"],
        "amount_yi": base["amount_yi"],
        "label": base["label"],
    }


def classify(metrics: dict, thresholds: dict | None = None) -> str:
    """把指标映射到五个阶段之一（确定性，按优先级短路）。"""
    cfg = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    limit_up = float(metrics.get("limit_up") or 0)
    premium = metrics.get("prev_premium")
    break_rate = metrics.get("break_rate")
    streak = float(metrics.get("max_streak") or 0)
    limit_up_prev = metrics.get("limit_up_prev")

    # 退潮优先：昨日打板资金已经明显亏钱，任何「看起来强」的票都不该追。
    if premium is not None and premium <= float(cfg["ebb_premium"]):
        return "ebb"
    if (
        premium is not None
        and premium <= -1.0
        and break_rate is not None
        and break_rate >= float(cfg["ebb_break_rate"])
    ):
        return "ebb"
    if (
        limit_up_prev
        and limit_up < float(limit_up_prev) * float(cfg["ebb_limit_up_drop"])
    ):
        return "ebb"

    if limit_up <= float(cfg["ice_limit_up"]) and (premium is None or premium <= 0):
        return "ice"

    if (limit_up >= float(cfg["climax_limit_up"]) or streak >= float(cfg["climax_streak"])) and (
        premium is None or premium >= float(cfg["climax_premium"])
    ):
        return "climax"

    if (
        limit_up >= float(cfg["ferment_limit_up"])
        and (premium is None or premium >= float(cfg["ferment_premium"]))
        and (break_rate is None or break_rate < float(cfg["ferment_break_rate"]))
    ):
        return "ferment"

    return "repair"


def temperature(metrics: dict) -> float:
    """0–100 的情绪温度，只用于展示与排序，不参与判定。"""
    limit_up = float(metrics.get("limit_up") or 0)
    premium = metrics.get("prev_premium")
    break_rate = metrics.get("break_rate")
    streak = float(metrics.get("max_streak") or 0)

    score = min(40.0, limit_up / 2.5)
    if premium is not None:
        score += max(0.0, min(30.0, (premium + 3) * 6))
    if break_rate is not None:
        score += max(0.0, min(15.0, (0.6 - break_rate) * 25))
    score += min(15.0, streak * 2.5)
    return round(max(0.0, min(100.0, score)), 1)


def state_from_metrics(
    metrics: dict,
    *,
    trade_date: date | None = None,
    thresholds: dict | None = None,
) -> CycleState:
    if not metrics.get("available"):
        return CycleState(
            phase="repair",
            label=PHASE_META["repair"]["label"],
            temperature=0.0,
            position_cap=PHASE_META["repair"]["position_cap"],
            rr_floor=PHASE_META["repair"]["rr_floor"],
            advice="行情数据不可用，按中性阶段处理，仓位从严。",
            metrics=metrics,
            trade_date=trade_date.isoformat() if trade_date else None,
            available=False,
            reason=metrics.get("reason"),
        )

    phase = classify(metrics, thresholds)
    meta = PHASE_META[phase]
    return CycleState(
        phase=phase,
        label=meta["label"],
        temperature=temperature(metrics),
        position_cap=meta["position_cap"],
        rr_floor=meta["rr_floor"],
        advice=meta["advice"],
        metrics=metrics,
        trade_date=trade_date.isoformat() if trade_date else None,
    )


# ------------------------------------------------------------------ IO
def _previous_open_day(session: Session, trade_date: date) -> date | None:
    dates = [day for day in open_dates(session) if day < trade_date]
    return max(dates) if dates else None


def _pool_summary(day: date | None) -> dict:
    if day is None:
        # No calendar (fresh DB / tests): skip the network call entirely.
        return {}
    try:
        pool = get_limit_pool(day)
    except Exception as exc:  # noqa: BLE001 - 情绪周期可降级
        logger.warning("limit pool unavailable for cycle {}: {}", day, exc)
        return {}
    if not pool.get("available"):
        return {}
    return pool


def compute_cycle(
    session: Session,
    *,
    trade_date: date | None = None,
    quotes: dict[str, Quote] | None = None,
    quote_service: QuoteService | None = None,
    refresh: bool = False,
    thresholds: dict | None = None,
) -> CycleState:
    """计算当日情绪周期；结果进程内缓存 3 分钟。"""
    trade_date = trade_date or datetime.now().date()
    now = time.monotonic()
    if not refresh:
        with _cache_lock:
            cached = _cache["data"]
            if (
                cached is not None
                and _cache["date"] == trade_date
                and now - float(_cache["ts"]) < CACHE_TTL_SECONDS
            ):
                return cached

    if quotes is None:
        from sqlalchemy import select

        from app.db.models import Stock

        codes = list(session.scalars(select(Stock.code).where(Stock.status == "normal")))
        try:
            quotes = (quote_service or QuoteService()).get_quotes(codes)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cycle quotes unavailable: {}", exc)
            return state_from_metrics({"available": False, "reason": str(exc)[:120]}, trade_date=trade_date)

    prev_day = _previous_open_day(session, trade_date)
    prev_pool = _pool_summary(prev_day)
    today_pool = _pool_summary(trade_date) if prev_day is not None else {}

    prev_codes = {
        row["code"] for row in prev_pool.get("stocks") or [] if row["code"] in quotes
    }
    metrics = build_metrics(
        quotes,
        prev_limit_codes=prev_codes,
        max_streak=today_pool.get("max_streak"),
        # Compare like with like: yesterday's pool is all boards, our snapshot is
        # main-board only, so count yesterday's boards that are in the universe.
        limit_up_prev=len(prev_codes) or prev_pool.get("total"),
    )
    state = state_from_metrics(metrics, trade_date=trade_date, thresholds=thresholds)

    with _cache_lock:
        _cache.update({"ts": now, "date": trade_date, "data": state})
    return state


def describe(state: CycleState) -> str:
    """一行摘要，给卡片和日志用。"""
    metrics = state.metrics or {}
    parts = [
        f"涨停 {_count(metrics.get('limit_up'))}",
        f"跌停 {_count(metrics.get('limit_down'))}",
        f"炸板率 {_fraction_pct(metrics.get('break_rate'))}",
        f"最高 {_count(metrics.get('max_streak'))} 板",
        f"昨涨停溢价 {_points(metrics.get('prev_premium'))}",
    ]
    return " · ".join(parts)


def _count(value) -> str:
    return "—" if value is None else f"{int(value)}"


def _fraction_pct(value) -> str:
    """0.35 -> 35%"""
    return "—" if value is None else f"{float(value) * 100:.0f}%"


def _points(value) -> str:
    """2.5 -> +2.5%（已经是百分点）"""
    return "—" if value is None else f"{float(value):+.1f}%"


def reset_cache() -> None:
    with _cache_lock:
        _cache.update({"ts": 0.0, "date": None, "data": None})


__all__ = [
    "CycleState",
    "DEFAULT_THRESHOLDS",
    "PHASE_META",
    "build_metrics",
    "classify",
    "compute_cycle",
    "describe",
    "reset_cache",
    "state_from_metrics",
    "temperature",
]
