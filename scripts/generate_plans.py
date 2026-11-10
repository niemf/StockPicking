"""Generate AI trade plans for the latest top candidates.

Usage:
    .venv\\python.exe scripts\\generate_plans.py --limit 5
    .venv\\python.exe scripts\\generate_plans.py --limit 10 --force
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.db.base import SessionLocal  # noqa: E402
from app.services.ai import usage_summary  # noqa: E402
from app.services.plans import generate_plans  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="为候选池生成 AI 交易计划")
    parser.add_argument("--limit", type=int, default=settings.ai_max_plans)
    parser.add_argument("--force", action="store_true", help="忽略当日缓存，重新生成")
    parser.add_argument("--usage", action="store_true", help="只查看今日 AI 用量")
    args = parser.parse_args()

    with SessionLocal() as session:
        if args.usage:
            print(usage_summary(session))
            return 0

        result = generate_plans(session, limit=args.limit, force=args.force)
        print(result["message"])
        for plan in result["plans"]:
            print(
                f"  {plan.code} {plan.name}  {plan.status}  "
                f"买入 {plan.buy_zone_low}~{plan.buy_zone_high}  止损 {plan.stop_loss}  "
                f"止盈 {plan.take_profit_1}  仓位 {plan.position_pct}%"
            )
        for failure in result["failed"]:
            print(f"  失败 {failure['code']}: {failure['error']}")
        print(usage_summary(session))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
