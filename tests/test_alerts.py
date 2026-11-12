"""Alert rule generation, state machine, notification payloads and one tick."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    AlertEvent,
    AlertRule,
    NotificationLog,
    Quote,
    Stock,
    TradePlan,
    Watchlist,
)
from app.providers.base import Quote as QuoteModel
from app.services import alerts, monitor
from app.services.notifier import Notifier, build_feishu_payload
from app.services.portfolio import add_holding, add_watch

MOMENT = datetime(2026, 11, 10, 10, 0, 0)


def make_quote(code: str, price: float, pct: float = 0.0, name: str = "测试") -> QuoteModel:
    return QuoteModel(
        code=code,
        name=name,
        price=price,
        pre_close=price / (1 + pct / 100) if pct else price,
        open=price,
        high=price,
        low=price,
        volume=1000.0,
        amount=10000.0,
        pct_chg=pct,
        change=0.0,
        source="test",
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
                Stock(code="601988", name="中国银行", exchange="sh", board="沪市主板", industry="银行"),
            ]
        )
        db_session.commit()
        yield db_session


def _make_watch_with_plan(session, *, low=40.0, high=42.0) -> Watchlist:
    watch = add_watch(session, "601988")
    plan = TradePlan(
        plan_no="PLAN-TEST-WATCH",
        code="601988",
        name="中国银行",
        source="manual",
        trade_date=MOMENT.date(),
        buy_zone_low=low,
        buy_zone_high=high,
        stop_loss=38.0,
        take_profit_1=45.0,
        status="confirmed",
        confirmed_at=MOMENT,
    )
    session.add(plan)
    session.flush()
    watch.plan_id = plan.id
    session.commit()
    return watch


def test_ensure_rules_from_holding_and_watch(session) -> None:
    holding = add_holding(
        session,
        code="600036",
        cost_price=38.0,
        shares=1000,
        stop_loss=36.5,
        take_profit_1=42.0,
    )
    _make_watch_with_plan(session)

    summary = alerts.ensure_rules(session)
    # 2 holding rules (stop / tp1; trailing waits for a run-up) + 7 watch rules
    # (buy zone, price change, breakout, strong/weak intraday, buy signal, pullback).
    assert summary["total"] == 9
    rules = list(session.scalars(select(AlertRule)))
    assert {rule.rule_type for rule in rules} == {
        "STOP_LOSS",
        "TAKE_PROFIT_1",
        "PRICE_IN_BUY_ZONE",
        "PRICE_CHANGE",
        "VOLUME_BREAKOUT",
        "INTRADAY_STRONG",
        "INTRADAY_WEAK",
        "BUY_SIGNAL",
        "PULLBACK_BUY",
    }
    stop_rule = next(rule for rule in rules if rule.rule_type == "STOP_LOSS")
    assert stop_rule.target_type == "holding"
    assert stop_rule.target_id == holding.id
    assert stop_rule.params["stop"] == pytest.approx(36.5)

    # Re-running must not duplicate rules.
    alerts.ensure_rules(session)
    assert session.scalar(select(func.count()).select_from(AlertRule)) == len(rules)


def test_stop_loss_triggers_and_respects_confirm(session) -> None:
    add_holding(session, code="600036", cost_price=38.0, shares=1000, stop_loss=36.5)
    alerts.ensure_rules(session)

    events = alerts.evaluate(
        session, {"600036": make_quote("600036", 36.4)}, moment=MOMENT
    )
    # STOP_LOSS confirm_samples defaults to 1; TRAILING_STOP needs 2.
    assert [event.condition_snapshot["rule_type"] for event in events] == ["STOP_LOSS"]
    assert events[0].trigger_price == pytest.approx(36.4)

    # Same condition again: already disarmed, no duplicate event.
    assert alerts.evaluate(session, {"600036": make_quote("600036", 36.4)}, moment=MOMENT) == []


def test_cooldown_and_rearm(session) -> None:
    add_holding(session, code="600036", cost_price=38.0, shares=1000, stop_loss=36.5)
    alerts.ensure_rules(session)

    first = alerts.evaluate(session, {"600036": make_quote("600036", 36.4)}, moment=MOMENT)
    assert len(first) == 1

    # Price recovers above the stop + rearm buffer, so the rule re-arms.
    alerts.evaluate(session, {"600036": make_quote("600036", 37.2)}, moment=MOMENT)
    rule = session.scalar(select(AlertRule).where(AlertRule.rule_type == "STOP_LOSS"))
    assert rule.params["state"]["armed"] is True

    # Drop again inside the cooldown window: suppressed.
    assert alerts.evaluate(session, {"600036": make_quote("600036", 36.4)}, moment=MOMENT) == []

    # After the cooldown a new event is allowed and the dedup key advances.
    rule.cooldown_sec = 0
    session.commit()
    second = alerts.evaluate(session, {"600036": make_quote("600036", 36.4)}, moment=MOMENT)
    assert len(second) == 1
    assert first[0].dedup_key.endswith(":001")
    assert second[0].dedup_key.endswith(":002")
    assert first[0].dedup_key != second[0].dedup_key


def test_take_profit_and_buy_zone(session) -> None:
    add_holding(
        session, code="600036", cost_price=38.0, shares=1000, stop_loss=36.5, take_profit_1=42.0
    )
    _make_watch_with_plan(session, low=40.0, high=42.0)
    alerts.ensure_rules(session)

    quotes = {"600036": make_quote("600036", 42.5), "601988": make_quote("601988", 41.0)}
    assert alerts.evaluate(session, quotes, moment=MOMENT) == []  # confirm_samples=2

    again = alerts.evaluate(session, quotes, moment=MOMENT)
    types = {event.condition_snapshot["rule_type"] for event in again}
    assert "TAKE_PROFIT_1" in types
    assert "PRICE_IN_BUY_ZONE" in types


def test_trailing_stop_appears_after_a_run_up(session) -> None:
    holding = add_holding(
        session, code="600036", cost_price=38.0, shares=1000, stop_loss=36.5
    )
    assert alerts.ensure_rules(session)["total"] == 1

    holding.trailing_high = 45.0
    session.commit()
    summary = alerts.ensure_rules(session)
    assert summary["total"] == 2
    trailing = session.scalar(select(AlertRule).where(AlertRule.rule_type == "TRAILING_STOP"))
    assert trailing is not None
    # max(hard stop 36.5, 45 * 0.92) = 41.4
    assert trailing.params["stop"] == pytest.approx(36.5)


def test_abnormal_quote_is_discarded(session) -> None:
    add_holding(session, code="600036", cost_price=38.0, shares=1000, stop_loss=36.5)
    alerts.ensure_rules(session)

    assert alerts.evaluate(session, {"600036": make_quote("600036", 0.0)}, moment=MOMENT) == []
    assert (
        alerts.evaluate(session, {"600036": make_quote("600036", 36.0, pct=99.0)}, moment=MOMENT)
        == []
    )
    assert session.scalar(select(func.count()).select_from(AlertEvent)) == 0


def test_feishu_card_payload(session) -> None:
    add_holding(
        session,
        code="600036",
        cost_price=38.0,
        shares=1000,
        stop_loss=36.5,
        take_profit_1=42.0,
    )
    alerts.ensure_rules(session)
    event = alerts.evaluate(
        session, {"600036": make_quote("600036", 36.4, pct=-3.2, name="招商银行")}, moment=MOMENT
    )[0]

    payload = build_feishu_payload(event)
    card = payload["card"]
    assert payload["msg_type"] == "interactive"
    assert card["header"]["template"] == "red"
    assert "止损" in card["header"]["title"]["content"]
    assert "招商银行" in card["header"]["title"]["content"]
    body = str(card["elements"])
    assert "36.40" in body
    assert "不构成投资建议" in body


def test_notifier_sends_and_logs(session) -> None:
    add_holding(session, code="600036", cost_price=38.0, shares=1000, stop_loss=36.5)
    alerts.ensure_rules(session)
    event = alerts.evaluate(session, {"600036": make_quote("600036", 36.4)}, moment=MOMENT)[0]

    calls: list[tuple[str, dict]] = []

    def fake_transport(url: str, payload: dict) -> tuple[int, str]:
        calls.append((url, payload))
        return 200, '{"code": 0, "msg": "success"}'

    notifier = Notifier(webhook="https://example.test/hook", transport=fake_transport, sleep=lambda _: None)
    log = notifier.send_event(session, event)

    assert log.status == "success"
    assert event.status == "sent"
    assert event.notified_at is not None
    assert calls and calls[0][0] == "https://example.test/hook"
    assert session.scalar(select(func.count()).select_from(NotificationLog)) == 1


def test_notifier_failure_marks_event_failed(session) -> None:
    add_holding(session, code="600036", cost_price=38.0, shares=1000, stop_loss=36.5)
    alerts.ensure_rules(session)
    event = alerts.evaluate(session, {"600036": make_quote("600036", 36.4)}, moment=MOMENT)[0]

    notifier = Notifier(
        webhook="https://example.test/hook",
        transport=lambda _url, _payload: (500, "boom"),
        sleep=lambda _: None,
    )
    log = notifier.send_event(session, event, retries=2)

    assert log.status == "failed"
    assert event.status == "failed"
    logs = list(session.scalars(select(NotificationLog)))
    assert len(logs) == 2
    assert {entry.retry_count for entry in logs} == {0, 1}


def test_run_once_force_persists_quotes_and_events(session) -> None:
    add_holding(session, code="600036", cost_price=38.0, shares=1000, stop_loss=36.5)

    class FakeQuoteService:
        def get_quotes(self, codes):
            return {code: make_quote(code, 36.4, pct=-2.0) for code in codes}

    summary = monitor.run_once(
        session,
        quote_service=FakeQuoteService(),
        moment=MOMENT,
        force=True,
        dry_run=True,
    )

    assert summary["status"] == "ok"
    assert summary["quotes"] == 1
    assert summary["dry_run"] is True
    assert session.scalar(select(func.count()).select_from(Quote)) == 1
    event = session.scalar(select(AlertEvent))
    assert event is not None and event.status == "pending"
    assert summary["events"][0]["rule"] == "STOP_LOSS"


def test_run_once_skips_outside_session(session) -> None:
    add_holding(session, code="600036", cost_price=38.0, shares=1000, stop_loss=36.5)

    summary = monitor.run_once(session, moment=datetime(2026, 11, 10, 20, 0), force=False)
    assert summary["status"] == "skipped"
    assert summary["reason"] in {"outside_session", "non_trading_day"}
