"""情绪周期择时测试：不选股，只决定「持有一篮子 / 空仓」。

    .venv\\python.exe scripts\\test_timing.py
    .venv\\python.exe scripts\\test_timing.py --start 2026-08-16

用前一日收盘算出的情绪阶段，决定今天是否持有等权全市场组合（避免未来函数），
对比「一直持有」的累计收益、最大回撤与夏普。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.backtest import load_panels  # noqa: E402
from app.db.base import SessionLocal  # noqa: E402
from app.db.models import MarketDaily  # noqa: E402

SWITCH_COST = 0.001  # 每次进出合计 0.1%

RULES = {
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


def main() -> int:
    parser = argparse.ArgumentParser(description="情绪周期择时测试")
    parser.add_argument("--start", type=str, default="")
    parser.add_argument("--end", type=str, default="")
    args = parser.parse_args()
    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None

    with SessionLocal() as session:
        panels, by_date = load_panels(session)
        phases = {row.trade_date: row.phase for row in session.scalars(select(MarketDaily))}

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
    print(f"{'rule':<16} {'total%':>8} {'annual%':>8} {'maxDD%':>8} {'sharpe':>7} {'win%':>6} {'invested':>9}")

    for name, allowed in RULES.items():
        returns: list[float] = []
        invested = 0
        held = False
        for index in range(1, len(sessions)):
            day = sessions[index]
            if day not in daily:
                continue
            phase = phases.get(sessions[index - 1])
            want = phase in allowed if phase else True
            value = daily[day] if want else 0.0
            if want:
                invested += 1
            if want != held:
                value -= SWITCH_COST
                held = want
            returns.append(value)
        result = stats(returns, invested)
        print(
            f"{name:<16} {result['total']:>+8.1f} {result['annual']:>+8.1f} {result['max_dd']:>8.1f} "
            f"{result['sharpe']:>7.2f} {result['win']:>6.1f} {result['days']:>9}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
