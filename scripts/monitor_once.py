"""Run one monitoring tick from the command line.

Examples:
    .venv\\python.exe scripts\\monitor_once.py --dry-run     # evaluate, don't send
    .venv\\python.exe scripts\\monitor_once.py --send        # evaluate and push
    .venv\\python.exe scripts\\monitor_once.py --sync-calendar --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db.base import SessionLocal  # noqa: E402
from app.providers.akshare_provider import AkShareProvider  # noqa: E402
from app.services.calendar import sync_calendar  # noqa: E402
from app.services.monitor import run_once  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="TradeAI 盘中轮询试跑")
    parser.add_argument("--send", action="store_true", help="真实推送通知（默认只评估）")
    parser.add_argument("--dry-run", action="store_true", help="只评估，不推送（默认行为）")
    parser.add_argument("--no-force", action="store_true", help="遵守交易时段限制")
    parser.add_argument("--sync-calendar", action="store_true", help="先通过 AkShare 同步交易日历")
    args = parser.parse_args()

    dry_run = not args.send or args.dry_run

    with SessionLocal() as session:
        if args.sync_calendar:
            inserted = sync_calendar(session, provider=AkShareProvider())
            print(f"交易日历新增 {inserted} 天")

        summary = run_once(
            session,
            force=not args.no_force,
            dry_run=dry_run,
        )

    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if summary.get("status") in {"ok", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
