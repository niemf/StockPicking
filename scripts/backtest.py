"""回测：把当前漏斗放回历史重放一遍。

Examples:
    .venv\\python.exe scripts\\backtest.py                       # 全部可用区间
    .venv\\python.exe scripts\\backtest.py --start 2026-06-01
    .venv\\python.exe scripts\\backtest.py --no-cycle             # 不用情绪周期门槛
    .venv\\python.exe scripts\\backtest.py --hold 5 --top 10
    .venv\\python.exe scripts\\backtest.py --json data\\backtest.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.backtest import run_backtest  # noqa: E402
from app.db.base import SessionLocal  # noqa: E402
from app.analysis.pipeline import load_profile  # noqa: E402


def _print_block(title: str, data: dict) -> None:
    if not data or not data.get("trades"):
        return
    print(f"\n{title}")
    print(
        f"  {'分组':<10} {'笔数':>4} {'胜率':>6} {'期望R':>7} {'盈亏因子':>8} {'平均持有':>7}"
    )
    for name, item in data.items():
        pf = f"{item['profit_factor']:.2f}" if item.get("profit_factor") else "—"
        print(
            f"  {name:<10} {item['trades']:>4} {item['win_rate']:>5.1f}% "
            f"{item['expectancy_r']:>+7.2f} {pf:>8} {item['avg_days']:>7.1f}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="历史回测（重放当前漏斗）")
    parser.add_argument("--start", type=str, default="")
    parser.add_argument("--end", type=str, default="")
    parser.add_argument("--hold", type=int, default=10, help="最长持有交易日")
    parser.add_argument("--top", type=int, default=None, help="每日候选上限（默认取配置）")
    parser.add_argument("--codes", type=str, default="", help="限定股票，逗号分隔")
    parser.add_argument("--no-cycle", action="store_true", help="不使用情绪周期的 RR 下限")
    parser.add_argument(
        "--exit",
        choices=("plan", "hold"),
        default="plan",
        help="plan=按止损/目标出场；hold=忽略止损目标，持有期满收盘平仓",
    )
    parser.add_argument(
        "--target-mode",
        choices=("level", "rr", "atr"),
        default="",
        help="目标位口径：level=前高/测量涨幅，rr=止损倍数，atr=ATR 倍数",
    )
    parser.add_argument("--target-rr", type=float, default=None, help="--target-mode rr 时的倍数")
    parser.add_argument("--target-atr", type=float, default=None, help="--target-mode atr 时的倍数")
    parser.add_argument("--profile", type=str, default="", help="策略文件路径")
    parser.add_argument("--json", type=str, default="", help="把完整结果写入 JSON")
    parser.add_argument("--trades", type=int, default=10, help="打印最近 N 笔成交")
    args = parser.parse_args()

    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    codes = [code.strip() for code in args.codes.split(",") if code.strip()] or None
    profile = load_profile(args.profile or None)
    if args.target_mode:
        profile.setdefault("risk", {})["target_mode"] = args.target_mode
    if args.target_rr is not None:
        profile.setdefault("risk", {})["target_rr"] = args.target_rr
    if args.target_atr is not None:
        profile.setdefault("risk", {})["target_atr_mult"] = args.target_atr

    with SessionLocal() as session:
        result = run_backtest(
            session,
            start=start,
            end=end,
            profile=profile,
            max_hold=args.hold,
            use_cycle=not args.no_cycle,
            top_n=args.top,
            codes=codes,
            exit_mode=args.exit,
        )

    summary = result["summary"]
    if not summary.get("trades"):
        print("没有产生任何成交：检查日线/指标表是否已回填，或放宽区间。")
        return 0

    print(f"区间 {summary['sessions']} 个交易日 · 每日候选均值 {summary['avg_picks']}")
    print(
        f"成交 {summary['trades']} 笔 · 胜率 {summary['win_rate']}% · "
        f"期望 {summary['expectancy_r']:+.2f}R · 累计 {summary['total_r']:+.1f}R · "
        f"最大回撤 {summary['max_drawdown_r']}R"
    )
    print(
        f"盈亏因子 {summary['profit_factor']} · 平均盈利 {summary['avg_win_r']}R / "
        f"平均亏损 {summary['avg_loss_r']}R · 平均持有 {summary['avg_days']} 天"
    )
    print(
        f"止损 {summary['stop_rate']}% · 达标 {summary['target_rate']}% · "
        f"到期 {summary['timeout_rate']}% · 平均最大浮盈 {summary['avg_mfe_r']}R / "
        f"最大浮亏 {summary['avg_mae_r']}R · 成本 {summary['cost_pct']}%"
    )
    if summary.get("edge_fwd5") is not None:
        print(
            f"选股 alpha：候选 5 日 {summary['avg_fwd5']:+.2f}% vs 全市场 "
            f"{summary['avg_fwd5_universe']:+.2f}% → 超额 {summary['edge_fwd5']:+.2f}%"
        )

    _print_block("按情绪阶段", result["by_phase"])
    _print_block("按盈亏比", result["by_rr"])
    _print_block("按评分", result["by_score"])

    if args.trades:
        print(f"\n最近 {args.trades} 笔")
        for trade in result["trades"][-args.trades :]:
            print(
                f"  {trade['signal_date']} → {trade['entry_date']} {trade['code']} {trade['name'][:6]:<7} "
                f"买 {trade['entry']:.2f} 卖 {trade['exit_price']:.2f} {trade['reason']:<7} "
                f"{trade['ret_pct']:+.2f}% {trade['r_multiple']:+.2f}R ({trade['days']}天)"
            )

    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n完整结果已写入 {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
