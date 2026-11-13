"""市场级日线（market_daily）回填与情绪阶段落库。"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import DailyBar, MarketDaily, Stock
from app.services import market_daily as md
from app.services.cycle import CycleState
from app.services.bars import upsert_bars

TODAY = date(2026, 11, 12)


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


def seed_bars(session, codes=("600000", "600001", "600002"), days=30, *, limit_up_days=0):
    import pandas as pd

    for stock_code in codes:
        rows = []
        dates = pd.date_range(end=TODAY - timedelta(days=1), periods=days, freq="B")
        price = 10.0
        for index, timestamp in enumerate(dates):
            if index >= days - limit_up_days and limit_up_days:
                price = round(price * 1.1, 2)
            else:
                price = round(price * 1.001, 2)
            rows.append(
                {
                    "trade_date": timestamp.date(),
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": 1_000_000.0,
                    "amount": price * 1_000_000.0,
                    "turnover_rate": 3.0,
                }
            )
        upsert_bars(session, stock_code, pd.DataFrame(rows))
    session.commit()


def seed_stocks(session, codes=("600000", "600001", "600002")):
    for code in codes:
        session.add(
            Stock(
                code=code,
                name=f"测试{code[-2:]}",
                exchange="sh",
                board="沪市主板",
                list_date=date(2010, 1, 1),
                status="normal",
            )
        )
    session.commit()


def test_backfill_writes_one_row_per_day(session, monkeypatch) -> None:
    seed_stocks(session)
    seed_bars(session)
    monkeypatch.setattr(md, "index_rows", lambda *a, **k: {})

    stats = md.backfill_from_bars(session, days=10)

    assert stats["rows"] == 10
    total = session.scalar(select(func.count()).select_from(MarketDaily))
    assert total == 10
    row = session.scalar(select(MarketDaily).order_by(MarketDaily.trade_date.desc()).limit(1))
    assert row.source == "bars"
    assert row.total == 3
    assert row.breadth is not None
    assert row.phase in {"ice", "repair", "ferment", "climax", "ebb"}


def test_backfill_counts_limit_ups_and_premium(session, monkeypatch) -> None:
    seed_stocks(session)
    # 最后两天全部涨停：第二天的 prev_premium 应为 +10%
    seed_bars(session, days=8, limit_up_days=2)
    monkeypatch.setattr(md, "index_rows", lambda *a, **k: {})

    md.backfill_from_bars(session, days=10)

    rows = list(session.scalars(select(MarketDaily).order_by(MarketDaily.trade_date)))
    last = rows[-1]
    assert last.limit_up == 3
    assert last.prev_premium == pytest.approx(10.0, abs=0.2)


def test_backfill_skips_today(session, monkeypatch) -> None:
    seed_stocks(session)
    import pandas as pd

    rows = [
        {
            "trade_date": TODAY,
            "open": 10,
            "high": 10,
            "low": 10,
            "close": 10,
            "volume": 1.0,
            "amount": 1.0,
            "turnover_rate": 1.0,
        }
    ]
    upsert_bars(session, "600000", pd.DataFrame(rows))
    session.commit()
    monkeypatch.setattr(md, "index_rows", lambda *a, **k: {})

    md.backfill_from_bars(session, days=5, today=TODAY)

    assert session.get(MarketDaily, TODAY) is None


def test_snapshot_uses_live_cycle_metrics(session, monkeypatch) -> None:
    monkeypatch.setattr(md, "index_rows", lambda *a, **k: {TODAY: {"sh_close": 4000.0, "sh_pct": 0.5}})
    cycle = CycleState(
        phase="ferment",
        label="发酵",
        temperature=72.0,
        position_cap=80,
        rr_floor=2.0,
        advice="主升段",
        metrics={
            "available": True,
            "total": 3000,
            "up": 2400,
            "down": 500,
            "flat": 100,
            "limit_up": 62,
            "limit_down": 3,
            "break_rate": 0.25,
            "max_streak": 5,
            "prev_premium": 1.8,
            "breadth": 80.0,
            "median_pct": 1.1,
            "amount_yi": 10800.0,
        },
    )

    row = md.snapshot(session, TODAY, cycle=cycle)

    assert row is not None
    assert row.phase == "ferment"
    assert row.limit_up == 62
    assert row.break_rate == pytest.approx(0.25)
    assert row.sh_close == 4000.0
    assert row.source == "live"
    assert session.get(MarketDaily, TODAY) is not None


def test_snapshot_is_idempotent(session, monkeypatch) -> None:
    monkeypatch.setattr(md, "index_rows", lambda *a, **k: {})
    metrics = {
        "available": True,
        "total": 3000,
        "up": 1500,
        "down": 1400,
        "flat": 100,
        "limit_up": 40,
        "limit_down": 5,
        "breadth": 50.0,
        "median_pct": 0.0,
        "amount_yi": 9000.0,
    }
    cycle = CycleState("repair", "修复", 50.0, 60, 2.0, "正常", metrics)

    md.snapshot(session, TODAY, cycle=cycle)
    metrics["limit_up"] = 45
    md.snapshot(session, TODAY, cycle=cycle)

    assert session.scalar(select(func.count()).select_from(MarketDaily)) == 1
    assert session.get(MarketDaily, TODAY).limit_up == 45


def test_snapshot_ignores_unavailable_cycle(session) -> None:
    cycle = CycleState("repair", "修复", 0.0, 60, 2.0, "数据不可用", {"available": False})

    assert md.snapshot(session, TODAY, cycle=cycle) is None


def test_load_history_returns_ascending_window(session, monkeypatch) -> None:
    seed_stocks(session)
    seed_bars(session)
    monkeypatch.setattr(md, "index_rows", lambda *a, **k: {})
    md.backfill_from_bars(session, days=10)

    rows = md.load_history(session, days=5)

    assert len(rows) == 5
    assert rows[0].trade_date < rows[-1].trade_date
    assert md.latest(session).trade_date == rows[-1].trade_date


def test_temperature_and_phase_are_finite(session, monkeypatch) -> None:
    seed_stocks(session)
    seed_bars(session)
    monkeypatch.setattr(md, "index_rows", lambda *a, **k: {})
    md.backfill_from_bars(session, days=5)

    for row in md.load_history(session, days=5):
        assert row.temperature is not None and not math.isnan(row.temperature)
        assert 0 <= row.temperature <= 100
