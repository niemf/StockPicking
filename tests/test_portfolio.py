"""Portfolio service tests: watchlist CRUD, holdings, transactions, cost basis.

No network: every stock used here is pre-inserted into the in-memory database, so
``_ensure_stock`` never falls back to the quote API.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import Holding, HoldingTransaction, Stock, TradePlan, Watchlist
from app.services.portfolio import (
    PortfolioError,
    add_holding,
    add_watch,
    holding_items,
    record_transaction,
    remove_holding,
    remove_watch,
    update_holding,
    watch_items,
)


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
        db_session.add_all(
            [
                Stock(code="600036", name="招商银行", exchange="sh", board="沪市主板", industry="银行"),
                Stock(code="000001", name="平安银行", exchange="sz", board="深市主板", industry="银行"),
                Stock(code="600519", name="贵州茅台", exchange="sh", board="沪市主板", industry="白酒"),
            ]
        )
        db_session.commit()
        yield db_session


def test_add_watch_normalizes_and_lists(session) -> None:
    row = add_watch(session, "sh600036", note="观察")
    assert row.code == "600036"

    items = watch_items(session)
    assert len(items) == 1
    assert items[0]["code"] == "600036"
    assert items[0]["has_plan"] is False
    assert items[0]["rating"] == "观察"
    assert items[0]["source"] == "手动添加"


def test_add_watch_rejects_duplicate_and_non_main_board(session) -> None:
    add_watch(session, "600036")

    with pytest.raises(PortfolioError, match="已在自选"):
        add_watch(session, "600036")
    with pytest.raises(PortfolioError, match="主板"):
        add_watch(session, "300750")
    with pytest.raises(PortfolioError, match="格式"):
        add_watch(session, "abc")


def test_add_watch_without_stock_row_uses_quote_fallback(session, monkeypatch) -> None:
    """A missing master-list row must not block the write (quote name is optional)."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr("app.services.quotes.QuoteService.get_quote", _boom)

    row = add_watch(session, "601318")
    assert row.name == "601318"  # falls back to the code, still committed
    assert session.get(Stock, "601318").name == "601318"


def test_remove_watch(session) -> None:
    row = add_watch(session, "600036")
    remove_watch(session, row.id)

    assert session.scalar(select(func.count()).select_from(Watchlist)) == 0

    with pytest.raises(PortfolioError):
        remove_watch(session, row.id)


def test_add_holding_validates_inputs(session) -> None:
    with pytest.raises(PortfolioError, match="100"):
        add_holding(session, code="600036", cost_price=38.0, shares=150, stop_loss=36.0)
    with pytest.raises(PortfolioError, match="止损"):
        add_holding(session, code="600036", cost_price=38.0, shares=100, stop_loss=39.0)
    with pytest.raises(PortfolioError, match="止盈"):
        add_holding(
            session, code="600036", cost_price=38.0, shares=100, stop_loss=36.0, take_profit_1=37.0
        )


def test_add_holding_creates_plan_and_initial_transaction(session) -> None:
    holding = add_holding(
        session,
        code="600036",
        cost_price=38.2,
        shares=1000,
        stop_loss=36.5,
        take_profit_1=42.0,
        open_date=date(2026, 9, 15),
    )

    assert holding.current_stop == pytest.approx(36.5)
    assert holding.plan_id is not None
    plan = session.get(TradePlan, holding.plan_id)
    assert plan is not None and plan.status == "confirmed"

    transactions = list(session.scalars(select(HoldingTransaction)))
    assert len(transactions) == 1
    assert transactions[0].action == "buy"
    assert transactions[0].shares == 1000

    items = holding_items(session)
    assert len(items) == 1
    assert items[0]["plan_id"] == plan.plan_no
    assert items[0]["take_profit_1"] == pytest.approx(42.0)


def test_add_holding_rejects_duplicate_open_position(session) -> None:
    add_holding(session, code="600036", cost_price=38.2, shares=1000, stop_loss=36.5)
    with pytest.raises(PortfolioError, match="已有持仓"):
        add_holding(session, code="600036", cost_price=39.0, shares=100, stop_loss=37.0)


def test_update_holding_stop_syncs_plan(session) -> None:
    holding = add_holding(session, code="600036", cost_price=38.2, shares=1000, stop_loss=36.5)

    update_holding(session, holding.id, current_stop=37.8)
    assert holding.current_stop == pytest.approx(37.8)
    assert session.get(TradePlan, holding.plan_id).stop_loss == pytest.approx(37.8)

    with pytest.raises(PortfolioError, match="低于成本价"):
        update_holding(session, holding.id, current_stop=40.0)


def test_record_transaction_buy_weights_cost(session) -> None:
    holding = add_holding(session, code="600036", cost_price=38.0, shares=1000, stop_loss=36.0)

    record_transaction(session, holding.id, action="buy", price=40.0, shares=1000, fee=5.0)

    assert holding.shares == 2000
    assert holding.available_shares == 2000
    # (38*1000 + 40*1000 + 5) / 2000
    assert holding.cost_price == pytest.approx(39.0025)


def test_record_transaction_sell_realizes_pnl_and_closes(session) -> None:
    holding = add_holding(session, code="600036", cost_price=38.0, shares=1000, stop_loss=36.0)

    record_transaction(session, holding.id, action="sell", price=41.0, shares=400, fee=2.0)
    assert holding.shares == 600
    assert holding.available_shares == 600
    assert holding.realized_pnl == pytest.approx((41.0 - 38.0) * 400 - 2.0)

    record_transaction(session, holding.id, action="sell", price=42.0, shares=600)
    assert holding.shares == 0
    assert holding.status == "closed"
    assert holding.closed_at is not None
    assert holding_items(session) == []


def test_record_transaction_rejects_oversell(session) -> None:
    holding = add_holding(session, code="600036", cost_price=38.0, shares=1000, stop_loss=36.0)

    with pytest.raises(PortfolioError, match="可卖数量不足"):
        record_transaction(session, holding.id, action="sell", price=40.0, shares=1200)
    with pytest.raises(PortfolioError, match="buy 或 sell"):
        record_transaction(session, holding.id, action="hold", price=40.0, shares=100)


def test_remove_holding_cascades_transactions(session) -> None:
    holding = add_holding(session, code="600036", cost_price=38.0, shares=1000, stop_loss=36.0)
    assert session.scalar(select(func.count()).select_from(HoldingTransaction)) == 1

    remove_holding(session, holding.id)
    assert session.scalar(select(func.count()).select_from(Holding)) == 0
    assert session.scalar(select(func.count()).select_from(HoldingTransaction)) == 0
