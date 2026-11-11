"""Report card: payload assembly, validation clamping, caching and persistence."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import AICallLog, DailyBar, Stock, StockReport
from app.providers.base import Quote
from app.services import report_card

TRADE_DATE = date(2026, 11, 10)


def make_bars(days: int = 60, start: float = 10.0) -> pd.DataFrame:
    rows = []
    for index in range(days):
        close = start + index * 0.05
        rows.append(
            {
                "trade_date": TRADE_DATE - timedelta(days=days - index),
                "open": close - 0.05,
                "high": close + 0.1,
                "low": close - 0.1,
                "close": close,
                "volume": 1_000_000 + index * 1000,
                "amount": 1e8,
                "turnover_rate": 1.5,
            }
        )
    return pd.DataFrame(rows)


def make_quote(price: float = 13.0) -> Quote:
    return Quote(
        code="600036",
        name="招商银行",
        price=price,
        pre_close=price / 1.01,
        open=price,
        high=price,
        low=price,
        volume=1e6,
        amount=2e8,
        pct_chg=1.0,
        change=0.1,
        turnover_rate=1.2,
        source="test",
    )


def good_output() -> dict:
    return {
        "conclusion": "看多",
        "score": 72,
        "trend": "上升",
        "trend_note": "均线多头排列，量能温和放大",
        "support": 12.2,
        "resistance": 14.5,
        "invalidation": 11.8,
        "summary": "价格站上 MA20，短期趋势向上，但仍需观察量能是否持续。",
        "risks": ["大盘系统性回调", "放量滞涨"],
        "catalysts": ["板块资金回流"],
        "checklist": ["回踩 12.2 附近分批", "跌破 11.8 减仓"],
    }


class FakeClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0

    def call(self, session, *, purpose, prompt_version, system, user, code=None, max_tokens=1200):
        self.calls += 1
        log = AICallLog(
            code=code,
            purpose=purpose,
            model="fake-model",
            prompt_version=prompt_version,
            input_tokens=100,
            output_tokens=50,
            status="success",
        )
        session.add(log)
        session.commit()
        return dict(self.payload), log


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
        db_session.add(
            Stock(code="601988", name="中国银行", exchange="sh", board="沪市主板", industry="银行")
        )
        for row in make_bars().to_dict("records"):
            db_session.add(DailyBar(code="600036", **row))
        for row in make_bars(days=5).to_dict("records"):
            db_session.add(DailyBar(code="601988", **row))
        db_session.commit()
        yield db_session


def test_validate_report_clamps_levels_and_enums() -> None:
    cleaned, notes = report_card.validate_report(
        {
            "conclusion": "买入",
            "score": 180,
            "trend": "上涨",
            "support": 15.0,  # above price -> must fall back
            "resistance": 12.0,  # below price -> must fall back
            "invalidation": 14.0,  # above support -> must drop below support
            "summary": "x" * 400,
            "risks": ["风险一", "风险一", "  "],
            "catalysts": "催化一；催化二",
            "checklist": [],
        },
        price=13.0,
        tech={"low20": 11.5, "high20_bars": 14.8},
    )

    assert cleaned["conclusion"] == "看多"
    assert cleaned["score"] == 100
    assert cleaned["trend"] == "上升"
    assert cleaned["support"] < 13.0
    assert cleaned["resistance"] > 13.0
    assert cleaned["invalidation"] < cleaned["support"]
    assert len(cleaned["summary"]) == 300
    assert cleaned["risks"] == ["风险一"]
    assert cleaned["catalysts"] == ["催化一", "催化二"]
    assert cleaned["checklist"]  # fallback item, never empty
    assert notes


def test_build_payload_requires_enough_bars(session) -> None:
    with pytest.raises(report_card.AIError):
        report_card.build_payload(session, "601988", quote=make_quote())


def test_generate_report_persists_caches_and_refreshes(session) -> None:
    client = FakeClient(good_output())
    row, cached, notes, log = report_card.generate_report(
        session, "600036", client=client, trade_date=TRADE_DATE, quote=make_quote()
    )
    assert cached is False
    assert notes == []
    assert log is not None
    assert row.conclusion == "看多"
    assert row.score == 72
    assert row.support < 13.0
    assert row.resistance > 13.0
    assert row.checklist == ["回踩 12.2 附近分批", "跌破 11.8 减仓"]
    assert client.calls == 1

    again, cached, _, log2 = report_card.generate_report(
        session, "600036", client=client, trade_date=TRADE_DATE, quote=make_quote()
    )
    assert cached is True
    assert log2 is None
    assert again.id == row.id
    assert client.calls == 1

    refreshed, cached, _, _ = report_card.generate_report(
        session, "600036", client=client, trade_date=TRADE_DATE, quote=make_quote(), force=True
    )
    assert cached is False
    assert refreshed.id == row.id
    assert client.calls == 2
    assert session.scalar(select(func.count()).select_from(StockReport)) == 1
    assert session.scalar(select(func.count()).select_from(AICallLog)) == 2

    latest = report_card.latest_report(session, "600036")
    assert latest is not None and latest.id == row.id
    items = report_card.recent_cards(session)
    assert items[0]["code"] == "600036"
    assert items[0]["score_label"] == "偏强"
