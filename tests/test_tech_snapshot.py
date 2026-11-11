"""Deterministic technical snapshot used by the watchlist cards."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import DailyBar, Stock
from app.providers.base import Quote
from app.services.tech_snapshot import build_snapshot, snapshot_many


def make_bars(days: int = 60) -> pd.DataFrame:
    rows = []
    start = date(2026, 8, 1)
    for index in range(days):
        close = 10.0 + index * 0.05
        volume = 1_000_000.0
        if index >= days - 3:
            volume = 3_000_000.0  # recent volume spike
        rows.append(
            {
                "trade_date": start + timedelta(days=index),
                "open": close - 0.03,
                "high": close + 0.08,
                "low": close - 0.08,
                "close": close,
                "volume": volume,
                "amount": volume * close,
                "turnover_rate": 1.0,
            }
        )
    return pd.DataFrame(rows)


def make_quote(price: float = 13.2) -> Quote:
    return Quote(
        code="600036",
        name="招商银行",
        price=price,
        pre_close=13.1,
        open=13.0,
        high=13.3,
        low=12.9,
        volume=3_500_000.0,
        amount=4e8,
        pct_chg=0.76,
        change=0.1,
        turnover_rate=1.1,
        ts=datetime.now(),
        source="test",
    )


def test_build_snapshot_levels_and_trend() -> None:
    bars = make_bars()
    snapshot = build_snapshot("600036", bars, make_quote(), name="招商银行", industry="银行")

    assert snapshot["available"] is True
    assert snapshot["price"] == pytest.approx(13.2)
    # The live quote is appended as today's bar, so windows are 4/19/29 bars + quote.
    expected_ma5 = round((bars["close"].tail(4).sum() + 13.2) / 5, 2)
    expected_ma20 = round((bars["close"].tail(19).sum() + 13.2) / 20, 2)
    assert snapshot["ma5"] == pytest.approx(expected_ma5, abs=0.01)
    assert snapshot["ma20"] == pytest.approx(expected_ma20, abs=0.01)
    assert snapshot["ma30"] is not None
    assert snapshot["trend"] == "多头排列"
    assert snapshot["trend_class"] == "up"

    assert snapshot["support"] is not None and snapshot["support"] < snapshot["price"]
    assert snapshot["resistance"] is not None and snapshot["resistance"] > snapshot["price"]
    assert snapshot["dense_low"] <= snapshot["dense_high"]
    assert snapshot["dense_low"] >= bars["low"].min()
    assert snapshot["dense_high"] <= bars["high"].max()
    assert snapshot["volume_state"] in {"放量", "量能正常", "缩量"}
    assert snapshot["verdict"] in {"看多", "中性", "看空"}
    assert snapshot["verdict"] == "看多"
    assert snapshot["score"] > 0
    assert snapshot["bullish"]
    assert "综合评分" in snapshot["summary"]


def test_verdict_is_bearish_on_downtrend() -> None:
    rows = []
    start = date(2026, 8, 1)
    for index in range(60):
        close = 20.0 - index * 0.12
        rows.append(
            {
                "trade_date": start + timedelta(days=index),
                "open": close + 0.03,
                "high": close + 0.08,
                "low": close - 0.08,
                "close": close,
                "volume": 1_000_000.0,
                "amount": 1e8,
                "turnover_rate": 1.0,
            }
        )
    bars = pd.DataFrame(rows)
    quote = Quote(
        code="600036",
        name="招商银行",
        price=float(bars["close"].iloc[-1]),
        pre_close=float(bars["close"].iloc[-1]) + 0.1,
        open=float(bars["close"].iloc[-1]),
        high=float(bars["close"].iloc[-1]),
        low=float(bars["close"].iloc[-1]),
        volume=1_200_000.0,
        amount=1e8,
        pct_chg=-0.6,
        change=-0.1,
        turnover_rate=1.0,
        ts=datetime.now(),
        source="test",
    )
    snapshot = build_snapshot("600036", bars, quote, name="招商银行", industry="银行")

    assert snapshot["verdict"] == "看空"
    assert snapshot["verdict_class"] == "down"
    assert snapshot["score"] < 0
    assert snapshot["bearish"]
    assert "综合评分" in snapshot["summary"]


def test_build_snapshot_handles_short_history() -> None:
    short = build_snapshot("600036", make_bars(days=10), None)
    assert short["available"] is False
    assert short["reason"] == "bars_missing"


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db_session:
        db_session.add(
            Stock(code="600036", name="招商银行", exchange="sh", board="沪市主板", industry="银行")
        )
        for row in make_bars().to_dict("records"):
            db_session.add(DailyBar(code="600036", **row))
        db_session.commit()
        yield db_session


def test_snapshot_many_uses_live_quote(session) -> None:
    result = snapshot_many(session, ["600036"], {"600036": make_quote(13.4)})
    snapshot = result["600036"]

    assert snapshot["available"] is True
    assert snapshot["price"] == pytest.approx(13.4)
    assert snapshot["pct_chg"] == pytest.approx(0.76)
    assert snapshot["last_bar"]
    assert snapshot_many(session, []) == {}
