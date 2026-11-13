"""择时/空仓纪律测试：不选股，只决定「持有一篮子 / 空仓」。

两组规则：

1. 情绪阶段（涨停家数/炸板率/昨日溢价推出来的冰点~退潮）；
2. 更朴素的趋势与宽度（上证均线、20 日动量、上涨家数占比均值、中位数均值）。

决策只用**前一交易日**已知的数据，避免未来函数；每次进出扣 0.1%。

    .venv\\python.exe scripts\\test_timing.py
    .venv\\python.exe scripts\\test_timing.py --start 2026-08-16
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.backtest import load_panels  # noqa: E402
from app.db.base import SessionLocal  # noqa: E402
from app.db.models import MarketDaily  # noqa: E402

SWITCH_COST = 0.001  # 每次进出合计 0.1%

PHASE_RULES = {
    "一直持有": {"ice", "repair", "ferment", "climax", "ebb"},
    "避开高潮+退潮": {"ice", "repair", "ferment"},
    "只在发酵/修复": {"ferment", "repair"},
    "只在发酵": {"ferment"},
    "只在冰点/修复": {"ice", "repair"},
    "反向（只在高潮）": {"climax"},
}


def stats(series: list[float], invested: int) -> dict:
    if not series:
        return {}
    values = np.array(series)
    equity = np.cumprod(1 + values)
    peak = np.maximum.accumulate(equity)
    drawdown = (equity / peak - 1).min()
    daily_mean = values.mean()
    daily_std = values.std(ddof=1) if len(values) > 1 else 0.0
    return {
        "total": (equity[-1] - 1) * 100,
        "annual": (equity[-1] ** (244 / len(values)) - 1) * 100,
        "max_dd": drawdown * 100,
        "sharpe": (daily_mean / daily_std * np.sqrt(244)) if daily_std else 0.0,
        "win": (values > 0).mean() * 100,
        "days": invested,
    }


def run_rule(
    sessions: list[date],
    daily: dict[date, float],
    decide,
) -> dict:
    returns: list[float] = []
    invested = 0
    held = False
    for index in range(1, len(sessions)):
        day = sessions[index]
        if day not in daily:
            continue
        want = bool(decide(sessions[index - 1]))
        value = daily[day] if want else 0.0
        if want:
            invested += 1
        if want != held:
            value -= SWITCH_COST
            held = want
        returns.append(value)
    return stats(returns, invested)


def main() -> int:
    parser = argparse.ArgumentParser(description="择时/空仓纪律测试")
    parser.add_argument("--start", type=str, default="")
    parser.add_argument("--end", type=str, default="")
    args = parser.parse_args()
    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None

    with SessionLocal() as session:
        panels, by_date = load_panels(session)
        rows = list(session.scalars(select(MarketDaily).order_by(MarketDaily.trade_date)))

    phases = {row.trade_date: row.phase for row in rows}
    frame = pd.DataFrame(
        {
            "date": [row.trade_date for row in rows],
            "close": [row.sh_close for row in rows],
            "breadth": [row.breadth for row in rows],
            "median": [row.median_pct for row in rows],
        }
    ).set_index("date")
    close = frame["close"].astype(float)
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    momentum20 = close / close.shift(20) - 1
    breadth_ma5 = frame["breadth"].astype(float).rolling(5).mean()
    median_ma3 = frame["median"].astype(float).rolling(3).mean()

    def flag(series, day, default=False):
        try:
            value = series.get(day)
        except AttributeError:
            value = None
        if value is None or (isinstance(value, float) and np.isnan(value)):
            return default
        return bool(value)

    sessions = sorted(
        day for day in by_date if (start is None or day >= start) and (end is None or day <= end)
    )
    daily: dict[date, float] = {}
    for index in range(1, len(sessions)):
        day, previous_day = sessions[index], sessions[index - 1]
        values = []
        for code in by_date[day]:
            panel = panels.get(code)
            if panel is None:
                continue
            idx = panel.index_of(day)
            if idx <= 0 or panel.dates[idx] != day:
                continue
            previous = panel.cols["close"][idx - 1]
            current = panel.cols["close"][idx]
            if previous > 0 and not np.isnan(previous) and not np.isnan(current):
                values.append(current / previous - 1)
        if values:
            daily[day] = float(np.mean(values))

    print(f"区间 {sessions[0]} ~ {sessions[-1]}，{len(daily)} 个交易日")
    print(f"{'rule':<26} {'total%':>8} {'annual%':>8} {'maxDD%':>8} {'sharpe':>7} {'win%':>6} {'invested':>9}")

    results: list[tuple[str, dict]] = []
    for name, allowed in PHASE_RULES.items():
        results.append(
            (
                name,
                run_rule(sessions, daily, lambda day, allowed=allowed: phases.get(day) in allowed),
            )
        )

    trend_rules = {
        "上证>MA20": lambda day: flag(close > ma20, day),
        "上证>MA60": lambda day: flag(close > ma60, day),
        "上证20日动量>0": lambda day: flag(momentum20 > 0, day),
        "宽度5日均>50%": lambda day: flag(breadth_ma5 > 50, day),
        "中位数3日均>0": lambda day: flag(median_ma3 > 0, day),
        "上证>MA20 且 宽度>45%": lambda day: flag(close > ma20, day) and flag(breadth_ma5 > 45, day),
        "上证<MA20（反向）": lambda day: not flag(close > ma20, day, default=True),
    }
    for name, decide in trend_rules.items():
        results.append((name, run_rule(sessions, daily, decide)))

    for name, result in results:
        if not result:
            continue
        print(
            f"{name:<26} {result['total']:>+8.1f} {result['annual']:>+8.1f} {result['max_dd']:>8.1f} "
            f"{result['sharpe']:>7.2f} {result['win']:>6.1f} {result['days']:>9}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
