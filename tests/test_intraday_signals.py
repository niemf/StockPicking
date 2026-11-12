"""Watchlist intraday signals: VWAP, breakout, strength, buy signal, pullback."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import AlertEvent, AlertRule, Stock
from app.providers.base import Quote as QuoteModel
from app.services import alerts, intraday
from app.services.portfolio import add_watch

MOMENT = datetime(2026, 11, 10, 10, 0, 0)


def make_quote(
    code: str = "600036",
    price: float = 41.0,
    pct: float = 2.0,
    *,
    pre_close: float | None = None,
    high: float | None = None,
    low: float | None = None,
    volume: float = 1_000_000.0,
    amount: float = 40_500_000.0,
    limit_up: float = 0.0,
) -> QuoteModel:
    return QuoteModel(
        code=code,
        name="招商银行",
        price=price,
        pre_close=pre_close if pre_close is not None else price / (1 + pct / 100),
        open=price,
        high=high if high is not None else price,
        low=low if low is not None else price,
        volume=volume,
        amount=amount,
        pct_chg=pct,
        change=0.0,
        limit_up=limit_up,
        source="test",
    )


def stats(**overrides) -> dict:
    base = {
        "available": True,
        "volume_ratio": 2.4,
        "high20_prev": 40.5,
        "ma20": 39.0,
        "ma20_slope3": 0.8,
        "rsi12": 58.0,
        "score": 7,
        "verdict": "看多",
        "resistance": 44.0,
        "resistance_dist": 7.3,
    }
    base.update(overrides)
    return base


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


# ------------------------------------------------------------------ stats math
def test_enrich_computes_vwap_limit_distance_and_amplitude() -> None:
    quote = make_quote(price=41.0, pre_close=40.0, high=41.5, low=40.2, limit_up=44.0)
    data = intraday.enrich({"600036": {"available": True}}, {"600036": quote})

    item = data["600036"]
    assert item["vwap"] == pytest.approx(40.5, abs=0.01)
    assert item["vwap_dist"] == pytest.approx((41.0 / 40.5 - 1) * 100, abs=0.01)
    assert item["limit_dist"] == pytest.approx((44.0 / 41.0 - 1) * 100, abs=0.01)
    assert item["amplitude"] == pytest.approx((41.5 - 40.2) / 40.0 * 100, abs=0.01)


def test_build_stats_without_codes_is_empty(session) -> None:
    assert intraday.build_stats(session, [], {}) == {}


def test_enrich_skips_unavailable_and_missing_quotes() -> None:
    data = intraday.enrich(
        {"600036": {"available": False}, "601988": {"available": True}},
        {"600036": make_quote()},
    )
    assert "vwap" not in data["600036"]
    assert "vwap" not in data["601988"]


# ------------------------------------------------------------------ triggers
def test_volume_breakout_needs_volume_price_and_positive_day() -> None:
    quote = make_quote(price=41.0, pct=2.0)
    assert alerts.volume_breakout_triggered({}, quote, stats()) is True

    # Price still below the 20-day high beyond the tolerance.
    assert alerts.volume_breakout_triggered({}, make_quote(price=39.5, pct=1.0), stats()) is False
    # Volume too small.
    assert alerts.volume_breakout_triggered({}, quote, stats(volume_ratio=1.3)) is False
    # Down day is not a breakout.
    assert alerts.volume_breakout_triggered({}, make_quote(price=41.0, pct=-2.0), stats()) is False
    # No stats (fresh database) must not fire.
    assert alerts.volume_breakout_triggered({}, quote, None) is False


def test_intraday_strength_uses_vwap() -> None:
    strong = make_quote(price=41.0, pct=2.0, volume=1_000_000.0, amount=40_000_000.0)
    # VWAP 40.0 -> +2.5%
    assert alerts.intraday_triggered({}, strong, stats(), side="strong") is True
    assert alerts.intraday_triggered({}, strong, stats(), side="weak") is False

    weak = make_quote(price=39.0, pct=-2.0, volume=1_000_000.0, amount=40_000_000.0)
    assert alerts.intraday_triggered({}, weak, stats(), side="weak") is True
    assert alerts.intraday_triggered({}, weak, stats(), side="strong") is False

    flat = make_quote(price=40.1, pct=0.2, volume=1_000_000.0, amount=40_000_000.0)
    assert alerts.intraday_triggered({}, flat, stats(), side="strong") is False


def test_intraday_needs_volume_confirmation() -> None:
    quote = make_quote(price=41.0, pct=2.0, volume=1_000_000.0, amount=40_000_000.0)
    quiet = stats(volume_ratio=1.0)
    assert alerts.intraday_triggered({}, quote, quiet, side="strong") is False


def test_buy_signal_requires_score_verdict_room_and_rsi() -> None:
    quote = make_quote(price=41.0, pct=2.0)
    assert alerts.buy_signal_triggered({}, quote, stats()) is True

    assert alerts.buy_signal_triggered({}, quote, stats(score=4)) is False
    assert alerts.buy_signal_triggered({}, quote, stats(verdict="中性")) is False
    assert alerts.buy_signal_triggered({}, quote, stats(rsi12=78.0)) is False
    assert alerts.buy_signal_triggered({}, quote, stats(resistance_dist=1.0)) is False
    # A sharp down day is a pullback question, not a breakout buy.
    assert alerts.buy_signal_triggered({}, make_quote(price=41.0, pct=-4.0), stats()) is False
    assert alerts.buy_signal_triggered({}, quote, stats(available=False)) is False
    assert alerts.buy_signal_triggered({}, quote, None) is False


def test_pullback_needs_flat_price_rising_ma_and_low_volume() -> None:
    quote = make_quote(price=39.2, pct=-0.5)  # MA20 39.0 -> +0.5%
    assert alerts.pullback_triggered({}, quote, stats(volume_ratio=0.7)) is True

    # Too far above MA20.
    assert alerts.pullback_triggered({}, make_quote(price=41.0, pct=1.0), stats(volume_ratio=0.7)) is False
    # MA20 falling.
    assert alerts.pullback_triggered({}, quote, stats(volume_ratio=0.7, ma20_slope3=-1.2)) is False
    # Volume expanding: that is a breakdown, not a pullback.
    assert alerts.pullback_triggered({}, quote, stats(volume_ratio=1.6)) is False


# ------------------------------------------------------------------ evaluation
def _isolate(session, rule_type: str) -> None:
    alerts.ensure_rules(session)
    for rule in list(session.scalars(select(AlertRule))):
        if rule.rule_type != rule_type:
            session.delete(rule)
    session.commit()


def test_buy_signal_triggers_after_confirmation_and_keeps_stats(session) -> None:
    add_watch(session, "600036")
    _isolate(session, "BUY_SIGNAL")

    quote = make_quote(price=41.0, pct=2.0)
    data = {"600036": stats()}

    assert alerts.evaluate(session, {"600036": quote}, moment=MOMENT, stats=data) == []

    events = alerts.evaluate(session, {"600036": quote}, moment=MOMENT, stats=data)
    assert len(events) == 1
    snapshot = events[0].condition_snapshot
    assert snapshot["rule_type"] == "BUY_SIGNAL"
    assert snapshot["stats"]["score"] == 7
    assert snapshot["stats"]["volume_ratio"] == pytest.approx(2.4)
    assert events[0].dedup_key.startswith("600036:BUY_SIGNAL:")

    # Still strong on the next tick: disarmed, no duplicate card.
    assert alerts.evaluate(session, {"600036": quote}, moment=MOMENT, stats=data) == []


def test_buy_signal_rearms_after_the_score_fades(session) -> None:
    add_watch(session, "600036")
    _isolate(session, "BUY_SIGNAL")
    quote = make_quote(price=41.0, pct=2.0)

    alerts.evaluate(session, {"600036": quote}, moment=MOMENT, stats={"600036": stats()})
    events = alerts.evaluate(session, {"600036": quote}, moment=MOMENT, stats={"600036": stats()})
    assert len(events) == 1

    # Score drops: rule re-arms without firing.
    faded = {"600036": stats(score=2, verdict="中性")}
    alerts.evaluate(session, {"600036": quote}, moment=MOMENT, stats=faded)

    rule = session.scalar(select(AlertRule).where(AlertRule.rule_type == "BUY_SIGNAL"))
    rule.cooldown_sec = 0
    session.commit()

    # Confirm samples are 2, so a re-armed rule needs two strong ticks again.
    again = alerts.evaluate(session, {"600036": quote}, moment=MOMENT, stats={"600036": stats()})
    assert again == []
    again = alerts.evaluate(session, {"600036": quote}, moment=MOMENT, stats={"600036": stats()})
    assert len(again) == 1
    assert again[0].id != events[0].id


def test_breakout_event_carries_threshold(session) -> None:
    add_watch(session, "600036")
    _isolate(session, "VOLUME_BREAKOUT")
    quote = make_quote(price=41.0, pct=2.0)
    data = {"600036": stats()}

    alerts.evaluate(session, {"600036": quote}, moment=MOMENT, stats=data)
    events = alerts.evaluate(session, {"600036": quote}, moment=MOMENT, stats=data)
    assert len(events) == 1
    assert events[0].condition_snapshot["threshold"] == pytest.approx(40.5)
    assert events[0].condition_snapshot["stats"]["high20_prev"] == pytest.approx(40.5)


def test_composite_metric_uses_stats(session) -> None:
    quote = make_quote(price=41.0, pct=2.0)
    triggered, detail = alerts.eval_composite(
        {"logic": "AND", "conditions": [{"metric": "volume_ratio", "op": ">=", "value": 2}]},
        quote,
        stats(),
    )
    assert triggered is True
    assert detail[0]["actual"] == pytest.approx(2.4)

    triggered, _ = alerts.eval_composite(
        {"logic": "AND", "conditions": [{"metric": "tech_score", "op": ">=", "value": 6}]},
        quote,
        stats(),
    )
    assert triggered is True

    # No stats -> metric unavailable -> never triggers.
    triggered, _ = alerts.eval_composite(
        {"logic": "AND", "conditions": [{"metric": "volume_ratio", "op": ">=", "value": 2}]},
        quote,
    )
    assert triggered is False


def test_builtin_rule_summary_lists_every_signal() -> None:
    labels = {item["label"] for item in alerts.builtin_rule_summary()}
    assert {"异动（大涨 / 大跌）", "放量突破", "分时走强 / 走弱", "买入信号", "回踩买点"} <= labels
    assert all(item["enabled"] for item in alerts.builtin_rule_summary())
