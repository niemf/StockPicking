"""情绪周期：指标聚合、阶段判定、仓位/RR 联动。"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.providers.base import Quote
from app.services.cycle import (
    DEFAULT_THRESHOLDS,
    PHASE_META,
    build_metrics,
    classify,
    describe,
    state_from_metrics,
    temperature,
)

DAY = date(2026, 11, 12)


def make_quote(
    code: str,
    *,
    price: float = 10.0,
    pct_chg: float = 1.0,
    high: float | None = None,
    limit_up: float = 11.0,
    amount: float = 1e8,
) -> Quote:
    return Quote(
        code=code,
        name=code,
        price=price,
        pre_close=price / (1 + pct_chg / 100) if pct_chg > -100 else price,
        open=price,
        high=high if high is not None else price,
        low=price * 0.99,
        volume=1_000_000.0,
        amount=amount,
        pct_chg=pct_chg,
        change=0.0,
        limit_up=limit_up,
        limit_down=0.0,
        ts=datetime(2026, 11, 12, 15, 0),
        source="test",
    )


def quotes_with(limit_ups: int, *, touched_extra: int = 0, total: int = 100, up: int = 60) -> dict:
    """A snapshot with ``limit_ups`` sealed boards and ``touched_extra`` fails."""
    quotes: dict[str, Quote] = {}
    for index in range(total):
        code = f"{index:06d}"
        if index < limit_ups:
            quotes[code] = make_quote(code, price=11.0, pct_chg=10.0, high=11.0)
        elif index < limit_ups + touched_extra:
            # Hit the limit intraday, closed below it -> 炸板
            quotes[code] = make_quote(code, price=10.5, pct_chg=5.0, high=11.0)
        else:
            pct = 1.0 if index < up else -1.0
            quotes[code] = make_quote(code, price=10.0, pct_chg=pct, high=10.2)
    return quotes


def test_break_rate_counts_touched_but_unsealed_boards() -> None:
    metrics = build_metrics(quotes_with(20, touched_extra=20, total=100))

    assert metrics["limit_up"] == 20
    assert metrics["touched"] == 40
    assert metrics["break_rate"] == pytest.approx(0.5)


def test_prev_premium_averages_yesterday_limit_ups() -> None:
    quotes = quotes_with(10, total=50)
    quotes["000001"] = make_quote("000001", price=10.0, pct_chg=-4.0, high=10.1)
    quotes["000002"] = make_quote("000002", price=10.0, pct_chg=6.0, high=10.1)

    metrics = build_metrics(quotes, prev_limit_codes={"000001", "000002"})

    assert metrics["prev_premium"] == pytest.approx(1.0)


def test_metrics_unavailable_when_no_quotes() -> None:
    metrics = build_metrics({})
    assert metrics["available"] is False


# ------------------------------------------------------------------ 阶段
@pytest.mark.parametrize(
    ("metrics", "expected"),
    [
        # 昨日打板资金大亏 -> 退潮，优先于一切
        ({"available": True, "limit_up": 80, "prev_premium": -3.0, "break_rate": 0.2, "max_streak": 4}, "ebb"),
        # 炸板率高 + 溢价明显为负 -> 退潮
        ({"available": True, "limit_up": 60, "prev_premium": -1.5, "break_rate": 0.55, "max_streak": 3}, "ebb"),
        # 只是小幅负溢价 + 高炸板率，不构成退潮，也不是发酵
        ({"available": True, "limit_up": 60, "prev_premium": -0.5, "break_rate": 0.55, "max_streak": 3}, "repair"),
        # 涨停家数腰斩 -> 退潮
        (
            {"available": True, "limit_up": 20, "limit_up_prev": 60, "prev_premium": 0.5,
             "break_rate": 0.2, "max_streak": 3},
            "ebb",
        ),
        # 涨停少、溢价不为正 -> 冰点
        ({"available": True, "limit_up": 15, "prev_premium": -0.5, "break_rate": 0.45, "max_streak": 2}, "ice"),
        # 高度 6 板 + 溢价高 -> 高潮
        ({"available": True, "limit_up": 60, "prev_premium": 3.0, "break_rate": 0.2, "max_streak": 6}, "climax"),
        # 涨停 100 家 -> 高潮
        ({"available": True, "limit_up": 100, "prev_premium": 3.0, "break_rate": 0.15, "max_streak": 4}, "climax"),
        # 涨停 60、溢价 2%、炸板率低 -> 发酵
        ({"available": True, "limit_up": 60, "prev_premium": 2.0, "break_rate": 0.2, "max_streak": 4}, "ferment"),
        # 其余 -> 修复
        ({"available": True, "limit_up": 40, "prev_premium": 0.5, "break_rate": 0.3, "max_streak": 3}, "repair"),
    ],
)
def test_classify_phases(metrics: dict, expected: str) -> None:
    assert classify(metrics) == expected


def test_classify_treats_a_limit_down_wave_as_ebb() -> None:
    # 涨停还有 36 家，但跌停 45 家、中位数 -1.0%：这是普跌，不是修复
    metrics = {
        "available": True,
        "limit_up": 36,
        "limit_down": 45,
        "prev_premium": 0.7,
        "break_rate": 0.42,
        "max_streak": 4,
        "breadth": 30.0,
        "median_pct": -1.0,
    }
    assert classify(metrics) == "ebb"


def test_classify_treats_weak_breadth_as_ebb() -> None:
    metrics = {
        "available": True,
        "limit_up": 50,
        "limit_down": 8,
        "prev_premium": 0.2,
        "break_rate": 0.3,
        "max_streak": 3,
        "breadth": 20.0,
        "median_pct": -1.8,
    }
    assert classify(metrics) == "ebb"


def test_thresholds_are_overridable() -> None:
    metrics = {"available": True, "limit_up": 40, "prev_premium": 2.0, "break_rate": 0.2, "max_streak": 3}
    assert classify(metrics) == "repair"
    assert classify(metrics, {"ferment_limit_up": 30}) == "ferment"


def test_state_carries_position_and_rr_floor() -> None:
    metrics = {"available": True, "limit_up": 100, "prev_premium": 4.0, "break_rate": 0.1, "max_streak": 7}
    state = state_from_metrics(metrics, trade_date=DAY)

    assert state.phase == "climax"
    assert state.label == "高潮"
    assert state.position_cap == PHASE_META["climax"]["position_cap"]
    assert state.rr_floor == PHASE_META["climax"]["rr_floor"]
    assert state.trade_date == DAY.isoformat()
    assert state.temperature > 50


def test_state_degrades_gracefully_without_data() -> None:
    state = state_from_metrics({"available": False, "reason": "no_quotes"}, trade_date=DAY)

    assert state.available is False
    assert state.phase == "repair"
    assert state.rr_floor == PHASE_META["repair"]["rr_floor"]
    assert "数据不可用" in state.advice


def test_temperature_is_bounded() -> None:
    assert temperature({"limit_up": 0, "prev_premium": -8, "break_rate": 0.9, "max_streak": 1}) >= 0
    assert temperature({"limit_up": 300, "prev_premium": 9, "break_rate": 0.0, "max_streak": 10}) <= 100


def test_describe_renders_percentages_and_counts() -> None:
    metrics = {
        "available": True,
        "limit_up": 62,
        "limit_down": 3,
        "break_rate": 0.25,
        "max_streak": 5,
        "prev_premium": 1.8,
    }
    text = describe(state_from_metrics(metrics, trade_date=DAY))

    assert "涨停 62" in text
    assert "炸板率 25%" in text
    assert "+1.8%" in text


def test_default_thresholds_cover_every_phase_boundary() -> None:
    assert set(DEFAULT_THRESHOLDS) >= {
        "ice_limit_up",
        "ebb_premium",
        "climax_limit_up",
        "ferment_limit_up",
    }
