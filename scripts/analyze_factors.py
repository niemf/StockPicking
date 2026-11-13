"""横截面因子体检：每个指标与「未来 5 日收益」的秩相关（IC）。

    .venv\\python.exe scripts\\analyze_factors.py
    .venv\\python.exe scripts\\analyze_factors.py --start 2026-08-16
    .venv\\python.exe scripts\\analyze_factors.py --horizon 10

IC > 0 表示指标越大、未来越可能涨；IC < 0 表示反向。
这是设计策略之前的「找 alpha」步骤：先确认哪个指标有预测力，再写规则。
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

from app.backtest import forward_return, load_panels  # noqa: E402
from app.db.base import SessionLocal  # noqa: E402

FEATURES = [
    "ret5",
    "ret20",
    "dev20",
    "volume_ratio",
    "rsi12",
    "atr_pct",
    "macd_hist",
    "close_position",
    "upper_shadow_pct",
    "up_streak",
    "range20_pct",
    "resistance_dist",
    "ma20_slope3",
    "amount_yi",
    "turnover_rate",
]


def feature_value(panel, idx: int, name: str):
    if name == "dev20":
        ma20 = panel.cols["ma20"][idx]
        if not ma20 or np.isnan(ma20):
            return None
        return (panel.cols["close"][idx] / ma20 - 1) * 100
    if name == "amount_yi":
        return panel.cols["amount"][idx] / 1e8
    array = panel.cols.get(name)
    if array is None:
        return None
    value = array[idx]
    return None if np.isnan(value) else float(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="因子 IC 体检")
    parser.add_argument("--start", type=str, default="")
    parser.add_argument("--end", type=str, default="")
    parser.add_argument("--horizon", type=int, default=5)
    args = parser.parse_args()
    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None

    with SessionLocal() as session:
        panels, by_date = load_panels(session)

    sessions = sorted(
        day for day in by_date if (start is None or day >= start) and (end is None or day <= end)
    )
    records: dict[str, list[float]] = {name: [] for name in FEATURES}
    spreads: dict[str, list[float]] = {name: [] for name in FEATURES}
    samples = 0

    for day in sessions:
        rows = []
        for code in by_date[day]:
            panel = panels.get(code)
            if panel is None:
                continue
            idx = panel.index_of(day)
            if idx >= len(panel.dates) or panel.dates[idx] != day:
                continue
            fwd = forward_return(panel, idx, args.horizon)
            if fwd is None:
                continue
            values = {name: feature_value(panel, idx, name) for name in FEATURES}
            values["fwd"] = fwd
            rows.append(values)
        if len(rows) < 200:
            continue
        frame = pd.DataFrame(rows).dropna(subset=["fwd"])
        if frame.empty:
            continue
        samples += 1
        target = frame["fwd"].rank()
        for name in FEATURES:
            column = frame[name]
            mask = column.notna()
            if mask.sum() < 200:
                continue
            ic = column[mask].rank().corr(target[mask])
            if ic is not None and not np.isnan(ic):
                records[name].append(float(ic))
            try:
                deciles = pd.qcut(column[mask].rank(method="first"), 10, labels=False)
                top = frame.loc[mask][deciles == 9]["fwd"].mean()
                bottom = frame.loc[mask][deciles == 0]["fwd"].mean()
                spreads[name].append(float(top - bottom))
            except ValueError:
                continue

    print(f"样本 {samples} 个交易日 · 前瞻 {args.horizon} 日")
    print(f"{'factor':<18} {'n':>4} {'IC均值':>8} {'IC标准差':>9} {'IC>0':>7} {'多空价差':>9}")
    rows = []
    for name in FEATURES:
        values = records[name]
        if not values:
            continue
        rows.append(
            (
                name,
                len(values),
                float(np.mean(values)),
                float(np.std(values)) if len(values) > 1 else 0.0,
                100.0 * sum(1 for value in values if value > 0) / len(values),
                float(np.mean(spreads[name])) if spreads[name] else 0.0,
            )
        )
    rows.sort(key=lambda item: -abs(item[2]))
    for name, count, ic_mean, ic_std, positive, spread in rows:
        print(
            f"{name:<18} {count:>4} {ic_mean:>+8.3f} {ic_std:>9.3f} "
            f"{positive:>6.1f}% {spread:>+8.2f}%"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
