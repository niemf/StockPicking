"""Persisted indicator tables: daily snapshots, weekly resample and readers."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import DailyBar, IndicatorDaily, WeeklyBar
from app.services.bars import upsert_bars
from app.services.indicators import (
    _weekly_frame,
    load_indicator_map,
    load_weekly_map,
    rebuild_daily,
    rebuild_weekly,
    weekly_trend,
)

TODAY = date(2026, 11, 10)


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db_session:
        yield db_session


def make_bars(days: int = 130, start_price: float = 10.0) -> pd.DataFrame:
    dates = pd.date_range(end=TODAY, periods=days, freq="B")
    rows = []
    price = start_price
    for index, timestamp in enumerate(dates):
        price = price * (1.001 if index % 3 else 0.998)
        rows.append(
            {
                "trade_date": timestamp.date(),
                "open": price * 0.995,
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
                "volume": 1_000_000.0 + index * 1000,
                "amount": price * 1_000_000.0,
                "turnover_rate": 3.0,
            }
        )
    return pd.DataFrame(rows)


def test_weekly_frame_aggregates_calendar_weeks() -> None:
    bars = make_bars(days=40)
    weekly = _weekly_frame(bars)

    assert len(weekly) == len(pd.unique(bars["trade_date"].map(lambda d: d.isocalendar()[:2])))
    first_week = weekly.iloc[0]
    week_rows = bars[
        bars["trade_date"].map(lambda d: d.isocalendar()[:2]) == bars["trade_date"].iloc[0].isocalendar()[:2]
    ]
    assert first_week["open"] == pytest.approx(week_rows["open"].iloc[0])
    assert first_week["high"] == pytest.approx(week_rows["high"].max())
    assert first_week["low"] == pytest.approx(week_rows["low"].min())
    assert first_week["close"] == pytest.approx(week_rows["close"].iloc[-1])
    assert first_week["volume"] == pytest.approx(week_rows["volume"].sum())


def test_weekly_moving_averages_need_enough_weeks() -> None:
    weekly = _weekly_frame(make_bars(days=10))  # two calendar weeks
    assert weekly["ma5"].isna().all()
    assert weekly["ma20"].isna().all()
    assert weekly["weeks"].iloc[-1] == len(weekly)


def test_weekly_trend_labels() -> None:
    assert weekly_trend(12.0, 11.0, 12.5) == "up"
    assert weekly_trend(9.0, 11.0, 10.0) == "down"
    assert weekly_trend(11.0, 10.9, 10.5) == "flat"
    assert weekly_trend(None, 10.0, 10.0) is None


def test_rebuild_daily_persists_ma_lines(session) -> None:
    bars = make_bars()
    upsert_bars(session, "600000", bars)
    session.commit()

    stats = rebuild_daily(session, ["600000"], days=260)
    assert stats["rows"] == len(bars)

    rows = list(
        session.scalars(
            select(IndicatorDaily).where(IndicatorDaily.code == "600000").order_by(IndicatorDaily.trade_date)
        )
    )
    assert len(rows) == len(bars)
    expected_ma5 = bars["close"].rolling(5).mean().iloc[-1]
    assert rows[-1].ma5 == pytest.approx(expected_ma5, rel=1e-4)
    assert rows[-1].ma20 is not None and rows[-1].ma30 is not None
    assert rows[-1].close == pytest.approx(bars["close"].iloc[-1], abs=1e-4)
    assert rows[-1].upper_shadow_pct is not None
    assert rows[-1].volume_ratio is not None


def test_rebuild_daily_is_idempotent(session) -> None:
    upsert_bars(session, "600000", make_bars())
    session.commit()

    rebuild_daily(session, ["600000"], progress=False)
    rebuild_daily(session, ["600000"], progress=False)

    assert session.scalar(select(func.count()).select_from(IndicatorDaily)) == len(make_bars())


def test_days_limit_only_keeps_the_tail(session) -> None:
    upsert_bars(session, "600000", make_bars())
    session.commit()

    stats = rebuild_daily(session, ["600000"], days=10, progress=False)

    assert stats["rows"] == 10
    assert session.scalar(select(func.count()).select_from(IndicatorDaily)) == 10


def test_rebuild_weekly_persists_weeks(session) -> None:
    upsert_bars(session, "600000", make_bars())
    session.commit()

    stats = rebuild_weekly(session, ["600000"], keep_weeks=6)

    assert stats["rows"] == 6
    rows = list(session.scalars(select(WeeklyBar).where(WeeklyBar.code == "600000")))
    assert len(rows) == 6
    assert all(len(str(row.week_end)) == 10 for row in rows)


def test_load_indicator_map_returns_latest_row_per_code(session) -> None:
    upsert_bars(session, "600000", make_bars())
    session.commit()
    rebuild_daily(session, ["600000"], progress=False)

    latest = load_indicator_map(session, ["600000"])
    assert latest["600000"]["trade_date"] == TODAY.isoformat()
    assert load_indicator_map(session, []) == {}

    # Explicit date returns that day, not the newest one.
    yesterday = max(
        row.trade_date
        for row in session.scalars(select(IndicatorDaily).where(IndicatorDaily.code == "600000"))
        if row.trade_date < TODAY
    )
    picked = load_indicator_map(session, ["600000"], trade_date=yesterday)
    assert picked["600000"]["trade_date"] == yesterday.isoformat()


def test_load_weekly_map_includes_trend(session) -> None:
    upsert_bars(session, "600000", make_bars(days=260))
    session.commit()
    rebuild_weekly(session, ["600000"], keep_weeks=12)

    week = load_weekly_map(session, ["600000"], lookback_days=30, today=TODAY)["600000"]

    assert week["week_end"] <= TODAY.isoformat()
    assert week["trend"] in {"up", "down", "flat"}
    assert load_weekly_map(session, [], today=TODAY) == {}


def test_weekly_rows_are_not_written_without_history(session) -> None:
    bars = make_bars(days=10)
    upsert_bars(session, "600000", bars)
    session.commit()

    stats = rebuild_weekly(session, ["600000"])

    assert stats["rows"] == 0
    assert stats["skipped"] == 1


def test_daily_bar_dates_survive_the_round_trip(session) -> None:
    upsert_bars(session, "600000", make_bars())
    session.commit()
    rebuild_daily(session, ["600000"], progress=False)

    newest = session.scalar(select(func.max(IndicatorDaily.trade_date)))
    assert newest == TODAY
    assert newest - timedelta(days=1) < newest


def test_indicator_table_matches_live_features(session) -> None:
    """The table must never drift from what the funnel computes on the fly."""
    from app.analysis.context import StockContext, StockMeta
    from app.services.bars import load_bars_map

    bars = make_bars()
    upsert_bars(session, "600000", bars)
    session.commit()
    rebuild_daily(session, ["600000"], progress=False)

    stored = load_indicator_map(session, ["600000"])["600000"]
    ctx = StockContext(
        meta=StockMeta(code="600000", name="测试"),
        bars=load_bars_map(session, ["600000"])["600000"],
    )

    for field in ("ma5", "ma10", "ma20", "ma30", "ma60", "atr_pct", "volume_ratio", "ret5", "ret20"):
        live = ctx.feature(field)
        assert live is not None, field
        assert stored[field] == pytest.approx(live, rel=1e-3, abs=1e-3), field
