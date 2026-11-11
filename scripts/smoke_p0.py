"""Live smoke test for the P0 batch (HTTP GET + local DB schema check).

Run against a running server:  .venv\\python.exe scripts\\smoke_p0.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"
DB = Path(__file__).resolve().parents[1] / "data" / "tradeai.db"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=120.0, follow_redirects=False) as client:
        pages: dict[str, str] = {}
        for path in ["/", "/watchlist", "/alerts", "/candidates", "/plans", "/reports", "/premarket", "/sectors"]:
            response = client.get(path)
            check(f"GET {path}", response.status_code == 200, f"status={response.status_code}")
            pages[path] = response.text

        home = pages["/"]
        check("dashboard sentiment cards", "涨跌家数" in home and "涨停 / 跌停" in home)
        check("dashboard sentiment panel", "市场情绪" in home)
        check("holdings hidden by flag", "持仓概览" not in home and "持仓分布" not in home)
        check("holdings nav hidden", 'href="/holdings"' not in home)

        alerts = pages["/alerts"]
        check("alerts custom rule form", "自定义组合预警" in alerts and "通知渠道" in alerts)

        watch = pages["/watchlist"]
        check("watchlist import + export", "批量导入" in watch and "导出 CSV" in watch)

        stock = client.get("/stock/600036")
        check("GET /stock/600036", stock.status_code == 200, f"status={stock.status_code}")
        check("stock report card panel", "AI 研报卡" in stock.text and "操作清单" in stock.text)
        check("stock fund flow panel", "资金流" in stock.text and "主力（当日）" in stock.text)
        report_api = client.get("/api/reports/600036")
        check(
            "GET /api/reports/600036",
            report_api.status_code == 200,
            report_api.text[:120],
        )
        check("sectors page", "板块资金" in pages["/sectors"])
        check("premarket page", "盘前简报" in pages["/premarket"])
        check(
            "premarket sections",
            "昨日复盘" in pages["/premarket"] and "纪律提醒" in pages["/premarket"],
        )

        sentiment = client.get("/api/market/sentiment")
        check("GET /api/market/sentiment", sentiment.status_code == 200, f"status={sentiment.status_code}")
        if sentiment.status_code == 200:
            data = sentiment.json()
            check(
                "sentiment payload",
                "available" in data,
                f"available={data.get('available')} total={data.get('total')} label={data.get('label')}",
            )

        export = client.get("/watchlist/export.csv")
        check(
            "GET /watchlist/export.csv",
            export.status_code == 200 and "code,name,group,note,tags" in export.text,
            f"status={export.status_code} bytes={len(export.content)}",
        )

    # --------------------------------------------------------------- schema
    if DB.exists():
        connection = sqlite3.connect(DB)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(watchlist)")}
            check("watchlist.group_name column", "group_name" in columns)
            check("watchlist.tags column", "tags" in columns)
            rows = list(connection.execute("SELECT code, group_name FROM watchlist LIMIT 5"))
            check(
                "existing watch rows default group",
                all((group or "默认") == "默认" for _, group in rows),
                f"rows={rows}",
            )
        finally:
            connection.close()
    else:
        check("database file", False, str(DB))

    print()
    if failures:
        print(f"FAILED: {len(failures)} -> {failures}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
