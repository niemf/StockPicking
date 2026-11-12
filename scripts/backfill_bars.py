"""Backfill or incrementally update daily bars for the main-board universe.

Examples:
    .venv\\python.exe scripts\\backfill_bars.py --limit 300          # pilot
    .venv\\python.exe scripts\\backfill_bars.py                      # full backfill
    .venv\\python.exe scripts\\backfill_bars.py --incremental        # daily update
    .venv\\python.exe scripts\\backfill_bars.py --codes 600036,000001
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db.base import SessionLocal  # noqa: E402
from app.db.models import Stock  # noqa: E402
from app.providers.base import is_main_board  # noqa: E402
from app.services.bars import backfill_bars, incremental_bars  # noqa: E402
from app.services.universe import sync_universe  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="日线回补 / 增量更新")
    parser.add_argument("--days", type=int, default=None, help="回补天数（增量默认 12 天）")
    parser.add_argument("--workers", type=int, default=settings.bar_fetch_workers)
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 只（试点用）")
    parser.add_argument("--codes", type=str, default="", help="指定代码，逗号分隔")
    parser.add_argument("--force", action="store_true", help="忽略已有数据，强制重拉")
    parser.add_argument("--incremental", action="store_true", help="只更新最近几个交易日")
    parser.add_argument(
        "--fallback-first",
        action="store_true",
        help="跳过 AkShare，直接用腾讯/新浪（AkShare 被限流时快 10 倍）",
    )
    parser.add_argument("--sync-universe", action="store_true", help="先同步股票列表")
    args = parser.parse_args()

    with SessionLocal() as session:
        if args.sync_universe:
            summary = sync_universe(session)
            print(f"股票池：新增 {summary['inserted']}，更新 {summary['updated']}")

        if args.codes:
            codes = [code.strip() for code in args.codes.split(",") if code.strip()]
        else:
            stocks = [
                stock
                for stock in session.scalars(select(Stock).where(Stock.status == "normal"))
                if is_main_board(stock.code)
            ]
            codes = [stock.code for stock in stocks]

        if args.limit:
            codes = codes[: args.limit]

        print(f"准备处理 {len(codes)} 只股票（workers={args.workers}）")
        if args.incremental:
            stats = incremental_bars(
                session,
                codes,
                days=args.days or settings.bar_incremental_days,
                workers=args.workers,
                prefer_fallback=args.fallback_first,
            )
        else:
            stats = backfill_bars(
                session,
                codes,
                days=args.days or settings.bar_backfill_days,
                workers=args.workers,
                force=args.force,
                prefer_fallback=args.fallback_first,
            )

    print(
        f"完成：写入 {stats['written']} 行，失败 {len(stats['failed'])} 只，"
        f"耗时 {stats.get('elapsed_sec', 0)}s"
    )
    if stats["failed"]:
        print("失败代码（前 20）：", ",".join(stats["failed"][:20]))
    return 0 if not stats["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
