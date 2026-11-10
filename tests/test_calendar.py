"""Trading calendar cache and session window tests."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import TradeCalendar
from app.services.calendar import (
    is_polling_time,
    is_trading_day,
    market_session,
    session_status,
    sync_calendar,
)


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db_session:
        yield db_session


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 10, hour, minute)


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (at(8, 30), "pre_open"),
        (at(9, 20), "auction"),
        (at(9, 30), "morning"),
        (at(11, 29), "morning"),
        (at(11, 30), "lunch"),
        (at(12, 59), "lunch"),
        (at(13, 0), "afternoon"),
        (at(14, 59), "afternoon"),
        (at(15, 0), "after_close"),
        (at(21, 0), "after_close"),
    ],
)
def test_market_session(session, moment: datetime, expected: str) -> None:
    assert market_session(moment) == expected


def test_is_polling_time_only_core_sessions() -> None:
    assert is_polling_time(at(9, 20)) is False
    assert is_polling_time(at(9, 20), include_auction=True) is True
    assert is_polling_time(at(10, 0)) is True
    assert is_polling_time(at(12, 0)) is False
    assert is_polling_time(at(14, 0)) is True
    assert is_polling_time(at(15, 1)) is False


def test_empty_calendar_falls_back_to_weekdays(session) -> None:
    assert is_trading_day(session, date(2026, 11, 10)) is True  # Tuesday
    assert is_trading_day(session, date(2026, 11, 7)) is False  # Saturday


def test_sync_calendar_is_idempotent(session) -> None:
    class FakeProvider:
        def get_trade_calendar(self, start=None, end=None):
            return [date(2026, 11, 9), date(2026, 11, 10)]

    first = sync_calendar(
        session,
        start=date(2026, 11, 1),
        end=date(2026, 11, 30),
        provider=FakeProvider(),
    )
    second = sync_calendar(
        session,
        start=date(2026, 11, 1),
        end=date(2026, 11, 30),
        provider=FakeProvider(),
    )

    assert first == 2
    assert second == 0
    assert session.scalar(select(TradeCalendar).where(TradeCalendar.trade_date == date(2026, 11, 10)))
    # With a real calendar, a weekday missing from it is a holiday.
    assert is_trading_day(session, date(2026, 11, 11)) is False


def test_sync_calendar_weekday_fallback_on_provider_error(session) -> None:
    class BrokenProvider:
        def get_trade_calendar(self, start=None, end=None):
            raise RuntimeError("network down")

    inserted = sync_calendar(
        session,
        start=date(2026, 11, 2),
        end=date(2026, 11, 8),
        provider=BrokenProvider(),
    )
    assert inserted == 5  # Mon-Fri
    assert is_trading_day(session, date(2026, 11, 2)) is True


def test_session_status(session) -> None:
    session.add(TradeCalendar(trade_date=date(2026, 11, 10)))
    session.commit()

    status = session_status(session, at(10, 0))
    assert status["is_trading_day"] is True
    assert status["is_polling_time"] is True
    assert status["session"] == "morning"

    weekend = session_status(session, datetime(2026, 11, 8, 10, 0))
    assert weekend["is_trading_day"] is False
    assert weekend["is_polling_time"] is False


def test_calendar_time_boundaries_are_exclusive_at_close() -> None:
    assert market_session(datetime(2026, 11, 10, 11, 30, 1)) == "lunch"
    assert market_session(datetime(2026, 11, 10, 13, 0, 1)) == "afternoon"
