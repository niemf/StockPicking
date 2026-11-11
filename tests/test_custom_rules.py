"""Composite (user-defined) alert rules: validation, trigger, caps, channel."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import AlertEvent, AlertRule, Stock
from app.providers.base import Quote as QuoteModel
from app.services import alerts
from app.services.notifier import Notifier
from app.services.portfolio import add_watch

MOMENT = datetime(2026, 11, 10, 10, 0, 0)

COMPOSITE_PARAMS = {
    "logic": "AND",
    "conditions": [
        {"metric": "pct_chg", "op": ">=", "value": 5},
        {"metric": "amount_yi", "op": ">=", "value": 2},
    ],
}


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
        db_session.add(
            Stock(code="600036", name="招商银行", exchange="sh", board="沪市主板", industry="银行")
        )
        db_session.commit()
        yield db_session


def test_eval_composite_and_or() -> None:
    quote = make_quote("600036", 10.5, pct=6.0)
    quote.amount = 3e8

    triggered, detail = alerts.eval_composite(COMPOSITE_PARAMS, quote)
    assert triggered is True
    assert [item["ok"] for item in detail] == [True, True]

    only_or = {**COMPOSITE_PARAMS, "logic": "OR"}
    quote.amount = 1e7
    triggered, detail = alerts.eval_composite(only_or, quote)
    assert triggered is True
    assert [item["ok"] for item in detail] == [True, False]

    triggered, _ = alerts.eval_composite(COMPOSITE_PARAMS, quote)
    assert triggered is False


def test_normalize_conditions_validation() -> None:
    cleaned = alerts.normalize_conditions(
        [{"metric": "price", "op": "<=", "value": "9.5"}, {"metric": "", "value": ""}]
    )
    assert cleaned == [{"metric": "price", "op": "<=", "value": 9.5}]

    with pytest.raises(ValueError):
        alerts.normalize_conditions([])
    with pytest.raises(ValueError):
        alerts.normalize_conditions([{"metric": "pe", "op": ">=", "value": 10}])
    with pytest.raises(ValueError):
        alerts.normalize_conditions([{"metric": "pct_chg", "op": ">=", "value": 99}])


def test_requires_watchlist_membership(session) -> None:
    with pytest.raises(ValueError):
        alerts.upsert_custom_rule(session, "600036", [{"metric": "price", "op": ">=", "value": 10}])


def test_custom_rule_triggers_once_and_respects_daily_cap(session) -> None:
    add_watch(session, "600036")
    rule = alerts.upsert_custom_rule(
        session,
        "600036",
        [{"metric": "pct_chg", "op": ">=", "value": 5}],
        max_per_day=1,
    )
    assert rule.rule_type == "COMPOSITE"
    assert (rule.params or {}).get("auto") is False

    # A sync must not delete the user rule.
    alerts.ensure_rules(session)
    assert session.get(AlertRule, rule.id) is not None

    hot = make_quote("600036", 10.5, pct=6.0)
    events = alerts.evaluate(session, {"600036": hot}, moment=MOMENT)
    assert len(events) == 1
    assert events[0].condition_snapshot["channel"] == "feishu"
    assert events[0].condition_snapshot["conditions"][0]["ok"] is True

    # Recovery re-arms the rule, but the daily cap still blocks a second event.
    calm = make_quote("600036", 10.0, pct=1.0)
    alerts.evaluate(session, {"600036": calm}, moment=datetime(2026, 11, 10, 10, 30))
    again = alerts.evaluate(session, {"600036": hot}, moment=datetime(2026, 11, 10, 11, 0))
    assert again == []
    count = session.scalar(select(func.count()).select_from(AlertEvent))
    assert count == 1


def test_expired_custom_rule_is_ignored(session) -> None:
    add_watch(session, "600036")
    alerts.upsert_custom_rule(
        session,
        "600036",
        [{"metric": "pct_chg", "op": ">=", "value": 5}],
        expires_on=MOMENT.date().replace(day=1),
    )
    events = alerts.evaluate(
        session, {"600036": make_quote("600036", 10.5, pct=6.0)}, moment=MOMENT
    )
    assert events == []


def test_email_preferred_channel(session, monkeypatch) -> None:
    add_watch(session, "600036")
    event = AlertEvent(
        code="600036",
        trigger_ts=MOMENT,
        trigger_price=10.5,
        condition_snapshot={
            "rule_type": "COMPOSITE",
            "name": "招商银行",
            "price": 10.5,
            "channel": "email",
        },
        status="pending",
        dedup_key="600036:COMPOSITE:2026-11-10:001",
    )
    session.add(event)
    session.commit()

    notifier = Notifier(webhook="")
    monkeypatch.setattr(Notifier, "_send_email", lambda self, evt: True)
    log = notifier.send_event(session, event)
    assert log.channel == "email"
    assert log.status == "success"
    assert event.status == "sent"
