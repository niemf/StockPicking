"""回测引擎：成交撮合、成本、止损/目标判定与指标汇总。"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app.analysis.risk import RiskPlan
from app.backtest import CodePanel, PanelContext, Trade, ROUND_TRIP_COST_PCT, simulate, summarise
from app.analysis.context import StockMeta

T = date(2026, 11, 2)


def make_panel(opens, highs, lows, closes, *, code="600000") -> CodePanel:
    dates = np.array([T + timedelta(days=index) for index in range(len(closes))])
    cols = {
        "open": np.array(opens, dtype="float64"),
        "high": np.array(highs, dtype="float64"),
        "low": np.array(lows, dtype="float64"),
        "close": np.array(closes, dtype="float64"),
        "pct_chg": np.zeros(len(closes)),
        "amount": np.ones(len(closes)),
        "turnover_rate": np.ones(len(closes)),
        "ma20": np.full(len(closes), 10.0),
        "ret20": np.full(len(closes), 5.0),
    }
    return CodePanel(
        code=code,
        meta=StockMeta(code=code, name="测试"),
        dates=dates,
        cols=cols,
    )


def plan(entry=10.0, stop=9.0, target=11.0) -> RiskPlan:
    return RiskPlan(
        entry=entry,
        stop=stop,
        target1=target,
        target2=target * 1.05,
        rr=(target - entry) / (entry - stop),
        stop_pct=(entry - stop) / entry * 100,
        upside_pct=(target / entry - 1) * 100,
        resistance=target,
        atr=0.3,
        atr_pct=3.0,
    )


def test_context_reads_features_with_offset() -> None:
    panel = make_panel([10, 10.1, 10.2], [10.1, 10.2, 10.3], [9.9, 10.0, 10.1], [10, 10.1, 10.2])
    panel.cols["ma5"] = np.array([9.0, 9.5, 10.0])

    ctx = PanelContext(panel, 2)

    assert ctx.price == pytest.approx(10.2)
    assert ctx.feature("ma5") == pytest.approx(10.0)
    assert ctx.feature("ma5", 1) == pytest.approx(9.5)
    assert ctx.feature("ma5", 5) is None
    assert ctx.feature("missing") is None
    assert ctx.quote.high == pytest.approx(10.3)


def test_simulate_hits_target() -> None:
    panel = make_panel(
        opens=[10.0, 10.0, 10.4, 10.8],
        highs=[10.2, 10.5, 11.2, 11.0],
        lows=[9.8, 9.9, 10.3, 10.7],
        closes=[10.0, 10.3, 11.1, 10.9],
    )

    trade = simulate(panel, 0, plan())

    assert trade is not None
    assert trade.reason == "target"
    assert trade.exit_price == pytest.approx(11.0)
    assert trade.days == 2
    assert trade.ret_pct == pytest.approx(10.0 - ROUND_TRIP_COST_PCT, abs=0.01)
    assert trade.r_multiple == pytest.approx(0.974, abs=0.01)


def test_simulate_stops_out() -> None:
    panel = make_panel(
        opens=[10.0, 10.0, 9.6],
        highs=[10.2, 10.1, 9.7],
        lows=[9.8, 9.4, 8.8],
        closes=[10.0, 9.5, 8.9],
    )

    trade = simulate(panel, 0, plan())

    assert trade is not None
    assert trade.reason == "stop"
    assert trade.exit_price == pytest.approx(9.0)
    assert trade.r_multiple == pytest.approx(-1.026, abs=0.01)


def test_simulate_prefers_stop_on_same_day() -> None:
    panel = make_panel(
        opens=[10.0, 10.0],
        highs=[10.2, 11.5],
        lows=[9.8, 8.5],
        closes=[10.0, 9.0],
    )

    trade = simulate(panel, 0, plan())

    assert trade is not None
    assert trade.reason == "stop"


def test_simulate_skips_gap_above_target() -> None:
    panel = make_panel(
        opens=[10.0, 11.5],
        highs=[10.2, 11.6],
        lows=[9.8, 11.3],
        closes=[10.0, 11.4],
    )

    assert simulate(panel, 0, plan()) is None


def test_simulate_skips_gap_below_stop() -> None:
    panel = make_panel(
        opens=[10.0, 8.8],
        highs=[10.2, 8.9],
        lows=[9.8, 8.6],
        closes=[10.0, 8.7],
    )

    assert simulate(panel, 0, plan()) is None


def test_simulate_times_out_at_close() -> None:
    panel = make_panel(
        opens=[10.0] + [10.2] * 4,
        highs=[10.2] + [10.4] * 4,
        lows=[9.9] + [10.0] * 4,
        closes=[10.0, 10.2, 10.3, 10.25, 10.35],
    )

    trade = simulate(panel, 0, plan(), max_hold=3)

    assert trade is not None
    assert trade.reason == "timeout"
    assert trade.days == 3
    assert trade.exit_date == panel.dates[3]


def test_summarise_computes_expectancy_and_drawdown() -> None:
    winning = Trade("600000", "A", T, T, 10.0, 9.0, 11.0)
    winning.exit_date = T + timedelta(days=2)
    winning.exit_price = 11.0
    winning.reason = "target"
    winning.days = 2
    winning.r_multiple = 2.0
    winning.ret_pct = 10.0

    losing = Trade("600001", "B", T, T, 10.0, 9.0, 11.0)
    losing.exit_date = T + timedelta(days=3)
    losing.exit_price = 9.0
    losing.reason = "stop"
    losing.days = 3
    losing.r_multiple = -1.0
    losing.ret_pct = -10.0

    summary = summarise([winning, losing])

    assert summary["trades"] == 2
    assert summary["win_rate"] == pytest.approx(50.0)
    assert summary["expectancy_r"] == pytest.approx(0.5)
    assert summary["profit_factor"] == pytest.approx(2.0)
    assert summary["max_drawdown_r"] == pytest.approx(1.0)
    assert summary["avg_days"] == pytest.approx(2.5)
    assert summary["stop_rate"] == pytest.approx(50.0)
    assert summary["target_rate"] == pytest.approx(50.0)


def test_forward_return_uses_close_to_close() -> None:
    from app.backtest import forward_return

    panel = make_panel(
        opens=[10.0] * 6,
        highs=[10.1] * 6,
        lows=[9.9] * 6,
        closes=[10.0, 10.1, 10.2, 10.3, 10.4, 11.0],
    )

    assert forward_return(panel, 0, 5) == pytest.approx(10.0)
    assert forward_return(panel, 2, 5) is None


def test_summarise_handles_empty() -> None:
    assert summarise([]) == {"trades": 0}
