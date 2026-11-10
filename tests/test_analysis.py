"""Indicator, factor and funnel tests built on synthetic bars (no network)."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from app.analysis.context import StockContext, StockMeta
from app.analysis.factors import FILTERS
from app.analysis.features import compute_features
from app.analysis.pipeline import load_profile, run_pipeline
from app.providers.base import Quote

TODAY = date(2026, 11, 10)


def make_bars(days: int = 120, last_volume: float = 180_000.0) -> pd.DataFrame:
    pattern = [0.8, -0.3, 0.5, -0.2, 0.9, -0.4]
    closes = [100.0]
    for index in range(1, days):
        if index < days - 40:
            closes.append(closes[-1] * (1 + (0.001 if index % 2 else -0.001)))
        else:
            closes.append(closes[-1] * (1 + pattern[(index - (days - 40)) % len(pattern)] / 100))

    rows = []
    for index, close in enumerate(closes):
        volume = 100_000.0 if index < days - 1 else last_volume
        rows.append(
            {
                "trade_date": TODAY,
                "open": close * 0.999,
                "high": close * 1.012,
                "low": close * 0.988,
                "close": close,
                "volume": volume,
                "amount": close * volume,
                "turnover_rate": 3.0,
            }
        )
    frame = pd.DataFrame(rows)
    dates = pd.date_range(end=TODAY, periods=days, freq="B")
    frame["trade_date"] = [timestamp.date() for timestamp in dates]
    return frame


def make_quote(price: float, **overrides) -> Quote:
    values = {
        "code": "600000",
        "name": "测试股份",
        "price": price,
        "pre_close": price * 0.995,
        "open": price * 0.998,
        "high": price * 1.01,
        "low": price * 0.99,
        "volume": 180_000.0,
        "amount": 200_000_000.0,
        "pct_chg": 0.5,
        "change": price * 0.005,
        "turnover_rate": 3.0,
        "ts": datetime(2026, 11, 10, 14, 55),
        "source": "test",
    }
    values.update(overrides)
    return Quote(**values)


def make_context(**overrides) -> StockContext:
    bars = overrides.pop("bars", None)
    if bars is None:
        bars = make_bars()
    quote = overrides.pop("quote", None)
    if quote is None:
        price = overrides.pop("price", float(bars["close"].iloc[-1]))
        quote = make_quote(price)
    else:
        overrides.pop("price", None)
    meta = overrides.pop(
        "meta",
        StockMeta(code="600000", name="测试股份", industry="银行", list_date=date(2020, 1, 1)),
    )
    context = StockContext(
        meta=meta,
        quote=quote,
        bars=bars,
        listed_days=overrides.pop("listed_days", 500),
        **overrides,
    )
    return context


def test_features_have_expected_values() -> None:
    bars = make_bars()
    frame = compute_features(bars)

    for column in ("ma5", "ma20", "ma60", "macd_hist", "rsi12", "atr_pct", "volume_ratio"):
        assert column in frame.columns
        assert pd.notna(frame[column].iloc[-1])

    assert frame["volume_ratio"].iloc[-1] == pytest.approx(1.8, rel=0.01)
    assert 40 <= frame["rsi12"].iloc[-1] <= 80
    assert 1.0 <= frame["atr_pct"].iloc[-1] <= 5.0
    assert frame["limit_up_streak"].iloc[-1] == 0


def test_context_uses_latest_quote_for_last_bar() -> None:
    bars = make_bars()
    price = float(bars["close"].iloc[-1]) * 1.01
    context = make_context(bars=bars, price=price, quote=make_quote(price))

    assert context.price == pytest.approx(price)
    assert context.feature("pct_chg") is not None


def test_full_pipeline_selects_a_healthy_trend_stock() -> None:
    context = make_context()

    def loader(codes):
        assert codes == ["600000"]
        return {"600000": context.bars}

    profile = load_profile()
    result = run_pipeline([context], profile, bars_loader=loader, today=TODAY)

    assert result.total_input == 1
    assert result.survivors == 1
    assert result.candidates, [stage.as_dict() for stage in result.stages]
    candidate = result.candidates[0]
    assert candidate["code"] == "600000"
    assert 0 < candidate["score"] <= 100
    assert candidate["indicator_snapshot"]["ma20"] is not None
    assert "MA20 向上" in candidate["rule_hits"]


def test_liquidity_rejects_before_loading_bars() -> None:
    context = make_context(quote=make_quote(100.0, amount=5_000_000.0))
    called = {"value": False}

    def loader(codes):
        called["value"] = True
        return {}

    result = run_pipeline([context], load_profile(), bars_loader=loader, today=TODAY)

    assert called["value"] is False
    assert result.survivors == 0
    assert result.stages[1].rejections.get("成交额不足") == 1


def test_st_stock_is_rejected_in_stage_one() -> None:
    meta = StockMeta(code="600000", name="ST 测试", is_st=True)
    context = make_context(meta=meta)
    result = run_pipeline([context], load_profile(), bars_loader=lambda codes: {}, today=TODAY)

    assert result.survivors == 0
    assert result.stages[0].rejections.get("ST") == 1


def test_sub_new_stock_is_rejected() -> None:
    context = make_context(listed_days=30)
    result = run_pipeline([context], load_profile(), bars_loader=lambda codes: {}, today=TODAY)

    assert result.stages[0].passed == 0
    assert any("次新" in reason for reason in result.stages[0].rejections)


def test_volume_spike_fails_volume_filter() -> None:
    bars = make_bars()
    price = float(bars["close"].iloc[-1])
    context = make_context(bars=bars, quote=make_quote(price, volume=600_000.0))
    result = run_pipeline([context], load_profile(), bars_loader=lambda codes: {"600000": context.bars})

    assert result.survivors == 0
    reasons = {key: value for stage in result.stages for key, value in stage.rejections.items()}
    assert reasons.get("量能过热") == 1


def test_filter_registry_is_extensible() -> None:
    assert "ma20_trend" in FILTERS
    assert FILTERS["ma20_trend"].label == "MA20 向上"
    assert FILTERS["ma20_trend"].requires == ("ma20", "ma20_slope3")


def test_profile_overrides_disable_a_filter() -> None:
    bars = make_bars()
    price = float(bars["close"].iloc[-1])
    context = make_context(bars=bars, quote=make_quote(price, volume=600_000.0))
    profile = load_profile(overrides={"filters": {"volume_ratio": {"enabled": False}}})

    result = run_pipeline(
        [context], profile, bars_loader=lambda codes: {"600000": context.bars}, today=TODAY
    )
    assert result.survivors == 1
