"""AI 基本面闸门：输出校验与当日缓存。"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import DailyBar, FundamentalReview, Stock
from app.services import fundamentals


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


class FakeClient:
    def __init__(self, payload: dict | None = None):
        self.payload = payload or {
            "verdict": "pass",
            "score": 78,
            "tags": ["机器人", "订单"],
            "reasons": ["新签订单"],
            "risks": [],
        }
        self.calls = 0

    def call(self, session, **kwargs):
        self.calls += 1
        return self.payload, SimpleNamespace(model="deepseek-v4.1-flash")


def test_validate_normalises_verdict_and_score() -> None:
    cleaned, notes = fundamentals.validate(
        {"verdict": "VETO", "score": 150, "tags": "解禁", "reasons": ["预亏"], "risks": []}
    )

    assert cleaned["verdict"] == "veto"
    assert cleaned["score"] == 100
    assert cleaned["tags"] == ["解禁"]
    assert cleaned["reasons"] == ["预亏"]


def test_validate_falls_back_to_watch() -> None:
    cleaned, notes = fundamentals.validate({"verdict": "buy", "score": "abc"})

    assert cleaned["verdict"] == "watch"
    assert cleaned["score"] == 0
    assert notes


def test_analyze_caches_once_per_day(session, monkeypatch) -> None:
    monkeypatch.setattr(fundamentals, "get_announcements", lambda code, **kwargs: {"entries": []})
    session.add(Stock(code="600000", name="测试股份", exchange="sh", board="沪市主板", status="normal"))
    session.add(
        DailyBar(
            code="600000",
            trade_date=date(2026, 11, 18),
            open=10,
            high=10.5,
            low=9.9,
            close=10.2,
            volume=1_000_000,
            amount=10_200_000,
        )
    )
    session.commit()

    client = FakeClient()
    row, cached = fundamentals.analyze(session, "600000", client=client)
    assert cached is False
    assert row.verdict == "pass"
    assert client.calls == 1

    again, cached_again = fundamentals.analyze(session, "600000", client=client)
    assert cached_again is True
    assert again.id == row.id
    assert client.calls == 1
    assert session.scalar(select(func.count()).select_from(FundamentalReview)) == 1


def test_analyze_force_refreshes(session, monkeypatch) -> None:
    monkeypatch.setattr(fundamentals, "get_announcements", lambda code, **kwargs: {"entries": []})
    session.add(Stock(code="600000", name="测试股份", exchange="sh", board="沪市主板", status="normal"))
    session.commit()

    client = FakeClient()
    fundamentals.analyze(session, "600000", client=client)
    client.payload = {"verdict": "veto", "score": 20, "tags": [], "reasons": [], "risks": ["解禁"]}
    row, cached = fundamentals.analyze(session, "600000", client=client, force=True)

    assert cached is False
    assert row.verdict == "veto"
    assert row.risks == ["解禁"]
    assert session.scalar(select(func.count()).select_from(FundamentalReview)) == 1


def test_latest_map_returns_newest_per_code(session, monkeypatch) -> None:
    monkeypatch.setattr(fundamentals, "get_announcements", lambda code, **kwargs: {"entries": []})
    session.add(Stock(code="600000", name="测试股份", exchange="sh", board="沪市主板", status="normal"))
    session.add(Stock(code="600001", name="另一只", exchange="sh", board="沪市主板", status="normal"))
    session.commit()

    client = FakeClient()
    fundamentals.analyze(session, "600000", client=client)
    result = fundamentals.latest_map(session, ["600000", "600001"])

    assert set(result) == {"600000"}
    assert result["600000"].verdict == "pass"
