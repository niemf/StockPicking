"""回填市场级日线（market_daily）并结算历史信号。

Examples:
    .venv\\python.exe scripts\\backfill_market.py            # 最近 260 个交易日
    .venv\\python.exe scripts\\backfill_market.py --days 60
    .venv\\python.exe scripts\\backfill_market.py --skip-indices
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db.base import SessionLocal  # noqa: E402
from app.services.market_daily import backfill_from_bars, load_history, phase_counts  # noqa: E402
from app.services.signals import (  # noqa: E402
    backfill_from_candidates,
    stats,
    track,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="市场数据回填 + 信号结算")
    parser.add_argument("--days", type=int, default=260, help="回填最近 N 个交易日")
    parser.add_argument("--skip-indices", action="store_true", help="跳过指数日线")
    parser.add_argument("--skip-signals", action="store_true", help="跳过信号结算")
    args = parser.parse_args()

    with SessionLocal() as session:
        if args.skip_indices:
            import app.services.market_daily as md

            md.fetch_index_daily = lambda *a, **k: {}
        result = backfill_from_bars(session, days=args.days)
        print(f"市场数据：写入 {result['rows']} 天，耗时 {result['elapsed_sec']}s")

        rows = load_history(session, days=10)
        for row in rows[-5:]:
            print(
                f"  {row.trade_date} {row.phase:<8} 涨停 {row.limit_up:>3} 跌停 {row.limit_down:>2} "
                f"成交 {row.amount_yi} 亿 中位数 {row.median_pct}% 溢价 {row.prev_premium}"
            )
        print("阶段分布（近 120 天）：", phase_counts(session, 120))

        if not args.skip_signals:
            added = backfill_from_candidates(session, days=args.days)
            print(f"历史候选补录：{added} 条")
            tracked = track(session, as_of=date.today())
            print(f"信号结算：检查 {tracked['checked']} 条，结算 {tracked['resolved']} 条")
            summary = stats(session, days=60)
            print(
                f"近 60 天：样本 {summary.get('resolved')}，胜率 {summary.get('win_rate')}%，"
                f"平均 5 日 {summary.get('avg_t5')}%，期望 {summary.get('expectancy_r')}R"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
