"""Live POST smoke test: import duplicate handling + custom rule create/delete.

Picks a watch item that has no composite rule yet, creates a harmless rule
(price <= 0.01, never triggers), verifies it renders, then deletes it.
Skips the rule part if every watch item already has a custom rule.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from urllib.parse import unquote

import httpx

BASE = "http://127.0.0.1:8000"
DB = Path(__file__).resolve().parents[1] / "data" / "tradeai.db"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


def main() -> int:
    connection = sqlite3.connect(DB)
    try:
        watch = list(
            connection.execute(
                "SELECT id, code FROM watchlist WHERE status='active' ORDER BY id"
            )
        )
        used = {
            row[0]
            for row in connection.execute(
                "SELECT target_id FROM alert_rule WHERE target_type='watch' AND rule_type='COMPOSITE'"
            )
        }
    finally:
        connection.close()

    free = [(wid, code) for wid, code in watch if wid not in used]

    with httpx.Client(base_url=BASE, timeout=60.0, follow_redirects=False) as client:
        if watch:
            code = watch[0][1]
            imported = client.post("/watchlist/import", data={"codes": code})
            location = unquote(imported.headers.get("location", ""))
            check(
                "POST /watchlist/import duplicate",
                imported.status_code == 303 and "跳过重复 1" in location,
                f"status={imported.status_code} location={location[:160]}",
            )
        else:
            print("SKIP import smoke: watchlist empty")

        if not free:
            print("SKIP rule smoke: every watch item already has a custom rule")
        else:
            watch_id, code = free[0]
            created = client.post(
                "/alerts/rules",
                data={
                    "code": code,
                    "logic": "AND",
                    "metric_a": "price",
                    "op_a": "<=",
                    "value_a": "0.01",
                    "metric_b": "",
                    "value_b": "",
                    "channel": "feishu",
                    "max_per_day": "1",
                    "expires_on": "",
                },
            )
            location = created.headers.get("location", "")
            check(
                f"POST /alerts/rules ({code})",
                created.status_code == 303 and "error" not in location,
                f"status={created.status_code} location={location[:160]}",
            )

            page = client.get("/alerts").text
            check("rule renders on /alerts", code in page and "价格≤0.01" in page.replace(" ", ""))

            connection = sqlite3.connect(DB)
            try:
                rule_id = connection.execute(
                    "SELECT id FROM alert_rule WHERE target_type='watch' AND rule_type='COMPOSITE' AND target_id=?",
                    (watch_id,),
                ).fetchone()
            finally:
                connection.close()

            if rule_id:
                deleted = client.post(f"/alerts/rules/{rule_id[0]}/delete")
                check(
                    "POST /alerts/rules/{id}/delete",
                    deleted.status_code == 303,
                    f"status={deleted.status_code}",
                )
                connection = sqlite3.connect(DB)
                try:
                    left = connection.execute(
                        "SELECT COUNT(*) FROM alert_rule WHERE id=?", (rule_id[0],)
                    ).fetchone()[0]
                finally:
                    connection.close()
                check("rule removed from database", left == 0)
            else:
                check("rule persisted in database", False, "no row found")

    print()
    if failures:
        print(f"FAILED: {len(failures)} -> {failures}")
        return 1
    print("ALL POST CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
