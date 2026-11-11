"""Deterministic per-stock technical diagnosis.

The AI report card was restating numbers we already have: price, turnover,
moving averages, MACD, RSI, support/resistance. Those facts and the resulting
verdict are computed here, from local daily bars plus the live quote — no model
call, no cost, and the same input always gives the same output.

Output shape (one dict per stock)::

    price / pct_chg / amount_yi / turnover_rate
    ma5 ma10 ma20 ma30 ma60 + ma20_dist + ma20_slope3
    volume_ratio / volume_state / rsi12 / macd_hist / atr_pct
    low20 / high20 / high20_prev / support / resistance / dense zone
    score / verdict (看多/中性/看空) / verdict_class (up/flat/down)
    bullish[] / bearish[] / signals[] / summary

``verdict_class`` follows A-share colours: ``up`` = red (bullish),
``down`` = green (bearish).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.analysis.context import StockContext, StockMeta
from app.db.models import Stock
from app.providers.base import Quote
from app.services.calendar import market_session
from app.services.discovery import load_bars_map

MA_KEYS = ("ma5", "ma10", "ma20", "ma30", "ma60")
DENSE_BINS = 12
DENSE_LOOKBACK = 60

VERDICT_BULL = 3
VERDICT_BEAR = -3


def _num(value: Any, digits: int = 2) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return round(number, digits)


def _feature(context: StockContext, key: str, digits: int = 4) -> float | None:
    try:
        return _num(context.feature(key), digits)
    except Exception:  # noqa: BLE001 - one missing indicator must not kill the card
        return None


def _dense_zone(bars: pd.DataFrame) -> tuple[float | None, float | None]:
    """Highest-volume price band over the recent window (typical price bins)."""
    tail = bars.tail(DENSE_LOOKBACK)
    if len(tail) < 10:
        return None, None
    typical = (tail["high"] + tail["low"] + tail["close"]) / 3.0
    volume = tail["volume"].fillna(0.0).to_numpy(dtype=float)
    if not np.isfinite(typical).all() or volume.sum() <= 0:
        return None, None

    low, high = float(typical.min()), float(typical.max())
    if high <= low:
        return _num(low), _num(high)

    edges = np.linspace(low, high, DENSE_BINS + 1)
    index = np.clip(np.digitize(typical.to_numpy(dtype=float), edges) - 1, 0, DENSE_BINS - 1)
    weights = np.zeros(DENSE_BINS)
    np.add.at(weights, index, volume)
    top = int(weights.argmax())
    return _num(edges[top]), _num(edges[top + 1])


def _trend(ma: dict[str, float | None]) -> tuple[str, str]:
    values = [ma.get(key) for key in ("ma5", "ma10", "ma20", "ma30")]
    if any(value is None for value in values):
        return "数据不足", "flat"
    ma5, ma10, ma20, ma30 = values
    if ma5 > ma10 > ma20 > ma30:
        return "多头排列", "up"
    if ma5 < ma10 < ma20 < ma30:
        return "空头排列", "down"
    return "震荡整理", "flat"


def _volume_state(ratio: float | None) -> str:
    if ratio is None:
        return "—"
    if ratio >= 1.5:
        return "放量"
    if ratio <= 0.7:
        return "缩量"
    return "量能正常"


def _session_fraction() -> float:
    """Fraction of the trading day elapsed, so an intraday 量比 is comparable."""
    session = market_session()
    if session in {"after_close", "closed"}:
        return 1.0
    if session == "lunch":
        return 0.5
    if session == "morning":
        now = datetime.now()
        elapsed = (now.hour * 60 + now.minute) - (9 * 60 + 30)
        return min(1.0, max(0.05, (elapsed + 1) / 240))
    if session == "afternoon":
        now = datetime.now()
        elapsed = 120 + (now.hour * 60 + now.minute) - (13 * 60)
        return min(1.0, max(0.05, (elapsed + 1) / 240))
    return 0.05


def _signal(label: str, weight: int, detail: str | None = None) -> dict[str, Any]:
    return {"label": label, "weight": weight, "detail": detail, "side": "bull" if weight > 0 else "bear"}


def _score(facts: dict[str, Any]) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
    """Rule-based score; every point is explainable and shown on the card."""
    price = facts["price"]
    bullish: list[dict[str, Any]] = []
    bearish: list[dict[str, Any]] = []

    def add(label: str, weight: int, detail: str | None = None) -> None:
        (bullish if weight > 0 else bearish).append(_signal(label, weight, detail))

    ma20 = facts.get("ma20")
    ma60 = facts.get("ma60")
    ma5, ma10, ma30 = facts.get("ma5"), facts.get("ma10"), facts.get("ma30")
    ma20_dist = facts.get("ma20_dist")
    slope = facts.get("ma20_slope3")
    macd = facts.get("macd_hist")
    rsi = facts.get("rsi12")
    volume_ratio = facts.get("volume_ratio")
    pct = facts.get("pct_chg") or 0.0
    resistance_dist = facts.get("resistance_dist")
    drawdown = facts.get("drawdown20")

    if ma20 is not None and price:
        if price > ma20:
            add("站上 MA20", 2, f"{ma20_dist:+.1f}%" if ma20_dist is not None else None)
        else:
            add("跌破 MA20", -2, f"{ma20_dist:+.1f}%" if ma20_dist is not None else None)

    if ma5 is not None and ma10 is not None:
        if ma5 > ma10:
            add("MA5 高于 MA10", 1)
        else:
            add("MA5 低于 MA10", -1)

    if slope is not None:
        if slope >= 0.3:
            add("MA20 向上", 2, f"{slope:+.2f}%")
        elif slope <= -0.3:
            add("MA20 向下", -2, f"{slope:+.2f}%")

    if ma5 and ma10 and ma20 and ma30:
        if ma5 > ma10 > ma20 > ma30:
            add("均线多头排列", 1)
        elif ma5 < ma10 < ma20 < ma30:
            add("均线空头排列", -1)

    if macd is not None:
        if macd > 0:
            add("MACD 柱为正", 1)
        else:
            add("MACD 柱为负", -1)

    if rsi is not None:
        if 50 <= rsi <= 70:
            add("RSI 处于强势区", 1, f"{rsi:.0f}")
        elif rsi > 80:
            add("RSI 超买", -1, f"{rsi:.0f}")
        elif rsi < 35:
            add("RSI 弱势", -1, f"{rsi:.0f}")

    if ma60 is not None and price:
        if price > ma60:
            add("站上 MA60", 1)
        else:
            add("MA60 上方有压力", -1)

    if facts.get("breakout"):
        add("突破 20 日高点", 2)

    if volume_ratio is not None:
        if volume_ratio >= 1.2 and pct >= 1:
            add("放量上涨", 1, f"量比 {volume_ratio:.2f}")
        elif volume_ratio >= 1.5 and pct <= -2:
            add("放量下跌", -2, f"量比 {volume_ratio:.2f}")

    if resistance_dist is not None and 0 < resistance_dist <= 1.5:
        add("接近压力位", -1, f"距压力 {resistance_dist:.1f}%")

    if drawdown is not None and drawdown <= -10:
        add("距 20 日高点回撤较大", -1, f"{drawdown:.1f}%")

    bullish.sort(key=lambda item: abs(item["weight"]), reverse=True)
    bearish.sort(key=lambda item: abs(item["weight"]), reverse=True)
    total = sum(item["weight"] for item in bullish + bearish)
    return total, bullish, bearish


def _verdict(score: int) -> tuple[str, str, int]:
    if score >= VERDICT_BULL:
        return "看多", "up", min(95, 50 + score * 5)
    if score <= VERDICT_BEAR:
        return "看空", "down", min(95, 50 + abs(score) * 5)
    return "中性", "flat", min(80, 45 + abs(score) * 5)


def _summary(facts: dict[str, Any]) -> str:
    parts = [
        f"现价 {facts['price']:.2f}，{facts.get('pct_chg') or 0:+.2f}%，"
        f"成交 {facts.get('amount_yi') or 0:.2f} 亿，换手 {facts.get('turnover_rate') or 0:.2f}%"
    ]
    if facts.get("ma20"):
        dist = facts.get("ma20_dist")
        position = "上方" if (dist or 0) >= 0 else "下方"
        slope = facts.get("ma20_slope3")
        parts.append(
            f"位于 MA20 {position}（{dist:+.1f}%）" if dist is not None else f"MA20 {facts['ma20']:.2f}"
        )
        if slope is not None:
            parts.append(f"MA20 三日斜率 {slope:+.2f}%")
    if facts.get("ma60"):
        parts.append(
            f"MA60 {facts['ma60']:.2f} {'在上方构成压力' if facts['price'] < facts['ma60'] else '形成支撑'}"
        )
    if facts.get("rsi12") is not None:
        parts.append(f"RSI12 {facts['rsi12']:.0f}")
    if facts.get("macd_hist") is not None:
        parts.append(f"MACD 柱{'为正' if facts['macd_hist'] > 0 else '为负'}")
    if facts.get("volume_ratio") is not None:
        parts.append(f"量比 {facts['volume_ratio']:.2f}（{facts.get('volume_state')}）")
    return "；".join(parts) + f"。综合评分 {facts['score']:+d} → {facts['verdict']}。"


def build_snapshot(
    code: str,
    bars: pd.DataFrame | None,
    quote: Quote | None = None,
    name: str | None = None,
    industry: str | None = None,
) -> dict[str, Any]:
    """Compute facts + verdict for one stock."""
    if bars is None or bars.empty or len(bars) < 20:
        return {"available": False, "code": code, "reason": "bars_missing"}

    frame = bars.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    meta = StockMeta(code=code, name=name or code, industry=industry)
    context = StockContext(meta=meta, quote=quote, bars=frame)

    ma: dict[str, float | None] = {}
    for key in MA_KEYS:
        ma[key] = _feature(context, key, 2)

    price = _num(quote.price if quote is not None and quote.price > 0 else frame["close"].iloc[-1])
    if not price:
        return {"available": False, "code": code, "reason": "price_missing"}

    pct_chg = _num(quote.pct_chg) if quote is not None else _num(
        (frame["close"].iloc[-1] / frame["close"].iloc[-2] - 1) * 100
    )
    amount = quote.amount if quote is not None else _num(frame["amount"].iloc[-1], 2)
    turnover = quote.turnover_rate if quote is not None else _num(frame["turnover_rate"].iloc[-1])
    if not amount:
        amount = _num(frame["amount"].iloc[-1], 2)
    if not turnover:
        turnover = _num(frame["turnover_rate"].iloc[-1])
    high_today = quote.high if quote is not None and quote.high else _num(frame["high"].iloc[-1])

    low20 = _num(frame.tail(20)["low"].min())
    high20 = _num(frame.tail(20)["high"].max())
    high20_prev = _feature(context, "high20_prev", 2)
    dense_low, dense_high = _dense_zone(frame)

    supports = [value for value in (low20, ma.get("ma20"), dense_low) if value and value < price]
    resistances = [
        value for value in (high20, ma.get("ma20"), dense_high) if value and value > price
    ]
    support = max(supports) if supports else _num(price * 0.95)
    resistance = min(resistances) if resistances else _num(price * 1.05)

    volume_ratio = _feature(context, "volume_ratio")
    progress = _session_fraction()
    if volume_ratio is not None and progress < 0.98:
        # Intraday volume is partial; scale it to a full-session equivalent.
        volume_ratio = _num(volume_ratio / progress)
    trend, trend_class = _trend(ma)
    ma20_dist = _num((price / ma["ma20"] - 1) * 100) if ma.get("ma20") else None
    support_dist = _num((price / support - 1) * 100) if support else None
    resistance_dist = _num((resistance / price - 1) * 100) if resistance else None
    breakout = bool(
        high20_prev and high_today and high_today > high20_prev and price > high20_prev
    )

    facts: dict[str, Any] = {
        "available": True,
        "code": code,
        "price": price,
        "pct_chg": pct_chg,
        "amount_yi": _num((amount or 0) / 1e8),
        "turnover_rate": _num(turnover),
        "ma5": ma.get("ma5"),
        "ma10": ma.get("ma10"),
        "ma20": ma.get("ma20"),
        "ma30": ma.get("ma30"),
        "ma60": ma.get("ma60"),
        "ma20_dist": ma20_dist,
        "ma20_slope3": _feature(context, "ma20_slope3"),
        "trend": trend,
        "trend_class": trend_class,
        "volume_ratio": volume_ratio,
        "volume_state": _volume_state(volume_ratio),
        "rsi12": _feature(context, "rsi12"),
        "macd_hist": _feature(context, "macd_hist", 4),
        "atr_pct": _feature(context, "atr_pct"),
        "ret20": _feature(context, "ret20"),
        "drawdown20": _feature(context, "drawdown20"),
        "low20": low20,
        "high20": high20,
        "high20_prev": high20_prev,
        "support": support,
        "resistance": resistance,
        "support_dist": support_dist,
        "resistance_dist": resistance_dist,
        "dense_low": dense_low,
        "dense_high": dense_high,
        "breakout": breakout,
        "bars": int(len(frame)),
        "last_bar": str(frame["trade_date"].iloc[-1]),
    }

    score, bullish, bearish = _score(facts)
    verdict, verdict_class, strength = _verdict(score)
    key_signals = sorted(bullish + bearish, key=lambda item: abs(item["weight"]), reverse=True)[:3]
    facts.update(
        {
            "score": score,
            "verdict": verdict,
            "verdict_class": verdict_class,
            "strength": strength,
            "bullish": bullish,
            "bearish": bearish,
            "signals": bullish + bearish,
            "key_signals": key_signals,
        }
    )
    facts["summary"] = _summary(facts)
    return facts


def snapshot_many(
    session: Session,
    codes: list[str],
    quotes: dict[str, Quote] | None = None,
) -> dict[str, dict[str, Any]]:
    """Compute diagnostics for many codes with a single bars query."""
    quotes = quotes or {}
    unique = list(dict.fromkeys(codes))
    if not unique:
        return {}

    bars_map = load_bars_map(session, unique)
    stocks = {row.code: row for row in session.query(Stock).filter(Stock.code.in_(unique))}
    result: dict[str, dict[str, Any]] = {}
    for code in unique:
        stock = stocks.get(code)
        result[code] = build_snapshot(
            code,
            bars_map.get(code),
            quotes.get(code),
            name=stock.name if stock else None,
            industry=stock.industry if stock else None,
        )
    return result


__all__ = ["build_snapshot", "snapshot_many"]
