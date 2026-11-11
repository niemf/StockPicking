"""Deterministic per-stock technical snapshot for the watchlist cards.

Everything here comes from local daily bars plus the live quote: moving
averages, trend alignment, volume state, nearest support/resistance and the
densest traded price band (筹码密集区). No AI, no extra network call.

The watchlist page calls :func:`snapshot_many` once per request; bars are loaded
in one query and every indicator is computed in memory.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.analysis.context import StockContext, StockMeta
from app.db.models import Stock
from app.providers.base import Quote
from app.services.discovery import load_bars_map

MA_KEYS = ("ma5", "ma10", "ma20", "ma30", "ma60")
DENSE_BINS = 12
DENSE_LOOKBACK = 60


def _num(value: Any, digits: int = 2) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return round(number, digits)


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


def build_snapshot(
    code: str,
    bars: pd.DataFrame | None,
    quote: Quote | None = None,
    name: str | None = None,
    industry: str | None = None,
) -> dict[str, Any]:
    """Compute one snapshot from bars (+ optional live quote)."""
    if bars is None or bars.empty or len(bars) < 20:
        return {"available": False, "code": code, "reason": "bars_missing"}

    frame = bars.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    meta = StockMeta(code=code, name=name or code, industry=industry)
    context = StockContext(meta=meta, quote=quote, bars=frame)

    ma: dict[str, float | None] = {}
    for key in MA_KEYS:
        try:
            ma[key] = _num(context.feature(key))
        except Exception:  # noqa: BLE001 - one missing MA must not kill the card
            ma[key] = None

    price = _num(quote.price if quote is not None and quote.price > 0 else frame["close"].iloc[-1])
    pct_chg = _num(quote.pct_chg) if quote is not None else _num(
        (frame["close"].iloc[-1] / frame["close"].iloc[-2] - 1) * 100
    )
    if not price:
        return {"available": False, "code": code, "reason": "price_missing"}

    low20 = _num(frame.tail(20)["low"].min())
    high20 = _num(frame.tail(20)["high"].max())
    dense_low, dense_high = _dense_zone(frame)

    supports = [value for value in (low20, ma.get("ma20"), dense_low) if value and value < price]
    resistances = [
        value
        for value in (high20, ma.get("ma20"), dense_high)
        if value and value > price
    ]
    support = max(supports) if supports else _num(price * 0.95)
    resistance = min(resistances) if resistances else _num(price * 1.05)

    volume_ratio = _num(context.feature("volume_ratio"))
    trend, trend_class = _trend(ma)
    ma20_dist = _num((price / ma["ma20"] - 1) * 100) if ma.get("ma20") else None
    support_dist = _num((price / support - 1) * 100) if support else None
    resistance_dist = _num((resistance / price - 1) * 100) if resistance else None

    parts = []
    if trend_class == "up":
        parts.append("均线多头排列")
    elif trend_class == "down":
        parts.append("均线空头排列")
    else:
        parts.append("均线纠缠")
    if ma20_dist is not None:
        parts.append(f"距 MA20 {ma20_dist:+.1f}%")
    if volume_ratio is not None:
        parts.append(f"量比 {volume_ratio:.2f}")
    if support and resistance:
        parts.append(f"支撑 {support:.2f} / 压力 {resistance:.2f}")
    if dense_low and dense_high:
        parts.append(f"密集区 {dense_low:.2f}–{dense_high:.2f}")

    return {
        "available": True,
        "code": code,
        "price": price,
        "pct_chg": pct_chg,
        "ma5": ma.get("ma5"),
        "ma10": ma.get("ma10"),
        "ma20": ma.get("ma20"),
        "ma30": ma.get("ma30"),
        "ma60": ma.get("ma60"),
        "ma20_dist": ma20_dist,
        "trend": trend,
        "trend_class": trend_class,
        "volume_ratio": volume_ratio,
        "volume_state": _volume_state(volume_ratio),
        "support": support,
        "resistance": resistance,
        "support_dist": support_dist,
        "resistance_dist": resistance_dist,
        "dense_low": dense_low,
        "dense_high": dense_high,
        "low20": low20,
        "high20": high20,
        "bars": int(len(frame)),
        "last_bar": str(frame["trade_date"].iloc[-1]),
        "summary": "；".join(parts),
    }


def snapshot_many(
    session: Session,
    codes: list[str],
    quotes: dict[str, Quote] | None = None,
) -> dict[str, dict[str, Any]]:
    """Compute snapshots for many codes with a single bars query."""
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
