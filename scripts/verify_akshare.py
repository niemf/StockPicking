"""Verify AkShare daily bars, financials, announcements, calendar and index data.

Run:
    .venv\\python.exe scripts\\verify_akshare.py
    .venv\\python.exe scripts\\verify_akshare.py --code 600519
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.providers.akshare_provider import AkShareProvider  # noqa: E402


def section(title: str) -> None:
    print("\n" + "-" * 78)
    print(title)
    print("-" * 78)


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 AkShare 数据接口")
    parser.add_argument("--code", default="600000", help="股票代码，默认 600000")
    parser.add_argument("--days", type=int, default=120, help="日线回溯天数，默认 120")
    args = parser.parse_args()

    provider = AkShareProvider()
    failures: list[str] = []
    started_all = time.perf_counter()

    print("=" * 78)
    print(f"TradeAI AkShare 验证 · {date.today().isoformat()} · {args.code}")
    print("=" * 78)

    # ---------------------------------------------------------------- stock list
    section("1. 股票列表 / 主板过滤")
    started = time.perf_counter()
    try:
        stocks = provider.get_stock_list(main_board_only=True)
        elapsed = (time.perf_counter() - started) * 1000
        print(f"  主板股票数量：{len(stocks)}，耗时 {elapsed:.0f} ms")
        for stock in stocks[:8]:
            print(f"  {stock.code}  {stock.name:<8}  {stock.exchange}  {stock.board}")
        print(f"  ... 共 {len(stocks)} 只")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {exc}")
        failures.append("股票列表")

    # ---------------------------------------------------------------- daily bars
    section(f"2. 日线（前复权，最近 {args.days} 天）")
    started = time.perf_counter()
    try:
        end = date.today()
        start = end - timedelta(days=args.days)
        bars = provider.get_daily_bars(args.code, start=start, end=end, adjust="qfq")
        elapsed = (time.perf_counter() - started) * 1000
        print(f"  记录数：{len(bars)}，耗时 {elapsed:.0f} ms")
        print(f"  字段：{list(bars.columns)}")
        if not bars.empty:
            print("\n  最近 5 个交易日：")
            print(bars.tail(5).to_string(index=False))
        else:
            print("  未返回数据")
            failures.append("日线")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {exc}")
        failures.append("日线")

    # ---------------------------------------------------------------- financials
    section("3. 财务指标")
    started = time.perf_counter()
    try:
        financials = provider.get_financial_indicators(args.code)
        elapsed = (time.perf_counter() - started) * 1000
        print(f"  记录数：{len(financials)}，字段数：{len(financials.columns)}，耗时 {elapsed:.0f} ms")
        print(f"  字段（前 15 个）：{list(financials.columns)[:15]}")
        print("\n  数据预览（前 10 列）：")
        print(financials.head(3).iloc[:, :10].to_string(index=False, max_colwidth=18))
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {exc}")
        failures.append("财务指标")

    # ---------------------------------------------------------------- announcements
    section("4. 公告（最近 30 天）")
    started = time.perf_counter()
    try:
        notices = provider.get_announcements(args.code, days=30)
        elapsed = (time.perf_counter() - started) * 1000
        print(f"  记录数：{len(notices)}，耗时 {elapsed:.0f} ms")
        if not notices.empty:
            print(f"  字段：{list(notices.columns)}")
            print(notices.head(5).to_string(index=False, max_colwidth=30))
        else:
            print("  未返回公告（可能是接口变更或该股票近期无公告）")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {exc}")
        failures.append("公告")

    # ---------------------------------------------------------------- calendar
    section("5. 交易日历")
    started = time.perf_counter()
    try:
        today = date.today()
        calendar = provider.get_trade_calendar(
            start=today - timedelta(days=14), end=today + timedelta(days=14)
        )
        elapsed = (time.perf_counter() - started) * 1000
        print(f"  返回 {len(calendar)} 个交易日，耗时 {elapsed:.0f} ms")
        print(f"  最近交易日：{[d.isoformat() for d in calendar[-5:]]}")
        print(f"  今天是交易日：{provider.is_trade_day(today)}")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {exc}")
        failures.append("交易日历")

    # ---------------------------------------------------------------- index
    section("6. 指数日线（sh000001）")
    started = time.perf_counter()
    try:
        index_bars = provider.get_index_daily("sh000001", start=date.today() - timedelta(days=30))
        elapsed = (time.perf_counter() - started) * 1000
        print(f"  记录数：{len(index_bars)}，耗时 {elapsed:.0f} ms")
        print(f"  字段：{list(index_bars.columns)}")
        if not index_bars.empty:
            print(index_bars.tail(3).to_string(index=False))
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {exc}")
        failures.append("指数日线")

    total = time.perf_counter() - started_all
    print("\n" + "=" * 78)
    print(f"总耗时：{total:.1f} s")
    if failures:
        print(f"失败项：{', '.join(failures)}")
        print("提示：AkShare 接口可能随上游站点改版而变化，失败项需要单独适配。")
        return 1
    print("结果：AkShare 日线 / 财务 / 公告 / 日历 / 指数接口均可用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
