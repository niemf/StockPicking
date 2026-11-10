"""Trading calendar and intraday session helpers.

The calendar is synced once per trading day from AkShare and cached in the
``trade_calendar`` table, so the 120-second polling loop never waits on a
network call just to ask "is the market open?".
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import TradeCalendar

TZ = ZoneInfo(settings.monitor_timezone)

MORNING_OPEN = time(9, 30)
MORNING_CLOSE = time(11, 30)
AFTERNOON_OPEN = time(13, 0)
AFTERNOON_CLOSE = time(15, 0)
AUCTION_OPEN = time(9, 15)
AUCTION_CLOSE = time(9, 25)

SESSION_LABELS = {
    "closed": "休市",
    "pre_open": "盘前",
    "auction": "集合竞价",
    "morning": "上午交易",
    "lunch": "午间休市",
    "afternoon": "下午交易",
    "after_close": "盘后",
}


def now_local() -> datetime:
    """Current Beijing time as a naive datetime (the whole app stores naive)."""
    return datetime.now(TZ).replace(tzinfo=None)


def _as_date(day: date | datetime | None = None) -> date:
    if day is None:
        return now_local().date()
    if isinstance(day, datetime):
        return day.date()
    return day


def market_session(moment: datetime | None = None) -> str:
    """Classify a local datetime into one of the trading session labels."""
    moment = moment or now_local()
    clock = moment.time()
    if clock < AUCTION_OPEN:
        return "pre_open"
    if clock < AUCTION_CLOSE:
        return "auction"
    if clock < MORNING_OPEN:
        return "pre_open"
    if clock < MORNING_CLOSE:
        return "morning"
    if clock < AFTERNOON_OPEN:
        return "lunch"
    if clock < AFTERNOON_CLOSE:
        return "afternoon"
    return "after_close"


def is_polling_time(moment: datetime | None = None, include_auction: bool = False) -> bool:
    """True while the quote loop should run (auction is recorded, not alerted)."""
    session = market_session(moment)
    if include_auction:
        return session in {"auction", "morning", "afternoon"}
    return session in {"morning", "afternoon"}


def calendar_size(session: Session) -> int:
    return int(session.scalar(select(func.count()).select_from(TradeCalendar)) or 0)


def is_trading_day(session: Session, day: date | datetime | None = None) -> bool:
    """Look up the cached calendar, falling back to weekday logic before sync."""
    target = _as_date(day)
    if calendar_size(session) == 0:
        return target.weekday() < 5
    return session.get(TradeCalendar, target) is not None


def session_status(session: Session, moment: datetime | None = None) -> dict:
    moment = moment or now_local()
    day = moment.date()
    session_name = market_session(moment)
    trading_day = is_trading_day(session, day)
    return {
        "now": moment.isoformat(timespec="seconds"),
        "date": day.isoformat(),
        "session": session_name,
        "session_label": SESSION_LABELS[session_name],
        "is_trading_day": trading_day,
        "is_polling_time": trading_day and is_polling_time(moment),
        "calendar_rows": calendar_size(session),
    }


def sync_calendar(
    session: Session,
    *,
    start: date | None = None,
    end: date | None = None,
    provider=None,
) -> int:
    """Cache open trading days for ``[start, end]``; returns rows inserted.

    AkShare failures are logged and fall back to weekdays for the requested
    range, so the monitor can still start on a normal weekday after a network
    hiccup. Rows are upserted, so repeated syncs are cheap and idempotent.
    """
    today = now_local().date()
    start = start or (today - timedelta(days=7))
    end = end or (today + timedelta(days=45))

    dates: list[date] = []
    if provider is not None:
        try:
            dates = provider.get_trade_calendar(start=start, end=end)
        except Exception as exc:  # noqa: BLE001 - cache must survive provider outages
            logger.warning("trade calendar sync failed: {}", exc)

    if not dates:
        logger.warning("trade calendar empty for {}..{}; falling back to weekdays", start, end)
        dates = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
        dates = [day for day in dates if day.weekday() < 5]

    existing = set(
        session.scalars(
            select(TradeCalendar.trade_date).where(
                TradeCalendar.trade_date >= start, TradeCalendar.trade_date <= end
            )
        )
    )
    inserted = 0
    for day in dates:
        if day in existing:
            continue
        session.add(TradeCalendar(trade_date=day, is_open=True, note="open"))
        inserted += 1

    session.commit()
    logger.info("trade calendar synced: {} open days ({} new)", len(dates), inserted)
    return inserted
