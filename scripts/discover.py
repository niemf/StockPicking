"""Run the whole-market discovery pipeline.

Examples:
    .venv\\python.exe scripts\\discover.py --limit 300        # pilot, still persists
    .venv\\python.exe scripts\\discover.py --no-persist       # inspect without writing
    .venv\\python.exe scripts\\discover.py --sync-universe
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.analysis.pipeline import load_profile  # noqa: E402
from app.db.base import SessionLocal  # noqa: E402
from app.services.discovery import run_discovery  # noqa: E402
from app.services.universe import sync_universe  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="全市场技术面发现")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 只（试点）")
    parser.add_argument("--no-persist", action="store_true", help="不写入 candidate 表")
    parser.add_argument("--sync-universe", action="store_true", help="先同步股票列表")
    parser.add_argument("--profile", type=str, default="", help="自定义策略 YAML 路径")
    parser.add_argument("--top", type=int, default=0, help="覆盖输出数量")
    args = parser.parse_args()

    profile = load_profile(args.profile or None)
    if args.top:
        profile.setdefault("output", {})["top_n"] = args.top

    with SessionLocal() as session:
        if args.sync_universe:
            summary = sync_universe(session)
            print(f"股票池：新增 {summary['inserted']}，更新 {summary['updated']}")

        output = run_discovery(
            session,
            profile=profile,
            persist=not args.no_persist,
            limit=args.limit or None,
        )

    print(json.dumps(output["summary"], ensure_ascii=False, indent=2))
    print("\nTop candidates:")
    for item in output["candidates"][:15]:
        print(
            f"  {item['rank']:>2}. {item['code']} {item['name']}  "
            f"{item['score']:.1f}  {item['price']:.2f} ({item['pct_chg']:+.2f}%)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
