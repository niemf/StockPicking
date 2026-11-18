"""Daily post-close maintenance: update today's bar, then run discovery.

Schedule this at 15:35 on trading days (Windows Task Scheduler / cron):
    .venv\\python.exe scripts\\daily_update.py

By default the day's bar is built from one batch of closing quotes (2–4 s)
instead of 3195 per-stock history requests. Run ``backfill_bars.py --force``
weekly to repair forward-adjustment drift around ex-dividend dates.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.db.base import SessionLocal  # noqa: E402
from app.db.models import Stock  # noqa: E402
from app.providers.base import is_main_board  # noqa: E402
from app.services.bars import bars_from_snapshot, incremental_bars  # noqa: E402
from app.services.calendar import is_trading_day, market_session  # noqa: E402
from app.services.cycle import compute_cycle  # noqa: E402
from app.services.discovery import run_discovery  # noqa: E402
from app.services.indicators import rebuild_daily, rebuild_weekly  # noqa: E402
from app.services.industries import needs_sync, sync_industries  # noqa: E402
from app.services.limit_board import get_lhb_seats, get_limit_pool  # noqa: E402
from app.services.market_daily import snapshot as snapshot_market  # noqa: E402
from app.services.quotes import QuoteService  # noqa: E402
from app.services.signals import track  # noqa: E402
from app.services.validation import run_validation  # noqa: E402
from app.services.universe import sync_universe  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="盘后日线更新 + 全市场发现")
    parser.add_argument("--date", type=str, default="", help="指定交易日 YYYY-MM-DD（默认今天）")
    parser.add_argument("--skip-bars", action="store_true", help="跳过日线更新")
    parser.add_argument(
        "--full-bars",
        action="store_true",
        help="逐只回补最近 12 天（较慢，除权修复用；日常不需要）",
    )
    parser.add_argument("--skip-universe", action="store_true", help="跳过股票列表同步")
    parser.add_argument("--skip-indicators", action="store_true", help="跳过指标表重建")
    parser.add_argument(
        "--fallback-first", action="store_true", help="日线走腾讯/新浪（AkShare 限流时用）"
    )
    parser.add_argument("--force", action="store_true", help="非交易日也执行")
    args = parser.parse_args()

    target = date.fromisoformat(args.date) if args.date else date.today()

    with SessionLocal() as session:
        if not args.force and not is_trading_day(session, target):
            print(f"{target} 不是交易日，跳过。")
            return 0

        if not args.skip_universe:
            summary = sync_universe(session)
            print(f"股票池：新增 {summary['inserted']}，更新 {summary['updated']}")

        if needs_sync():
            try:
                stats = sync_industries(session)
                print(f"行业分类：{stats['count']} 个代码，更新 {stats['updated']} 只")
            except Exception as exc:  # noqa: BLE001 - industry labels are optional
                print(f"行业分类同步失败：{exc}")

        stocks = [
            stock
            for stock in session.scalars(select(Stock).where(Stock.status == "normal"))
            if is_main_board(stock.code)
        ]
        codes = [stock.code for stock in stocks]
        quotes = None

        if not args.skip_bars:
            if args.full_bars:
                stats = incremental_bars(
                    session, codes, prefer_fallback=args.fallback_first
                )
                print(
                    f"日线增量（逐只）：写入 {stats['written']} 行，失败 {len(stats['failed'])} 只，"
                    f"耗时 {stats.get('elapsed_sec', 0)}s"
                )
            elif market_session() != "after_close":
                print("尚未收盘，跳过快照 K 线写入（收盘后再运行）。")
            else:
                quotes = QuoteService().get_quotes(codes)
                written = bars_from_snapshot(session, quotes)
                session.commit()
                print(f"收盘快照 K 线：写入 {written} 只（未逐只请求）")

        # Rebuild the derived tables for the latest sessions. Features are
        # computed from the full history, so the last few rows are exact.
        if not args.skip_indicators:
            stats = rebuild_daily(session, codes, days=3)
            weekly = rebuild_weekly(session, codes)
            print(
                f"指标表：日线 {stats['rows']} 行 / {stats['elapsed_sec']}s，"
                f"周线 {weekly['rows']} 行"
            )

        # 市场级快照 + 结算前几天的信号（都用刚写好的日线，顺序不能反）
        try:
            # 盘中手动运行不写市场行：涨跌停/炸板率会是不完整的半日数据。
            if market_session() == "after_close" or target < date.today():
                if quotes is None:
                    quotes = QuoteService().get_quotes(codes)
                cycle = compute_cycle(session, trade_date=target, quotes=quotes, refresh=True)
                market_row = snapshot_market(session, target, cycle=cycle)
                if market_row is not None:
                    break_pct = (
                        f"{market_row.break_rate * 100:.0f}%"
                        if market_row.break_rate is not None
                        else "—"
                    )
                    print(
                        f"市场快照：{cycle.label}（{cycle.phase}）· 涨停 {market_row.limit_up} · "
                        f"炸板率 {break_pct} · 成交 {market_row.amount_yi} 亿"
                    )
            tracked = track(session, as_of=target)
            print(f"信号结算：检查 {tracked['checked']} 条，结算 {tracked['resolved']} 条")
        except Exception as exc:  # noqa: BLE001 - 市场快照失败不应阻断发现
            print(f"市场快照/信号结算失败：{exc}")

        output = run_discovery(session, quotes=quotes, today=target, strategy="indicator")
        try:
            youshi = run_discovery(session, quotes=quotes, today=target, strategy="youshi")
            print(
                f"游资选股：{len(youshi['candidates'])} 只（{youshi['summary']['survivors']} 只过闸门）"
            )
        except Exception as exc:  # noqa: BLE001 - 游资档失败不影响主流程
            print(f"游资选股失败：{exc}")

        # 收盘后滚动回测验证：两套策略都用最近 60 个交易日评估
        try:
            validation = run_validation(session, days=60, moment=datetime.now())
            for key in ("indicator", "youshi"):
                item = validation.get(key) or {}
                print(
                    f"回测验证[{key}]：{item.get('trades') or 0} 笔 · "
                    f"期望 {item.get('expectancy_r')}R · alpha {item.get('edge_fwd5')}%"
                )
        except Exception as exc:  # noqa: BLE001 - 验证失败不影响选股
            print(f"回测验证失败：{exc}")
        summary = output["summary"]
        print(
            f"发现完成：{summary['codes']} 只 -> {len(output['candidates'])} 只候选，"
            f"用时 {summary['total_sec']}s"
        )
        for item in output["candidates"][:10]:
            print(f"  {item['rank']:>2}. {item['code']} {item['name']} {item['score']:.1f}")

        # Warm the pages that are expensive on first view (~15 s combined).
        try:
            pool = get_limit_pool(target, refresh=True)
            seats = get_lhb_seats(target, refresh=True)
            print(
                f"涨停池预热：{pool.get('total') or 0} 只，高度 {pool.get('max_streak') or 0}；"
                f"龙虎榜席位 {seats.get('count') or 0} 个"
            )
        except Exception as exc:  # noqa: BLE001 - warming must not fail the job
            print(f"涨停池预热失败：{exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
