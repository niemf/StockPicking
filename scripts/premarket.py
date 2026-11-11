"""Build and push the pre-market briefing.

Examples:
    .venv\\python.exe scripts\\premarket.py                 # print only
    .venv\\python.exe scripts\\premarket.py --send          # push to Feishu
    .venv\\python.exe scripts\\premarket.py --send --force  # send on a non-trading day
    .venv\\python.exe scripts\\premarket.py --date 2026-11-12
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db.base import SessionLocal  # noqa: E402
from app.services.premarket import build_briefing, send_briefing  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="TradeAI 盘前简报（08:45）")
    parser.add_argument("--send", action="store_true", help="推送到飞书（默认只打印）")
    parser.add_argument("--force", action="store_true", help="非交易日也发送")
    parser.add_argument("--date", help="简报日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = parser.parse_args()

    day = date.fromisoformat(args.date) if args.date else None

    with SessionLocal() as session:
        if not args.send:
            briefing = build_briefing(session, day=day)
            if args.json:
                print(json.dumps(briefing, ensure_ascii=False, indent=2, default=str))
            else:
                print(briefing["text"])
                if not briefing["trading_day"]:
                    print("\n（非交易日，--send 会跳过；需要发送请加 --force）")
            return 0

        result = send_briefing(session, force=args.force, day=day)

    briefing = result["briefing"]
    print(briefing["text"])
    print()
    if result.get("reason") == "non_trading_day":
        print("跳过：非交易日（加 --force 可强制发送）")
        return 0
    print(f"发送结果：{result['status']} · {result['response'][:200]}")
    return 0 if result["sent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
