"""板块资金分时采样：入库、节流与曲线数据。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import SectorFlowSample
from app.services import sector_flow

DAY = date(2026, 11, 20)


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


def payload(rows: list[tuple[str, float]]) -> dict:
    return {
        "available": True,
        "rows": [
            {"name": name, "net_inflow": value * 1e8, "net_inflow_yi": value, "pct_chg": 1.0}
            for name, value in rows
        ],
    }


def test_sample_writes_rows_and_throttles(session, monkeypatch) -> None:
    calls = {"count": 0}

    def fake_flow(day, refresh=False):
        calls["count"] += 1
        return payload([("半导体", -86.5), ("元件", 20.0)])

    monkeypatch.setattr(sector_flow, "get_sector_flow", fake_flow)
    monkeypatch.setattr(sector_flow, "is_polling_time", lambda moment=None: True)

    moment = datetime(2026, 11, 20, 10, 0)
    assert sector_flow.sample(session, moment=moment) == 2
    assert session.scalar(select(func.count()).select_from(SectorFlowSample)) == 2

    # 同一分钟再采：节流，不请求
    assert sector_flow.sample(session, moment=moment + timedelta(minutes=1)) == 0
    assert calls["count"] == 1

    # 五分钟后允许再采
    assert sector_flow.sample(session, moment=moment + timedelta(minutes=6)) == 2
    assert calls["count"] == 2


def test_sample_skips_outside_session(session, monkeypatch) -> None:
    monkeypatch.setattr(sector_flow, "is_polling_time", lambda moment=None: False)
    assert sector_flow.sample(session, moment=datetime(2026, 11, 20, 20, 0)) == 0


def test_sample_skips_when_unavailable(session, monkeypatch) -> None:
    monkeypatch.setattr(
        sector_flow, "get_sector_flow", lambda day, refresh=False: {"available": False, "rows": []}
    )
    monkeypatch.setattr(sector_flow, "is_polling_time", lambda moment=None: True)

    assert sector_flow.sample(session, moment=datetime(2026, 11, 20, 10, 0)) == 0


def test_intraday_series_picks_top_and_bottom(session) -> None:
    base = datetime(2026, 11, 20, 9, 35)
    rows = [
        ("半导体", -80.0, -86.0),
        ("元件", 12.0, 20.0),
        ("银行", 5.0, 3.0),
        ("电池", -30.0, -40.0),
        ("通信", -10.0, -5.0),
    ]
    for name, first, second in rows:
        session.add(
            SectorFlowSample(trade_date=DAY, ts=base, sector=name, net_inflow=first, pct_chg=1.0)
        )
        session.add(
            SectorFlowSample(
                trade_date=DAY,
                ts=base + timedelta(minutes=5),
                sector=name,
                net_inflow=second,
                pct_chg=1.0,
            )
        )
    session.commit()

    series = sector_flow.intraday_series(session, DAY, top=2, bottom=2)

    assert series["available"] is True
    assert series["times"] == ["09:35", "09:40"]
    assert [item["name"] for item in series["inflow"]] == ["元件", "银行"]
    assert [item["name"] for item in series["outflow"]] == ["半导体", "电池"]
    assert series["series"][0]["name"] == "元件"
    assert series["series"][0]["data"] == [12.0, 20.0]
    assert series["samples"] == 2


def test_intraday_series_empty(session) -> None:
    result = sector_flow.intraday_series(session, DAY)
    assert result["available"] is False
    assert result["times"] == []


def test_prune_removes_old_days(session, monkeypatch) -> None:
    session.add(
        SectorFlowSample(
            trade_date=date.today() - timedelta(days=40),
            ts=datetime.now() - timedelta(days=40),
            sector="旧板块",
            net_inflow=1.0,
        )
    )
    session.add(
        SectorFlowSample(
            trade_date=date.today(),
            ts=datetime.now(),
            sector="新板块",
            net_inflow=2.0,
        )
    )
    session.commit()

    assert sector_flow.prune(session, keep_days=20) == 1
    assert session.scalar(select(func.count()).select_from(SectorFlowSample)) == 1
