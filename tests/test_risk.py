"""Reward-risk gate, position-risk features and the new hard filters.

Everything here is synthetic: the point is to pin the arithmetic (stop, target,
RR) and the "do not buy an extended stock" rules, not to replay the market.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from app.analysis.context import StockContext, StockMeta
from app.analysis.factors import FILTERS
from app.analysis.features import compute_features
from app.analysis.risk import RiskPlan, build_plan, rr_bonus

TODAY = date(2026, 11, 10)
RISK_CFG = {
    "atr_mult": 1.2,
    "ma20_buffer": 0.01,
    "min_stop_pct": 1.5,
    "max_stop_pct": 8.0,
    "min_upside_pct": 3.0,
    "max_target_pct": 25.0,
    "min_reward_risk": 2.0,
    "target2_extension": 1.618,
    "rr_bonus_cap": 10.0,
}


def make_bars(
    closes: list[float],
    *,
    spread: float = 0.02,
    volumes: list[float] | None = None,
    highs: list[float] | None = None,
) -> pd.DataFrame:
    """Bars from a close series; ``spread`` is the intraday range as a fraction."""
    dates = pd.date_range(end=TODAY, periods=len(closes), freq="B")
    rows = []
    for index, close in enumerate(closes):
        high = highs[index] if highs else close * (1 + spread / 2)
        volume = volumes[index] if volumes else 1_000_000.0
        rows.append(
            {
                "trade_date": dates[index].date(),
                "open": close,
                "high": high,
                "low": close * (1 - spread / 2),
                "close": close,
                "volume": volume,
                "amount": close * volume,
                "turnover_rate": 3.0,
            }
        )
    return pd.DataFrame(rows)


def make_context(bars: pd.DataFrame, code: str = "600000") -> StockContext:
    return StockContext(
        meta=StockMeta(code=code, name="测试股份", industry="银行"),
        bars=bars,
        listed_days=500,
    )


def flat_then(closes: list[float], days: int = 90) -> list[float]:
    """A long flat base followed by the given tail, so MA20/MA60 exist."""
    return [100.0] * days + closes


def test_plan_is_built_for_a_quiet_stock_near_its_average() -> None:
    ctx = make_context(make_bars(flat_then([100.0] * 25)))

    plan, reason = build_plan(ctx, RISK_CFG)

    assert reason is None
    assert isinstance(plan, RiskPlan)
    assert plan.entry == pytest.approx(ctx.price)
    assert plan.stop < plan.entry < plan.target1
    assert plan.rr == pytest.approx(plan.upside_pct / plan.stop_pct, rel=1e-6)
    assert plan.rr >= RISK_CONFIG_MIN_RR


RISK_CONFIG_MIN_RR = RISK_CFG["min_reward_risk"]


def test_plan_rejects_an_extended_stock_with_a_wide_stop() -> None:
    # +2% a day for ten sessions leaves the price far above MA20, so the
    # structural stop (MA20) is more than max_stop_pct away.
    tail = [110.0 * (1.02**step) for step in range(10)]
    ctx = make_context(make_bars(flat_then(tail)))
    deviation = (ctx.price / ctx.feature("ma20") - 1) * 100

    plan, reason = build_plan(ctx, RISK_CFG)

    assert deviation > 8
    assert plan is None
    assert reason is not None and "止损过宽" in reason


def test_target_uses_the_nearest_real_level_above_the_price() -> None:
    # A prior high 6% above the close is the honest target; the measured move
    # would overstate the upside. The wick is placed far enough back that it
    # does not inflate today's ATR.
    closes = flat_then([100.0] * 20)
    highs = [close * 1.01 for close in closes]
    highs[-8] = 106.0
    ctx = make_context(make_bars(closes, highs=highs))

    plan, reason = build_plan(ctx, RISK_CFG)

    assert reason is None, reason
    assert plan is not None
    assert plan.target1 == pytest.approx(106.0)
    assert plan.upside_pct == pytest.approx(6.0, abs=0.1)


def test_target_can_be_set_by_reward_risk_multiple() -> None:
    ctx = make_context(make_bars(flat_then([100.0] * 25)))
    cfg = {**RISK_CFG, "target_mode": "rr", "target_rr": 1.5, "min_reward_risk": 1.0}

    plan, reason = build_plan(ctx, cfg)

    assert reason is None, reason
    assert plan is not None
    assert plan.rr == pytest.approx(1.5, rel=0.01)
    assert plan.upside_pct == pytest.approx(plan.stop_pct * 1.5, rel=0.01)


def test_target_can_be_set_by_atr_multiple() -> None:
    ctx = make_context(make_bars(flat_then([100.0] * 25)))
    atr_pct = ctx.feature("atr_pct")
    cfg = {**RISK_CFG, "target_mode": "atr", "target_atr_mult": 2.0, "min_reward_risk": 1.0}

    plan, reason = build_plan(ctx, cfg)

    assert reason is None, reason
    assert plan is not None
    assert plan.upside_pct == pytest.approx(atr_pct * 2.0, rel=0.01)


def test_level_mode_is_the_default() -> None:
    ctx = make_context(make_bars(flat_then([100.0] * 25)))
    assert build_plan(ctx, RISK_CFG)[0] is not None
    assert build_plan(ctx, {**RISK_CFG, "target_mode": "level"})[0] is not None


def test_plan_needs_volatility_data() -> None:
    ctx = make_context(make_bars([100.0] * 5))
    plan, reason = build_plan(ctx, RISK_CFG)
    assert plan is None
    assert reason == "缺少波动数据"


def test_rr_bonus_rewards_higher_reward_risk_and_is_capped() -> None:
    assert rr_bonus(1.0, RISK_CFG) == pytest.approx(0.0)
    assert rr_bonus(RISK_CFG["min_reward_risk"], RISK_CFG) == pytest.approx(0.0)
    assert rr_bonus(3.0, RISK_CFG) == pytest.approx(5.0)
    assert rr_bonus(9.0, RISK_CFG) == pytest.approx(RISK_CFG["rr_bonus_cap"])


# ------------------------------------------------------------------ features
def test_short_momentum_features() -> None:
    closes = flat_then([100.0] * 20 + [101.0, 102.0, 103.0, 104.0, 105.0])
    frame = compute_features(make_bars(closes))

    assert frame["up_streak"].iloc[-1] == 5
    assert frame["ret5"].iloc[-1] == pytest.approx((105.0 / 100.0 - 1) * 100, rel=0.02)


def test_upper_shadow_is_measured_against_the_body() -> None:
    closes = flat_then([100.0] * 25)
    highs = [close * 1.01 for close in closes]
    highs[-1] = 106.0
    frame = compute_features(make_bars(closes, highs=highs))

    assert frame["upper_shadow_pct"].iloc[-1] == pytest.approx(6.0, abs=0.01)
    assert frame["lower_shadow_pct"].iloc[-1] < 2.0


def test_resistance_distance_only_counts_levels_above_the_price() -> None:
    closes = flat_then([100.0] * 20 + [95.0])
    frame = compute_features(make_bars(closes, highs=[close * 1.0 for close in closes]))

    # The 20-day high is above the last close, so it is the resistance.
    assert frame["resistance_dist"].iloc[-1] == pytest.approx((100.0 / 95.0 - 1) * 100, rel=0.01)


# ------------------------------------------------------------------ filters
def test_extension_filter_rejects_a_stretched_stock() -> None:
    ctx = make_context(make_bars(flat_then([100.0] * 20 + [112.0])))
    assert FILTERS["extension"].fn(ctx, {"max_deviation_pct": 8.0}) is not None
    assert FILTERS["extension"].fn(ctx, {"max_deviation_pct": 15.0}) is None


def test_run_up_filter_rejects_short_term_spikes() -> None:
    ctx = make_context(make_bars(flat_then([100.0] * 20 + [104.0, 108.0, 112.0, 116.0, 121.0])))

    reason = FILTERS["run_up"].fn(ctx, {"max_ret5_pct": 20.0, "max_up_streak": 5})

    assert reason is not None
    assert "5 日涨幅" in reason or "连涨" in reason
    assert FILTERS["run_up"].fn(ctx, {"max_ret5_pct": 40.0, "max_up_streak": 8}) is None


def test_upper_shadow_filter_needs_volume_to_confirm_distribution() -> None:
    closes = flat_then([100.0] * 25)
    highs = [close * 1.01 for close in closes]
    highs[-1] = 106.0
    quiet = make_bars(closes, highs=highs)
    loud = quiet.copy()
    loud.loc[loud.index[-1], "volume"] = 10_000_000.0

    cfg = {"lookback": 3, "max_shadow_pct": 4.0, "min_volume_ratio": 1.5, "min_hits": 1}

    assert FILTERS["upper_shadow"].fn(make_context(quiet), cfg) is None
    assert FILTERS["upper_shadow"].fn(make_context(loud), cfg) is not None


def test_stagnation_filter_rejects_heavy_volume_closing_weak() -> None:
    closes = flat_then([100.0] * 25)
    bars = make_bars(closes)
    bars.loc[bars.index[-1], "open"] = 100.0
    bars.loc[bars.index[-1], "high"] = 104.0
    bars.loc[bars.index[-1], "low"] = 100.0
    bars.loc[bars.index[-1], "close"] = 100.5
    bars.loc[bars.index[-1], "volume"] = 6_000_000.0

    reason = FILTERS["stagnation"].fn(
        make_context(bars), {"min_volume_ratio": 2.5, "max_close_position": 0.5}
    )

    assert reason is not None and "下半区" in reason


def test_breakout_filter_can_cap_the_extension() -> None:
    closes = flat_then([100.0] * 20 + [108.0])
    ctx = make_context(make_bars(closes, highs=[close * 1.0 for close in closes]))
    # 价格已经比 20 日高点多出 8%，设置上限后应被拒绝
    assert FILTERS["breakout_20d"].fn(ctx, {"min_ratio": 0.90, "max_ratio": 1.02}) is not None
    assert FILTERS["breakout_20d"].fn(ctx, {"min_ratio": 0.90, "max_ratio": 1.20}) is None


def test_overhead_filter_rejects_a_stock_pinned_under_its_high() -> None:
    closes = flat_then([100.0] * 20 + [99.0])
    highs = [close * 1.0 for close in closes]
    highs[-2] = 100.5
    ctx = make_context(make_bars(closes, highs=highs))

    assert FILTERS["overhead"].fn(ctx, {"min_room_pct": 3.0}) is not None
    assert FILTERS["overhead"].fn(ctx, {"min_room_pct": 0.5}) is None
