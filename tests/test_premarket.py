"""Pre-market briefing: sections, zone distance, text rendering and delivery."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import DailyBar, NotificationLog, ReviewReport, Stock, TradePlan, Watchlist
from app.services.notifier import Notifier
from app.services.premarket import build_briefing, render_text, send_briefing

PREV = date(2026, 11, 10)
TODAY = date(2026, 11, 11)  # Wednesday
SATURDAY = date(2026, 11, 14)


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
                Stock(code="601318", name="中国平安", exchange="sh", board="沪市主板", industry="保险"),
            ]
        )
        # Two bars per stock: the briefing must use the latest close only.
        for code, older, latest in (("600036", 41.0, 43.0), ("601318", 39.0, 40.0)):
            db_session.add(
                DailyBar(
                    code=code,
                    trade_date=date(2026, 11, 9),
                    open=older,
                    high=older,
                    low=older,
                    close=older,
                    volume=1e6,
                    amount=1e8,
                )
            )
            db_session.add(
                DailyBar(
                    code=code,
                    trade_date=PREV,
                    open=latest,
                    high=latest,
                    low=latest,
                    close=latest,
                    volume=1e6,
                    amount=1e8,
                )
            )

        plan_zone = TradePlan(
            plan_no="P-600036",
            code="600036",
            name="招商银行",
            trade_date=PREV,
            buy_zone_low=42.0,
            buy_zone_high=43.0,
            stop_loss=41.0,
            take_profit_1=46.0,
            status="confirmed",
        )
        plan_below = TradePlan(
            plan_no="P-601318",
            code="601318",
            name="中国平安",
            trade_date=PREV,
            buy_zone_low=41.0,
            buy_zone_high=42.0,
            stop_loss=39.5,
            take_profit_1=45.0,
            status="confirmed",
        )
        db_session.add_all([plan_zone, plan_below])
        db_session.flush()
        db_session.add_all(
            [
                Watchlist(code="600036", name="招商银行", source="manual", status="active", plan_id=plan_zone.id),
                Watchlist(code="601318", name="中国平安", source="manual", status="active", plan_id=plan_below.id),
            ]
        )

        db_session.add(
            ReviewReport(
                trade_date=PREV,
                market_summary="风格：防守（-3）。上证 -0.4%。",
                watch_summary="自选 2 只，进入建仓区 1 只。",
                holding_summary="持仓 1 只，浮动盈亏 +2.0%。",
                raw={
                    "summaries": {
                        "market_summary": "风格：防守（-3）。上证 -0.4%。",
                        "sector_summary": "资金流入前三：银行；流出前三：半导体。",
                        "lhb_summary": "龙虎榜 3 只，净买入靠前：招商银行。",
                        "holding_summary": "持仓 1 只，浮动盈亏 +2.0%。",
                    }
                },
            )
        )
        db_session.commit()
        yield db_session


def test_build_briefing_sections_and_levels(session) -> None:
    briefing = build_briefing(session, day=TODAY, holdings_enabled=False)

    assert briefing["trading_day"] is True
    assert briefing["previous_review"] == PREV.isoformat()
    assert [section["title"] for section in briefing["sections"]] == [
        f"昨日复盘（{PREV.isoformat()}）",
        "今日关注",
        "自选关键价位",
        "纪律提醒",
    ]

    review = briefing["sections"][0]["lines"]
    assert len(review) == 3
    assert "资金流入前三" in review[1]

    # 600036 sits on the upper edge of the buy zone; 601318 is 2.44% below it.
    codes = [item["code"] for item in briefing["focus"]]
    assert codes == ["600036", "601318"]
    assert briefing["focus"][0]["in_zone"] is True
    assert briefing["focus"][0]["price"] == pytest.approx(43.0)
    assert briefing["focus"][1]["zone_status"] == "低于建仓区"
    assert briefing["focus"][1]["distance"] == pytest.approx(-2.44, abs=0.01)

    levels = briefing["sections"][2]["lines"]
    assert any("建仓 42.00–43.00" in line for line in levels)
    assert any("止损 41.00" in line for line in levels)

    assert "盘前简报" in briefing["text"]
    assert "【昨日复盘" in briefing["text"]
    assert render_text("t", [{"title": "x", "lines": ["- y"]}]) == "t\n\n【x】\n- y"


def test_non_trading_day_skips_send(session) -> None:
    briefing = build_briefing(session, day=SATURDAY, holdings_enabled=False)
    assert briefing["trading_day"] is False

    result = send_briefing(session, briefing, force=False)
    assert result["sent"] is False
    assert result["reason"] == "non_trading_day"
    assert session.scalar(select(NotificationLog)) is None


def test_send_briefing_pushes_card_and_logs(session) -> None:
    sent: list[dict] = []

    def transport(url: str, payload: dict) -> tuple[int, str]:
        sent.append(payload)
        return 200, '{"code": 0, "msg": "ok"}'

    notifier = Notifier(webhook="https://example.invalid/hook", transport=transport, sleep=lambda _: None)
    result = send_briefing(session, notifier=notifier, day=TODAY)

    assert result["sent"] is True
    assert sent and sent[0]["msg_type"] == "interactive"
    assert "盘前简报" in sent[0]["card"]["header"]["title"]["content"]

    log = session.scalar(select(NotificationLog))
    assert log is not None
    assert log.event_id is None
    assert log.channel == "feishu"
    assert log.status == "success"


def test_send_briefing_reports_failure(session) -> None:
    notifier = Notifier(
        webhook="https://example.invalid/hook",
        transport=lambda url, payload: (500, "boom"),
        sleep=lambda _: None,
    )
    result = send_briefing(session, notifier=notifier, day=TODAY)
    assert result["sent"] is False
    assert result["status"] == "failed"

    log = session.scalar(select(NotificationLog))
    assert log is not None and log.status == "failed"
    assert log.retry_count == 1
