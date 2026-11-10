"""Daily post-close maintenance: update bars, then run discovery.

Schedule this at 15:35 on trading days (Windows Task Scheduler / cron):
    .venv\\python.exe scripts\\daily_update.py

It is safe to re-run: bars are upserted and candidates for the day are replaced.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.db.base import SessionLocal  # noqa: E402
from app.db.models import Stock  # noqa: E402
from app.providers.base import is_main_board  # noqa: E402
from app.services.bars import incremental_bars  # noqa: E402
from app.services.calendar import is_trading_day  # noqa: E402
from app.services.discovery import run_discovery  # noqa: E402
from app.services.universe import sync_universe  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="盘后日线更新 + 全市场发现")
    parser.add_argument("--date", type=str, default="", help="指定交易日 YYYY-MM-DD（默认今天）")
    parser.add_argument("--skip-bars", action="store_true", help="跳过日线增量")
    parser.add_argument("--skip-universe", action="store_true", help="跳过股票列表同步")
    parser.add_argument("--force", action="store_true", help="非交易日也执行")
    args = parser.parse_args()

    target = date.fromisoformat(args.date) if args.date else date.today()

    with SessionLocal() as session:
        if not args.force and not is_trading_day(session, target):
            print(f"{target} 不是交易日，跳过。")
            return 0

        if not args.skip_universe:
            summary = sync_universe(session)
            print(f"股票池：新增 {summary['inserted']}，更新 {summary['updated']}")

        if not args.skip_bars:
            stocks = [
                stock
                for stock in session.scalars(select(Stock).where(Stock.status == "normal"))
                if is_main_board(stock.code)
            ]
            stats = incremental_bars(session, [stock.code for stock in stocks])
            print(
                f"日线增量：写入 {stats['written']} 行，失败 {len(stats['failed'])} 只，"
                f"耗时 {stats.get('elapsed_sec', 0)}s"
            )

        output = run_discovery(session, today=target)
        summary = output["summary"]
        print(
            f"发现完成：{summary['codes']} 只 -> {len(output['candidates'])} 只候选，"
            f"用时 {summary['total_sec']}s"
        )
        for item in output["candidates"][:10]:
            print(f"  {item['rank']:>2}. {item['code']} {item['name']} {item['score']:.1f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
