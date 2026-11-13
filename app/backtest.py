"""历史回测：把**当前这套漏斗**放回过去 200 多个交易日重放一遍。

设计原则：**不重写策略**。过滤器、评分、盈亏比闸门都调用 `app/analysis` 里的
原函数，只是把输入从「实时行情 + 日线」换成「指标表里当天那一行」。这样回测
结果和线上候选是同一套逻辑，不会出现"回测能赚、实盘不行"的经典问题。

成交假设（故意保守）：

* T 日收盘出信号，**T+1 开盘价**成交（不给收盘价成交的便宜）；
* 开盘价已经跌破止损或越过目标 → 放弃该笔（无法成交）；
* 逐日判断：先触止损？先触目标？同一天两者都触及时按**先止损**；
* 滑点 0.1% + 手续费 0.03%，买卖各一次，往返 0.26%；
* 超过 `max_hold` 个交易日仍未触发，按当日收盘平仓。

输出：笔数、胜率、平均 R、期望 R、盈亏因子、最大回撤、平均持有天数，
并按情绪阶段 / RR 档 / 评分档分组。验收线：≥30 笔、期望 ≥0.2R、回撤 <15R。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.context import StockMeta
from app.analysis.factors import FILTERS, SCORERS
from app.analysis.pipeline import _enabled, _deep_merge
from app.analysis.risk import RiskPlan, build_plan, rr_bonus
from app.db.models import DailyBar, IndicatorDaily, MarketDaily, Stock
from app.providers.base import is_main_board
from app.services.cycle import PHASE_META
from app.services.universe import listed_trading_days

SLIPPAGE = 0.001
COMMISSION = 0.0003
ROUND_TRIP_COST_PCT = (SLIPPAGE + COMMISSION) * 2 * 100  # 0.26%

QUOTE_KEYS = ("close", "high", "low", "pct_chg")
INDICATOR_COLUMNS = (
    "ma5",
    "ma10",
    "ma20",
    "ma30",
    "ma60",
    "ma20_slope3",
    "volume_ratio",
    "rsi12",
    "macd_dif",
    "macd_dea",
    "macd_hist",
    "atr14",
    "atr_pct",
    "high20",
    "high20_prev",
    "low20",
    "high60_prev",
    "resistance_dist",
    "range20_pct",
    "drawdown20",
    "close_position",
    "upper_shadow_pct",
    "lower_shadow_pct",
    "ret5",
    "ret20",
    "up_streak",
)


# ------------------------------------------------------------------ context
class _Quote:
    """过滤器和风险模块只用到这几个字段，不必构造完整 Quote。"""

    __slots__ = ("price", "high", "low", "pct_chg", "pre_close", "amount", "turnover_rate")

    def __init__(self, price, high, low, pct_chg, pre_close=0.0, amount=0.0, turnover_rate=0.0):
        self.price = price
        self.high = high
        self.low = low
        self.pct_chg = pct_chg
        self.pre_close = pre_close
        self.amount = amount
        self.turnover_rate = turnover_rate


@dataclass
class CodePanel:
    code: str
    meta: StockMeta
    dates: np.ndarray
    cols: dict[str, np.ndarray]

    def index_of(self, day: date) -> int:
        return int(np.searchsorted(self.dates, day))


class PanelContext:
    """和 ``StockContext`` 同接口的只读视图，指向指标表的某一行。"""

    __slots__ = ("panel", "idx", "extras")

    def __init__(self, panel: CodePanel, idx: int) -> None:
        self.panel = panel
        self.idx = idx
        self.extras: dict = {"session_progress": 1.0}

    # -- 与 StockContext 对齐的属性
    @property
    def code(self) -> str:
        return self.panel.code

    @property
    def name(self) -> str:
        return self.panel.meta.name

    @property
    def meta(self) -> StockMeta:
        return self.panel.meta

    @property
    def price(self) -> float:
        return float(self.panel.cols["close"][self.idx])

    @property
    def pct_chg(self) -> float:
        return float(self.panel.cols["pct_chg"][self.idx])

    @property
    def amount(self) -> float:
        return float(self.panel.cols["amount"][self.idx])

    @property
    def turnover_rate(self) -> float:
        return float(self.panel.cols["turnover_rate"][self.idx])

    @property
    def listed_days(self) -> int:
        return listed_trading_days(self.panel.meta.list_date, self.panel.dates[self.idx], [])

    @property
    def quote(self):
        return _Quote(
            price=self.price,
            high=float(self.panel.cols["high"][self.idx]),
            low=float(self.panel.cols["low"][self.idx]),
            pct_chg=self.pct_chg,
            amount=self.amount,
            turnover_rate=self.turnover_rate,
        )

    def feature(self, key: str, offset: int = 0):
        array = self.panel.cols.get(key)
        if array is None:
            return None
        index = self.idx - offset
        if index < 0 or index >= len(array):
            return None
        value = array[index]
        if value is None or (isinstance(value, float) and np.isnan(value)):
            return None
        return float(value)

    def has(self, keys: tuple[str, ...]) -> bool:
        return all(key in self.panel.cols for key in keys)


# ------------------------------------------------------------------ data
def load_panels(session: Session, *, codes: list[str] | None = None) -> tuple[dict, dict]:
    """把日线和指标表拼成 {code: CodePanel} 与 {交易日: [code...]}。"""
    bars = pd.read_sql_query(
        select(
            DailyBar.code,
            DailyBar.trade_date,
            DailyBar.open,
            DailyBar.high,
            DailyBar.low,
            DailyBar.close,
            DailyBar.amount,
            DailyBar.turnover_rate,
        ).order_by(DailyBar.code, DailyBar.trade_date),
        session.connection(),
    )
    indicators = pd.read_sql_query(
        select(IndicatorDaily.code, IndicatorDaily.trade_date, *[getattr(IndicatorDaily, name) for name in INDICATOR_COLUMNS]),
        session.connection(),
    )
    if bars.empty:
        return {}, {}

    frame = bars.merge(indicators, on=["code", "trade_date"], how="left")
    if codes:
        frame = frame[frame["code"].isin(codes)]

    stocks = {
        stock.code: stock
        for stock in session.scalars(select(Stock))
        if is_main_board(stock.code)
    }

    panels: dict[str, CodePanel] = {}
    by_date: dict[date, list[str]] = {}
    for code, group in frame.groupby("code", sort=False):
        stock = stocks.get(code)
        if stock is None:
            continue
        group = group.sort_values("trade_date")
        dates = group["trade_date"].to_numpy()
        cols: dict[str, np.ndarray] = {}
        close = group["close"].to_numpy(dtype="float64")
        prev = np.concatenate(([np.nan], close[:-1]))
        cols["close"] = close
        cols["open"] = group["open"].to_numpy(dtype="float64")
        cols["high"] = group["high"].to_numpy(dtype="float64")
        cols["low"] = group["low"].to_numpy(dtype="float64")
        cols["amount"] = group["amount"].fillna(0).to_numpy(dtype="float64")
        cols["turnover_rate"] = group["turnover_rate"].fillna(0).to_numpy(dtype="float64")
        cols["pct_chg"] = (close / prev - 1) * 100
        open_ = cols["open"]
        with np.errstate(invalid="ignore", divide="ignore"):
            cols["gap_pct"] = (open_ / prev - 1) * 100
        limit_up = cols["pct_chg"] >= 9.7
        streak = np.zeros(len(close))
        for index in range(len(close)):
            streak[index] = streak[index - 1] + 1 if limit_up[index] and index else (1 if limit_up[index] else 0)
        cols["limit_up_streak"] = streak
        cols["ma20"] = group["ma20"].to_numpy(dtype="float64")
        for name in INDICATOR_COLUMNS:
            if name in cols:
                continue
            cols[name] = group[name].to_numpy(dtype="float64")

        panels[code] = CodePanel(
            code=code,
            meta=StockMeta(
                code=code,
                name=stock.name,
                industry=stock.industry,
                list_date=stock.list_date,
                is_st=bool(stock.is_st),
                is_delisting_risk=bool(stock.is_delisting_risk),
                status=stock.status or "normal",
            ),
            dates=dates,
            cols=cols,
        )
        for day in dates:
            by_date.setdefault(day, []).append(code)

    del frame
    logger.info("backtest panels: {} codes, {} sessions", len(panels), len(by_date))
    return panels, by_date


# ------------------------------------------------------------------ screening
def screen_day(
    day: date,
    codes: list[str],
    panels: dict[str, CodePanel],
    profile: dict,
    *,
    cycle_phase: str | None = None,
) -> list[dict]:
    """在某个交易日重放漏斗，返回通过闸门的候选（含计划）。"""
    filters_cfg = profile.get("filters", {})
    universe_cfg = profile.get("universe", {})
    scoring_cfg = profile.get("scoring", {})
    risk_cfg = dict(profile.get("risk") or {})
    if cycle_phase and cycle_phase in PHASE_META:
        risk_cfg["min_reward_risk"] = max(
            float(risk_cfg.get("min_reward_risk", 2.0)), PHASE_META[cycle_phase]["rr_floor"]
        )

    stage_defs = {
        stage: _enabled(FILTERS, stage, filters_cfg) for stage in range(1, 6)
    }
    survivors: list[PanelContext] = []
    for code in codes:
        panel = panels.get(code)
        if panel is None:
            continue
        index = panel.index_of(day)
        if index >= len(panel.dates) or panel.dates[index] != day:
            continue
        context = PanelContext(panel, index)
        if context.feature("close") is None:
            continue
        keep = True
        for stage in range(1, 6):
            for definition in stage_defs[stage]:
                config = (
                    universe_cfg
                    if definition.key == "universe_basic"
                    else filters_cfg.get(definition.key, {})
                )
                if definition.fn(context, config):
                    keep = False
                    break
            if not keep:
                break
        if keep:
            survivors.append(context)

    if not survivors:
        return []

    ret20 = [ctx.feature("ret20") or -999 for ctx in survivors]
    percentiles = pd.Series(ret20).rank(pct=True).tolist()
    for ctx, percentile in zip(survivors, percentiles):
        ctx.extras["rs_percentile"] = percentile

    weights = {
        key: float(config.get("weight", 0))
        for key, config in scoring_cfg.items()
        if isinstance(config, dict) and float(config.get("weight", 0)) > 0
    }
    total_weight = sum(weights.values()) or 1.0

    scored: list[dict] = []
    for ctx in survivors:
        weighted = 0.0
        breakdown: dict[str, float] = {}
        for key, weight in weights.items():
            definition = SCORERS.get(key)
            if definition is None:
                continue
            value = max(0.0, min(100.0, definition.fn(ctx, scoring_cfg.get(key, {}))))
            breakdown[key] = round(value, 2)
            weighted += value * weight
        base = weighted / total_weight
        plan, _reason = build_plan(ctx, risk_cfg)
        if plan is None:
            continue
        scored.append(
            {
                "code": ctx.code,
                "name": ctx.name,
                "idx": ctx.idx,
                "score": round(base, 2),
                "rank_score": round(base + rr_bonus(plan.rr, risk_cfg), 2),
                "plan": plan,
                "breakdown": breakdown,
            }
        )

    scored.sort(key=lambda item: item["rank_score"], reverse=True)
    top_n = int(profile.get("output", {}).get("top_n", 15))
    return scored[:top_n]


# ------------------------------------------------------------------ execution
@dataclass
class Trade:
    code: str
    name: str
    signal_date: date
    entry_date: date
    entry: float
    stop: float
    target: float
    exit_date: date | None = None
    exit_price: float | None = None
    reason: str = "open"
    days: int = 0
    ret_pct: float = 0.0
    r_multiple: float = 0.0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    phase: str | None = None
    score: float = 0.0
    rr_planned: float = 0.0
    fwd5_pct: float | None = None

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "signal_date": self.signal_date.isoformat(),
            "entry_date": self.entry_date.isoformat(),
            "entry": round(self.entry, 3),
            "stop": round(self.stop, 3),
            "target": round(self.target, 3),
            "exit_date": self.exit_date.isoformat() if self.exit_date else None,
            "exit_price": round(self.exit_price, 3) if self.exit_price is not None else None,
            "reason": self.reason,
            "days": self.days,
            "ret_pct": round(self.ret_pct, 2),
            "r_multiple": round(self.r_multiple, 2),
            "mfe_r": round(self.mfe_r, 2),
            "mae_r": round(self.mae_r, 2),
            "phase": self.phase,
            "score": self.score,
            "rr_planned": round(self.rr_planned, 2),
            "fwd5_pct": round(self.fwd5_pct, 2) if self.fwd5_pct is not None else None,
        }


def simulate(
    panel: CodePanel,
    signal_idx: int,
    plan: RiskPlan,
    *,
    max_hold: int = 10,
) -> Trade | None:
    """从信号日次日开盘买入，逐日判断止损/目标，返回一笔成交。"""
    entry_idx = signal_idx + 1
    if entry_idx >= len(panel.dates):
        return None

    opens = panel.cols["open"]
    highs = panel.cols["high"]
    lows = panel.cols["low"]
    closes = panel.cols["close"]

    entry = float(opens[entry_idx])
    if not entry or np.isnan(entry) or entry <= 0:
        return None
    # 跳空越过边界：无法按计划成交
    if entry <= plan.stop or entry >= plan.target1:
        return None

    risk = entry - plan.stop
    if risk <= 0:
        return None

    trade = Trade(
        code=panel.code,
        name=panel.meta.name,
        signal_date=panel.dates[signal_idx],
        entry_date=panel.dates[entry_idx],
        entry=entry,
        stop=float(plan.stop),
        target=float(plan.target1),
        rr_planned=float(plan.rr),
    )

    exit_price = None
    reason = "open"
    exit_index = entry_idx
    for index in range(entry_idx, min(entry_idx + max_hold, len(panel.dates))):
        exit_index = index
        if lows[index] <= plan.stop:
            exit_price, reason = float(plan.stop), "stop"
            break
        if highs[index] >= plan.target1:
            exit_price, reason = float(plan.target1), "target"
            break
    if exit_price is None:
        exit_price, reason = float(closes[exit_index]), "timeout"

    trade.exit_date = panel.dates[exit_index]
    trade.exit_price = exit_price
    trade.reason = reason
    trade.days = int(exit_index - entry_idx + 1)
    trade.ret_pct = (exit_price / entry - 1) * 100 - ROUND_TRIP_COST_PCT
    trade.r_multiple = trade.ret_pct / (risk / entry * 100)
    window = slice(entry_idx, exit_index + 1)
    trade.mfe_r = ((float(np.nanmax(highs[window])) / entry - 1) * 100) / (risk / entry * 100)
    trade.mae_r = ((float(np.nanmin(lows[window])) / entry - 1) * 100) / (risk / entry * 100)
    return trade


# ------------------------------------------------------------------ metrics
def summarise(trades: list[Trade]) -> dict:
    if not trades:
        return {"trades": 0}

    done = [trade for trade in trades if trade.exit_price is not None]
    if not done:
        return {"trades": 0}

    rs = [trade.r_multiple for trade in done]
    wins = [value for value in rs if value > 0]
    losses = [value for value in rs if value <= 0]
    ordered = sorted(done, key=lambda trade: trade.exit_date or trade.signal_date)
    forward = [trade.fwd5_pct for trade in done if trade.fwd5_pct is not None]

    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for trade in ordered:
        cumulative += trade.r_multiple
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    return {
        "trades": len(done),
        "win_rate": round(len(wins) / len(done) * 100, 1),
        "avg_r": round(statistics.fmean(rs), 3),
        "expectancy_r": round(statistics.fmean(rs), 3),
        "avg_win_r": round(statistics.fmean(wins), 2) if wins else None,
        "avg_loss_r": round(statistics.fmean(losses), 2) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "total_r": round(cumulative, 1),
        "max_drawdown_r": round(max_drawdown, 1),
        "avg_days": round(statistics.fmean([trade.days for trade in done]), 1),
        "stop_rate": round(sum(1 for trade in done if trade.reason == "stop") / len(done) * 100, 1),
        "target_rate": round(sum(1 for trade in done if trade.reason == "target") / len(done) * 100, 1),
        "timeout_rate": round(sum(1 for trade in done if trade.reason == "timeout") / len(done) * 100, 1),
        "avg_mfe_r": round(statistics.fmean([trade.mfe_r for trade in done]), 2),
        "avg_mae_r": round(statistics.fmean([trade.mae_r for trade in done]), 2),
        # 不看止损/目标，只看「信号日收盘 → 5 日后收盘」的原始涨幅：
        # 这是判断选股本身有没有 alpha 的关键数字。
        "avg_fwd5": round(statistics.fmean(forward), 3) if forward else None,
    }


def group_by(trades: list[Trade], key) -> dict:
    buckets: dict[str, list[Trade]] = {}
    for trade in trades:
        buckets.setdefault(str(key(trade)), []).append(trade)
    return {name: summarise(items) for name, items in sorted(buckets.items())}


# ------------------------------------------------------------------ runner
def forward_return(panel: CodePanel, idx: int, horizon: int = 5) -> float | None:
    """信号日收盘 → N 个交易日后收盘的涨幅（不看止损/目标）。"""
    target = idx + horizon
    if target >= len(panel.dates):
        return None
    start = panel.cols["close"][idx]
    end = panel.cols["close"][target]
    if not start or start <= 0 or np.isnan(start) or np.isnan(end):
        return None
    return float((end / start - 1) * 100)


def run_backtest(
    session: Session,
    *,
    start: date | None = None,
    end: date | None = None,
    profile: dict,
    max_hold: int = 10,
    use_cycle: bool = True,
    top_n: int | None = None,
    codes: list[str] | None = None,
    panels: dict | None = None,
    by_date: dict | None = None,
) -> dict:
    if panels is None or by_date is None:
        panels, by_date = load_panels(session, codes=codes)
    if not panels:
        return {"summary": {"trades": 0}, "trades": [], "sessions": 0}

    if top_n is not None:
        profile = _deep_merge(profile, {"output": {"top_n": top_n}})

    phases = {
        row.trade_date: row.phase
        for row in session.scalars(select(MarketDaily))
    }

    sessions = sorted(day for day in by_date if (start is None or day >= start) and (end is None or day <= end))
    trades: list[Trade] = []
    per_day: list[dict] = []
    universe_forward: list[float] = []

    for day in sessions:
        phase = phases.get(day) if use_cycle else None
        picks = screen_day(day, by_date[day], panels, profile, cycle_phase=phase)

        # 全市场等权基准：同一天所有主板股票「收盘 → 5 日后收盘」的平均涨幅。
        day_returns: list[float] = []
        for code in by_date[day]:
            panel = panels.get(code)
            if panel is None:
                continue
            index = panel.index_of(day)
            if index >= len(panel.dates) or panel.dates[index] != day:
                continue
            value = forward_return(panel, index, 5)
            if value is not None:
                day_returns.append(value)
        if day_returns:
            universe_forward.append(statistics.fmean(day_returns))

        for pick in picks:
            panel = panels[pick["code"]]
            trade = simulate(panel, pick["idx"], pick["plan"], max_hold=max_hold)
            if trade is None:
                continue
            trade.phase = phase
            trade.score = pick["score"]
            trade.fwd5_pct = forward_return(panel, pick["idx"], 5)
            trades.append(trade)
        per_day.append({"date": day.isoformat(), "phase": phase, "picks": len(picks)})

    summary = summarise(trades)
    summary["sessions"] = len(sessions)
    summary["avg_picks"] = round(statistics.fmean([item["picks"] for item in per_day] or [0]), 1)
    summary["cost_pct"] = ROUND_TRIP_COST_PCT
    summary["avg_fwd5_universe"] = (
        round(statistics.fmean(universe_forward), 3) if universe_forward else None
    )
    if summary.get("avg_fwd5") is not None and summary["avg_fwd5_universe"] is not None:
        summary["edge_fwd5"] = round(summary["avg_fwd5"] - summary["avg_fwd5_universe"], 3)
    else:
        summary["edge_fwd5"] = None

    return {
        "summary": summary,
        "trades": [trade.as_dict() for trade in trades],
        "by_phase": group_by(trades, lambda trade: trade.phase or "—"),
        "by_rr": group_by(
            trades,
            lambda trade: (
                "RR 2-3" if trade.rr_planned < 3 else ("RR 3-5" if trade.rr_planned < 5 else "RR 5+")
            ),
        ),
        "by_score": group_by(
            trades,
            lambda trade: (
                "评分 <50" if trade.score < 50 else ("评分 50-65" if trade.score < 65 else "评分 65+")
            ),
        ),
        "sessions_detail": per_day,
    }


__all__ = [
    "CodePanel",
    "PanelContext",
    "Trade",
    "forward_return",
    "group_by",
    "load_panels",
    "run_backtest",
    "screen_day",
    "simulate",
    "summarise",
]
