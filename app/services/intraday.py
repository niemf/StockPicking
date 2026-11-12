"""Per-tick watchlist diagnostics for the alert engine.

The monitor already fetches one batched quote per tick. This module adds the
handful of numbers the intraday rules need — VWAP distance, distance to the
limit-up price, amplitude, and the deterministic technical score — without any
extra quote request and without AI.

Cost: one ``daily_bar`` read for the watchlist (tens of codes), once per minute.
Holdings are excluded: buy signals are a watchlist concept.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.providers.base import Quote


def enrich(
    stats: dict[str, dict[str, Any]],
    quotes: dict[str, Quote],
) -> dict[str, dict[str, Any]]:
    """Add quote-only fields (VWAP, limit distance, amplitude) to each stat."""
    for code, item in stats.items():
        quote = quotes.get(code)
        if quote is None or not item.get("available"):
            continue
        if quote.volume > 0 and quote.amount > 0:
            average = quote.amount / quote.volume
            item["vwap"] = round(average, 3)
            item["vwap_dist"] = round((quote.price / average - 1) * 100, 2)
        if quote.limit_up and quote.limit_up > quote.price > 0:
            item["limit_dist"] = round((quote.limit_up / quote.price - 1) * 100, 2)
        if quote.pre_close > 0:
            item["amplitude"] = round((quote.high - quote.low) / quote.pre_close * 100, 2)
    return stats


def build_stats(
    session: Session,
    codes: list[str],
    quotes: dict[str, Quote],
    *,
    moment: datetime | None = None,  # reserved for deterministic tests
) -> dict[str, dict[str, Any]]:
    """Compute diagnostics for a small set of codes (the watchlist)."""
    unique = list(dict.fromkeys(codes))
    if not unique:
        return {}

    # Imported lazily so importing the alert engine never pulls in pandas.
    from app.services.tech_snapshot import snapshot_many

    return enrich(snapshot_many(session, unique, quotes), quotes)


__all__ = ["build_stats", "enrich"]
