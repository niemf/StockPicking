"""Sync industry labels for all listed stocks (Baostock, ~90 s).

Usage:
    .venv\\python.exe scripts\\sync_industries.py
    .venv\\python.exe scripts\\sync_industries.py --force
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db.base import SessionLocal  # noqa: E402
from app.services.industries import needs_sync, sync_industries  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="同步全市场行业分类（Baostock）")
    parser.add_argument("--force", action="store_true", help="忽略 7 天缓存，强制同步")
    args = parser.parse_args()

    with SessionLocal() as session:
        if not args.force and not needs_sync():
            print("最近 7 天已同步，跳过（--force 可强制）")
            return 0
        stats = sync_industries(session, force=args.force)

    print(f"行业分类：{stats['count']} 个代码，更新 {stats['updated']} 只股票（{stats['date']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
