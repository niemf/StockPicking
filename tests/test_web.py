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
from app.db.models import AlertRule, Holding, HoldingTransaction, Stock, Watchlist
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


def test_watchlist_import_export_and_inline_update(client) -> None:
    imported = client.post(
        "/watchlist/import",
        data={"codes": "600036,银行,招行,低波;红利\n601988", "group_name": "自选"},
    )
    assert imported.status_code == 303
    assert "ok=" in _location(imported)

    rows = {row.code: row for row in client.db.scalars(select(Watchlist))}
    assert rows["600036"].group_name == "银行"
    assert rows["600036"].tags == ["低波", "红利"]
    assert rows["601988"].group_name == "自选"

    export = client.get("/watchlist/export.csv")
    assert export.status_code == 200
    text = export.content.decode("utf-8-sig")
    assert "code,name,group,note,tags" in text
    assert "600036,招商银行,银行,招行,低波;红利" in text

    updated = client.post(
        f"/watchlist/{rows['601988'].id}/update",
        data={"group_name": "核心", "tags": "大金融", "note": "观察"},
    )
    assert updated.status_code == 303
    refreshed = client.db.get(Watchlist, rows["601988"].id)
    assert refreshed.group_name == "核心"
    assert refreshed.tags == ["大金融"]
    assert refreshed.note == "观察"


def test_alerts_custom_rule_create_and_delete(client) -> None:
    client.post("/watchlist/add", data={"code": "600036"})

    created = client.post(
        "/alerts/rules",
        data={
            "code": "600036",
            "logic": "OR",
            "metric_a": "pct_chg",
            "op_a": ">=",
            "value_a": "5",
            "metric_b": "amount_yi",
            "op_b": ">=",
            "value_b": "2",
            "channel": "both",
            "max_per_day": "3",
            "expires_on": "",
        },
    )
    assert created.status_code == 303
    assert "error=" not in _location(created)

    rule = client.db.scalar(select(AlertRule).where(AlertRule.rule_type == "COMPOSITE"))
    assert rule is not None
    assert rule.params["logic"] == "OR"
    assert rule.params["channel"] == "both"
    assert rule.params["max_per_day"] == 3
    assert len(rule.params["conditions"]) == 2

    deleted = client.post(f"/alerts/rules/{rule.id}/delete")
    assert deleted.status_code == 303
    assert client.db.get(AlertRule, rule.id) is None
