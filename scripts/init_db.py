"""Create the SQLite schema (and optionally load demo rows).

Usage:
    .venv\\python.exe scripts\\init_db.py              # create missing tables
    .venv\\python.exe scripts\\init_db.py --seed       # + demo watchlist/holdings
    .venv\\python.exe scripts\\init_db.py --reset --seed  # drop everything first

``--reset`` deletes the SQLite file (or drops every table) and is destructive.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select  # noqa: E402

from app.db.base import Base, SessionLocal, engine, init_db  # noqa: E402
from app.db import models  # noqa: E402,F401
from app.db.seed import seed_demo_data  # noqa: E402

TABLES = [
    "stock",
    "daily_bar",
    "quote",
    "indicator_daily",
    "candidate",
    "watchlist",
    "holding",
    "holding_transaction",
    "trade_plan",
    "alert_rule",
    "alert_event",
    "notification_log",
    "ai_call_log",
    "review_report",
    "market_regime",
    "trade_calendar",
    "job_run",
]


def print_counts() -> None:
    with SessionLocal() as session:
        print("\n表记录数：")
        for name in TABLES:
            table = Base.metadata.tables[name]
            total = session.scalar(select(func.count()).select_from(table))
            print(f"  {name:<22}{total:>6}")


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化 TradeAI 数据库")
    parser.add_argument("--seed", action="store_true", help="导入演示自选股/持仓/计划")
    parser.add_argument("--reset", action="store_true", help="先删除所有表（危险）")
    args = parser.parse_args()

    if args.reset:
        print("Dropping all tables ...")
        Base.metadata.drop_all(engine)

    init_db()

    if args.seed:
        with SessionLocal() as session:
            counts = seed_demo_data(session)
        print("\n演示数据写入：")
        for name, value in counts.items():
            print(f"  {name:<12}{value:>4}")

    print_counts()
    print("\n完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
