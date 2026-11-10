"""Web form flow tests (stage B): add / update / delete through the HTTP layer.

The quote provider is never called: every code used here already exists in the
in-memory ``stock`` table, and these routes do not render quote-bearing pages.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base, get_db
from app.db.models import Holding, HoldingTransaction, Stock, Watchlist
from app.main import app


@pytest.fixture()
def client():
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
    session = factory()
    session.add_all(
        [
            Stock(code="600036", name="招商银行", exchange="sh", board="沪市主板", industry="银行"),
            Stock(code="601988", name="中国银行", exchange="sh", board="沪市主板", industry="银行"),
        ]
    )
    session.commit()

    def _override():
        yield session

    app.dependency_overrides[get_db] = _override
    with TestClient(app, follow_redirects=False) as test_client:
        test_client.db = session  # type: ignore[attr-defined]
        yield test_client
    app.dependency_overrides.clear()
    session.close()


def _location(response) -> str:
    return response.headers["location"]


def test_watchlist_add_and_duplicate(client) -> None:
    response = client.post("/watchlist/add", data={"code": "601988", "note": "测试"})
    assert response.status_code == 303
    assert "ok=" in _location(response)

    row = client.db.scalar(select(Watchlist).where(Watchlist.code == "601988"))
    assert row is not None and row.source == "manual" and row.note == "测试"

    duplicate = client.post("/watchlist/add", data={"code": "601988"})
    assert duplicate.status_code == 303
    assert "error=" in _location(duplicate)


def test_watchlist_add_rejects_non_main_board(client) -> None:
    response = client.post("/watchlist/add", data={"code": "300750"})
    assert response.status_code == 303
    assert "error=" in _location(response)
    assert client.db.scalar(select(func.count()).select_from(Watchlist)) == 0


def test_watchlist_delete(client) -> None:
    client.post("/watchlist/add", data={"code": "601988"})
    row = client.db.scalar(select(Watchlist).where(Watchlist.code == "601988"))

    response = client.post(f"/watchlist/{row.id}/delete")
    assert response.status_code == 303
    assert "ok=" in _location(response)
    assert client.db.get(Watchlist, row.id) is None


def test_holding_add_update_stop_and_delete(client) -> None:
    created = client.post(
        "/holdings/add",
        data={
            "code": "601988",
            "cost_price": "4.5",
            "shares": "100",
            "stop_loss": "4.2",
            "take_profit_1": "5.2",
            "take_profit_2": "",
        },
    )
    assert created.status_code == 303
    holding = client.db.scalar(select(Holding).where(Holding.code == "601988"))
    assert holding is not None
    assert holding.shares == 100
    assert client.db.scalar(select(func.count()).select_from(HoldingTransaction)) == 1

    updated = client.post(f"/holdings/{holding.id}/stop", data={"current_stop": "4.4"})
    assert updated.status_code == 303
    assert holding.current_stop == pytest.approx(4.4)

    bad_stop = client.post(f"/holdings/{holding.id}/stop", data={"current_stop": "9.9"})
    assert "error=" in _location(bad_stop)

    deleted = client.post(f"/holdings/{holding.id}/delete")
    assert deleted.status_code == 303
    assert client.db.get(Holding, holding.id) is None


def test_holding_add_rejects_bad_share_lot(client) -> None:
    response = client.post(
        "/holdings/add",
        data={"code": "600036", "cost_price": "38", "shares": "150", "stop_loss": "36"},
    )
    assert response.status_code == 303
    assert "error=" in _location(response)
    assert client.db.scalar(select(func.count()).select_from(Holding)) == 0
