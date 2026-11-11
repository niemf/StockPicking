"""Database model tests. No network, no real database file."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    AlertEvent,
    DailyBar,
    Holding,
    HoldingTransaction,
    JobRun,
    Stock,
    TradePlan,
    Watchlist,
)
from app.db.seed import seed_demo_data

EXPECTED_TABLES = {
    "stock",
    "daily_bar",
    "quote",
    "indicator_daily",
    "candidate",
    "watchlist",
    "holding",
    "holding_transaction",
    "trade_plan",
    "alert_rule",
    "alert_event",
    "notification_log",
    "ai_call_log",
    "review_report",
    "stock_report",
    "market_regime",
    "trade_calendar",
    "job_run",
}


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
        yield db_session


def test_all_tables_created(session) -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_daily_bar_composite_primary_key(session) -> None:
    session.add(DailyBar(code="600036", trade_date=date(2026, 11, 10), open=1, high=2, low=1, close=2))
    session.commit()

    session.add(DailyBar(code="600036", trade_date=date(2026, 11, 10), open=1, high=2, low=1, close=2))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_watchlist_code_is_unique(session) -> None:
    session.add(Watchlist(code="600036", name="招商银行"))
    session.commit()

    session.add(Watchlist(code="600036", name="招商银行"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_alert_event_dedup_key_is_unique(session) -> None:
    key = "600036:stop_loss:2026-11-10"
    session.add(AlertEvent(code="600036", trigger_ts=datetime.now(), dedup_key=key))
    session.commit()

    session.add(AlertEvent(code="600036", trigger_ts=datetime.now(), dedup_key=key))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_job_run_slot_is_unique(session) -> None:
    slot = datetime(2026, 11, 10, 9, 0)
    session.add(JobRun(job_name="pre_market", trade_date=date(2026, 11, 10), scheduled_at=slot))
    session.commit()

    session.add(JobRun(job_name="pre_market", trade_date=date(2026, 11, 10), scheduled_at=slot))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_json_fields_roundtrip(session) -> None:
    plan = TradePlan(
        plan_no="PLAN-TEST-01",
        code="600036",
        name="招商银行",
        trade_date=date(2026, 11, 10),
        catalysts=["降息", "财富管理恢复"],
        risks=["房地产风险"],
        invalid_conditions=["收盘跌破 39.80"],
        status="draft",
    )
    session.add(plan)
    session.commit()
    session.expire_all()

    stored = session.scalar(select(TradePlan).where(TradePlan.code == "600036"))
    assert stored is not None
    assert stored.catalysts == ["降息", "财富管理恢复"]
    assert stored.risks == ["房地产风险"]
    assert stored.invalid_conditions == ["收盘跌破 39.80"]


def test_holding_transactions_cascade(session) -> None:
    holding = Holding(code="600036", name="招商银行", cost_price=38.2, shares=1000)
    session.add(holding)
    session.flush()
    session.add_all(
        [
            HoldingTransaction(
                holding_id=holding.id, trade_date=date(2026, 9, 15), action="buy", price=38.2, shares=1000
            ),
            HoldingTransaction(
                holding_id=holding.id, trade_date=date(2026, 10, 15), action="sell", price=41.0, shares=200
            ),
        ]
    )
    session.commit()

    assert session.scalar(select(func.count()).select_from(HoldingTransaction)) == 2

    session.delete(holding)
    session.commit()

    assert session.scalar(select(func.count()).select_from(HoldingTransaction)) == 0


def test_seed_demo_data_is_idempotent(session) -> None:
    first = seed_demo_data(session)
    assert first["watchlist"] == 6
    assert first["holding"] == 4
    assert first["trade_plan"] == 10

    second = seed_demo_data(session)
    assert second == {"stock": 0, "trade_plan": 0, "watchlist": 0, "holding": 0}

    watch = session.scalar(select(Watchlist).where(Watchlist.code == "600036"))
    assert watch is not None
    assert watch.plan is not None
    assert watch.plan.status == "confirmed"
    # 招商银行 is both a watchlist item and a holding; both share one stock row.
    assert session.get(Stock, "600036") is not None
