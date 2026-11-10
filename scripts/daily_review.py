"""Generate the daily review report (deterministic + optional AI narrative).

Usage:
    .venv\\python.exe scripts\\daily_review.py
    .venv\\python.exe scripts\\daily_review.py --no-ai
    .venv\\python.exe scripts\\daily_review.py --date 2026-11-10 --force
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db.base import SessionLocal  # noqa: E402
from app.services.review import generate_review  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="生成每日复盘")
    parser.add_argument("--date", type=str, default="", help="YYYY-MM-DD，默认今天")
    parser.add_argument("--no-ai", action="store_true", help="只生成确定性摘要，不调用 AI")
    parser.add_argument("--force", action="store_true", help="覆盖已有复盘")
    args = parser.parse_args()

    target = date.fromisoformat(args.date) if args.date else date.today()
    with SessionLocal() as session:
        report = generate_review(
            session, trade_date=target, use_ai=not args.no_ai, force=args.force
        )
        print(f"{report.trade_date} 复盘已生成")
        print("市场：", report.market_summary)
        print("自选：", report.watch_summary)
        print("持仓：", report.holding_summary)
        summaries = (report.raw or {}).get("summaries", {})
        print("板块：", summaries.get("sector_summary"))
        print("龙虎榜：", summaries.get("lhb_summary"))
        print("操作：", summaries.get("operation_summary"))
        if report.ai_narrative:
            print("AI：", report.ai_narrative[:500])
        else:
            print("AI：未生成（未配置 key、预算不足或使用 --no-ai）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
