"""Build the persisted indicator tables (``indicator_daily`` / ``weekly_bar``).

Examples:
    .venv\\python.exe scripts\\build_indicators.py                 # 全市场，最近 260 根日线
    .venv\\python.exe scripts\\build_indicators.py --days 60       # 只保留最近 60 根
    .venv\\python.exe scripts\\build_indicators.py --codes 600000,000001
    .venv\\python.exe scripts\\build_indicators.py --limit 200     # 试点
    .venv\\python.exe scripts\\build_indicators.py --skip-weekly
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.db.base import SessionLocal  # noqa: E402
from app.db.models import Stock  # noqa: E402
from app.providers.base import is_main_board  # noqa: E402
from app.services.indicators import rebuild_daily, rebuild_weekly  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="重建日线 / 周线指标表")
    parser.add_argument("--days", type=int, default=260, help="每只股票保留的日线根数")
    parser.add_argument("--weeks", type=int, default=12, help="每只股票保留的周线根数")
    parser.add_argument("--codes", type=str, default="", help="指定代码，逗号分隔")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 只（试点用）")
    parser.add_argument("--skip-daily", action="store_true")
    parser.add_argument("--skip-weekly", action="store_true")
    args = parser.parse_args()

    with SessionLocal() as session:
        if args.codes:
            codes = [code.strip() for code in args.codes.split(",") if code.strip()]
        else:
            codes = [
                stock.code
                for stock in session.scalars(select(Stock).where(Stock.status == "normal"))
                if is_main_board(stock.code)
            ]
        if args.limit:
            codes = codes[: args.limit]

        print(f"准备处理 {len(codes)} 只股票")

        if not args.skip_daily:
            stats = rebuild_daily(session, codes, days=args.days)
            print(
                f"日线指标：{stats['rows']} 行（跳过 {stats['skipped']} 只），"
                f"耗时 {stats['elapsed_sec']}s"
            )

        if not args.skip_weekly:
            stats = rebuild_weekly(session, codes, keep_weeks=args.weeks)
            print(f"周线：{stats['rows']} 行（跳过 {stats['skipped']} 只）")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
