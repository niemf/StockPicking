"""Stock universe metadata: listing dates, ST flags and trading-day age."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import date, timedelta

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Stock, TradeCalendar
from app.providers.akshare_provider import AkShareProvider


def _st_flags(name: str) -> tuple[bool, bool]:
    cleaned = name.replace(" ", "").upper()
    is_st = "ST" in cleaned
    is_delisting = cleaned.startswith("*ST") or "退" in cleaned
    return is_st, is_delisting


def sync_universe(session: Session, provider: AkShareProvider | None = None) -> dict[str, int]:
    """Upsert the main-board stock master list from the two exchange lists."""
    provider = provider or AkShareProvider()
    rows = provider.get_listed_stocks()
    if not rows:
        raise RuntimeError("交易所股票列表为空，未更新数据库")

    existing = {stock.code: stock for stock in session.scalars(select(Stock))}
    inserted = updated = 0

    for row in rows:
        is_st, is_delisting = _st_flags(row["name"])
        stock = existing.get(row["code"])
        if stock is None:
            stock = Stock(
                code=row["code"],
                name=row["name"],
                exchange=row["exchange"],
                board=row["board"],
                industry=row.get("industry"),
                list_date=row.get("list_date"),
                is_st=is_st,
                is_delisting_risk=is_delisting,
                status="normal",
            )
            session.add(stock)
            inserted += 1
            continue

        stock.name = row["name"]
        stock.exchange = row["exchange"]
        stock.board = row["board"]
        stock.industry = row.get("industry") or stock.industry
        stock.list_date = row.get("list_date") or stock.list_date
        stock.is_st = is_st
        stock.is_delisting_risk = is_delisting
        if stock.status in {"", None}:
            stock.status = "normal"
        updated += 1

    session.commit()
    summary = {"inserted": inserted, "updated": updated, "total": len(rows)}
    logger.info("universe synced: {}", summary)
    return summary


def open_dates(session: Session) -> list[date]:
    return list(
        session.scalars(
            select(TradeCalendar.trade_date)
            .where(TradeCalendar.is_open.is_(True))
            .order_by(TradeCalendar.trade_date)
        )
    )


def listed_trading_days(
    list_date: date | None,
    today: date,
    calendar: list[date] | None = None,
) -> int:
    """Trading-day age used by the sub-new filter.

    Falls back to a weekday estimate when the calendar has not been synced yet,
    which is deliberately conservative (60 trading days ≈ 84 calendar days).
    """
    if list_date is None:
        return 9999
    if list_date > today:
        return 0

    # Anything listed more than ~120 calendar days ago has certainly traded for
    # more than 60 sessions. This avoids depending on how far back the cached
    # calendar happens to reach.
    if (today - list_date).days > 120:
        return 9999

    if calendar:
        start = bisect_left(calendar, list_date)
        end = bisect_right(calendar, today)
        return max(0, end - start)

    weekdays = 0
    cursor = list_date
    while cursor < today:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            weekdays += 1
    return weekdays
