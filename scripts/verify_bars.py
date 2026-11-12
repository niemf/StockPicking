"""Audit the daily-bar table: coverage, staleness, gaps and impossible moves.

Examples:
    .venv\\python.exe scripts\\verify_bars.py
    .venv\\python.exe scripts\\verify_bars.py --days 10 --limit 30
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.db.base import SessionLocal  # noqa: E402
from app.db.models import DailyBar, IndicatorDaily, Stock, WeeklyBar  # noqa: E402
from app.providers.base import is_main_board  # noqa: E402
from app.services.universe import open_dates  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="日线 / 指标表体检")
    parser.add_argument("--days", type=int, default=10, help="检查最近 N 个交易日")
    parser.add_argument("--limit", type=int, default=20, help="最多列出 N 只异常股票")
    args = parser.parse_args()

    with SessionLocal() as session:
        total = session.scalar(select(func.count()).select_from(DailyBar))
        codes = session.scalar(select(func.count(func.distinct(DailyBar.code))))
        first = session.scalar(select(func.min(DailyBar.trade_date)))
        latest = session.scalar(select(func.max(DailyBar.trade_date)))
        print(f"日线：{total} 行 / {codes} 只，{first} → {latest}")

        calendar = open_dates(session)
        window = [day for day in calendar if latest and day <= latest][-args.days :]
        counts = dict(
            session.execute(
                select(DailyBar.trade_date, func.count())
                .where(DailyBar.trade_date.in_(window))
                .group_by(DailyBar.trade_date)
            ).all()
        )
        print(f"\n最近 {len(window)} 个交易日覆盖：")
        for day in window:
            print(f"  {day}  {counts.get(day, 0):>5} 只")

        rows = session.execute(
            select(DailyBar.code, func.max(DailyBar.trade_date), func.count()).group_by(DailyBar.code)
        ).all()
        lagging = [row for row in rows if row[1] != latest]
        print(f"\n最新日期不是 {latest} 的股票：{len(lagging)} 只（停牌或缺失）")
        for code, last_date, bars in lagging[: args.limit]:
            stock = session.get(Stock, code)
            name = stock.name if stock else ""
            print(f"  {code} {name:<8} 最后一根 {last_date}，共 {bars} 根")

        short = [row for row in rows if row[2] < 60]
        print(f"\n不足 60 根日线的股票：{len(short)} 只")
        for code, last_date, bars in short[: args.limit]:
            print(f"  {code} 只有 {bars} 根（最后一根 {last_date}）")

        # A main-board stock cannot fall more than ~10% in one session (ST 5%);
        # anything larger means the series was stitched from two adjustments.
        history = session.execute(
            select(DailyBar.code, DailyBar.trade_date, DailyBar.close)
            .where(DailyBar.trade_date >= (first or latest))
            .order_by(DailyBar.code, DailyBar.trade_date)
        ).all()
        frame = pd.DataFrame(history, columns=["code", "trade_date", "close"])
        frame["ret"] = frame.groupby("code")["close"].pct_change() * 100
        suspects = frame[frame["ret"].abs() > 10.6]
        print(f"\n单日涨跌幅超过 ±10.6% 的行：{len(suspects)}（次新、复牌/重组首日或复权衔接）")
        for _, row in suspects.head(args.limit).iterrows():
            print(f"  {row['code']} {row['trade_date']} {row['ret']:+.1f}% close={row['close']}")

        latest_indicator = session.scalar(select(func.max(IndicatorDaily.trade_date)))
        indicator_rows = session.scalar(select(func.count()).select_from(IndicatorDaily))
        weekly_rows = session.scalar(select(func.count()).select_from(WeeklyBar))
        weekly_latest = session.scalar(select(func.max(WeeklyBar.week_end)))
        print(
            f"\n指标表：indicator_daily {indicator_rows} 行（最新 {latest_indicator}），"
            f"weekly_bar {weekly_rows} 行（最新周 {weekly_latest}）"
        )
        if latest_indicator and latest and latest_indicator < latest:
            print(f"  ⚠ 指标表落后日线 {latest}，请运行 scripts\\build_indicators.py")

        main_board = [
            stock.code
            for stock in session.scalars(select(Stock).where(Stock.status == "normal"))
            if is_main_board(stock.code)
        ]
        missing = sorted(set(main_board) - {row[0] for row in rows})
        print(f"\n主板股票中没有日线的：{len(missing)} 只 {missing[: args.limit]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
