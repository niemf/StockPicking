"""Sync the main-board stock master list (listing dates + ST flags).

Usage:
    .venv\\python.exe scripts\\sync_universe.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db.base import SessionLocal  # noqa: E402
from app.services.universe import sync_universe  # noqa: E402


def main() -> int:
    with SessionLocal() as session:
        summary = sync_universe(session)
    print(
        f"股票池已更新：新增 {summary['inserted']}，更新 {summary['updated']}，"
        f"主板合计 {summary['total']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
