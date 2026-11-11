"""Daily post-close maintenance: update today's bar, then run discovery.

Schedule this at 15:35 on trading days (Windows Task Scheduler / cron):
    .venv\\python.exe scripts\\daily_update.py

By default the day's bar is built from one batch of closing quotes (2–4 s)
instead of 3195 per-stock history requests. Run ``backfill_bars.py --force``
weekly to repair forward-adjustment drift around ex-dividend dates.
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
from app.services.bars import bars_from_snapshot, incremental_bars  # noqa: E402
from app.services.calendar import is_trading_day, market_session  # noqa: E402
from app.services.discovery import run_discovery  # noqa: E402
from app.services.industries import needs_sync, sync_industries  # noqa: E402
from app.services.limit_board import get_lhb_seats, get_limit_pool  # noqa: E402
from app.services.quotes import QuoteService  # noqa: E402
from app.services.universe import sync_universe  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="盘后日线更新 + 全市场发现")
    parser.add_argument("--date", type=str, default="", help="指定交易日 YYYY-MM-DD（默认今天）")
    parser.add_argument("--skip-bars", action="store_true", help="跳过日线更新")
    parser.add_argument(
        "--full-bars",
        action="store_true",
        help="逐只回补最近 12 天（较慢，除权修复用；日常不需要）",
    )
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

        if needs_sync():
            try:
                stats = sync_industries(session)
                print(f"行业分类：{stats['count']} 个代码，更新 {stats['updated']} 只")
            except Exception as exc:  # noqa: BLE001 - industry labels are optional
                print(f"行业分类同步失败：{exc}")

        stocks = [
            stock
            for stock in session.scalars(select(Stock).where(Stock.status == "normal"))
            if is_main_board(stock.code)
        ]
        codes = [stock.code for stock in stocks]
        quotes = None

        if not args.skip_bars:
            if args.full_bars:
                stats = incremental_bars(session, codes)
                print(
                    f"日线增量（逐只）：写入 {stats['written']} 行，失败 {len(stats['failed'])} 只，"
                    f"耗时 {stats.get('elapsed_sec', 0)}s"
                )
            elif market_session() != "after_close":
                print("尚未收盘，跳过快照 K 线写入（收盘后再运行）。")
            else:
                quotes = QuoteService().get_quotes(codes)
                written = bars_from_snapshot(session, quotes)
                session.commit()
                print(f"收盘快照 K 线：写入 {written} 只（未逐只请求）")

        output = run_discovery(session, quotes=quotes, today=target)
        summary = output["summary"]
        print(
            f"发现完成：{summary['codes']} 只 -> {len(output['candidates'])} 只候选，"
            f"用时 {summary['total_sec']}s"
        )
        for item in output["candidates"][:10]:
            print(f"  {item['rank']:>2}. {item['code']} {item['name']} {item['score']:.1f}")

        # Warm the pages that are expensive on first view (~15 s combined).
        try:
            pool = get_limit_pool(target, refresh=True)
            seats = get_lhb_seats(target, refresh=True)
            print(
                f"涨停池预热：{pool.get('total') or 0} 只，高度 {pool.get('max_streak') or 0}；"
                f"龙虎榜席位 {seats.get('count') or 0} 个"
            )
        except Exception as exc:  # noqa: BLE001 - warming must not fail the job
            print(f"涨停池预热失败：{exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
