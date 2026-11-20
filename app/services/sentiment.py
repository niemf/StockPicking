"""Market breadth and sentiment from the full-market quote snapshot.

The discovery pipeline already fetches every main-board quote in one batched
call. This module reuses that snapshot to derive the numbers retail traders
look at first: advance/decline, limit-up/limit-down, turnover and median move.

Results are cached in-process for a few minutes so the dashboard does not pay
the full-market request on every page view.
"""

from __future__ import annotations

import statistics
import threading
import time
from datetime import datetime

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Stock
from app.providers.base import Quote
from app.services.quotes import QuoteService

CACHE_TTL_SECONDS = 180

_cache_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "date": None, "data": None}

BELOW_LIMIT = 1e-6


def _is_limit_up(quote: Quote) -> bool:
    if quote.limit_up > 0:
        return quote.price >= quote.limit_up - BELOW_LIMIT
    return quote.pct_chg >= 9.8


def _is_limit_down(quote: Quote) -> bool:
    if quote.limit_down > 0:
        return quote.price <= quote.limit_down + BELOW_LIMIT
    return quote.pct_chg <= -9.8


def _label(breadth: float, median: float) -> str:
    if breadth >= 70:
        return "普涨"
    if breadth >= 55:
        return "偏强"
    if breadth >= 45:
        return "分化"
    if breadth >= 30:
        return "偏弱"
    return "普跌"


def compute(quotes: dict[str, Quote]) -> dict:
    """Aggregate a quote snapshot into breadth statistics."""
    valid = [q for q in quotes.values() if q.price > 0 and q.pre_close > 0]
    if not valid:
        return {
            "available": False,
            "total": 0,
            "reason": "no_quotes",
            "ts": datetime.now().isoformat(timespec="seconds"),
        }

    up = sum(1 for q in valid if q.pct_chg > 0)
    down = sum(1 for q in valid if q.pct_chg < 0)
    flat = len(valid) - up - down
    limit_up = sum(1 for q in valid if _is_limit_up(q))
    limit_down = sum(1 for q in valid if _is_limit_down(q))
    strong = sum(1 for q in valid if q.pct_chg >= 5)
    weak = sum(1 for q in valid if q.pct_chg <= -5)
    # 炸板率：盘中触及涨停但收盘没封住的比例
    touched = [q for q in valid if q.limit_up > 0 and q.high >= q.limit_up * 0.999]
    sealed = [q for q in touched if q.price >= q.limit_up * 0.999]
    break_rate = (1 - len(sealed) / len(touched)) if touched else None
    amount = sum(q.amount for q in valid)
    changes = [q.pct_chg for q in valid]
    median = statistics.median(changes)
    average = statistics.fmean(changes)
    breadth = up / len(valid) * 100

    return {
        "available": True,
        "total": len(valid),
        "up": up,
        "down": down,
        "flat": flat,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "touched": len(touched),
        "sealed": len(sealed),
        "break_rate": round(break_rate, 3) if break_rate is not None else None,
        "strong": strong,
        "weak": weak,
        "breadth": round(breadth, 1),
        "median_pct": round(median, 2),
        "avg_pct": round(average, 2),
        "amount": round(amount, 2),
        "amount_yi": round(amount / 1e8, 1),
        "label": _label(breadth, median),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "source": "snapshot",
    }


def _market_codes(session: Session) -> list[str]:
    return list(session.scalars(select(Stock.code).where(Stock.status == "normal")))


def market_sentiment(
    session: Session,
    *,
    refresh: bool = False,
    quote_service: QuoteService | None = None,
) -> dict:
    """Return cached breadth stats, fetching the full-market snapshot if stale."""
    today = datetime.now().date()
    now = time.monotonic()
    with _cache_lock:
        cached = _cache["data"]
        if (
            not refresh
            and cached is not None
            and _cache["date"] == today
            and now - float(_cache["ts"]) < CACHE_TTL_SECONDS
        ):
            return cached

    codes = _market_codes(session)
    if not codes:
        return {
            "available": False,
            "total": 0,
            "reason": "empty_universe",
            "ts": datetime.now().isoformat(timespec="seconds"),
        }

    try:
        quotes = (quote_service or QuoteService()).get_quotes(codes)
        data = compute(quotes)
    except Exception as exc:  # noqa: BLE001 - a missing card must not break the page
        logger.warning("market sentiment unavailable: {}", exc)
        with _cache_lock:
            if _cache["data"] is not None and _cache["date"] == today:
                return {**_cache["data"], "stale": True}
        return {
            "available": False,
            "total": 0,
            "reason": str(exc)[:200],
            "ts": datetime.now().isoformat(timespec="seconds"),
        }

    with _cache_lock:
        _cache.update({"ts": now, "date": today, "data": data})
    return data


def reset_cache() -> None:
    with _cache_lock:
        _cache.update({"ts": 0.0, "date": None, "data": None})


__all__ = ["compute", "market_sentiment", "reset_cache"]
