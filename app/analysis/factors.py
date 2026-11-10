"""Hard filters and scoring factors.

Filters answer "can this stock be recommended at all" and return the rejection
reason when they say no. Scorers only rank the survivors. Both are registered,
which is what makes the screen composable: adding a factor never touches the
pipeline.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.analysis.context import StockContext

Reason = str | None


@dataclass(frozen=True)
class FilterDef:
    key: str
    stage: int
    label: str
    requires: tuple[str, ...]
    fn: Callable[[StockContext, dict], Reason]
    order: int = 100


@dataclass(frozen=True)
class ScoreDef:
    key: str
    label: str
    requires: tuple[str, ...]
    fn: Callable[[StockContext, dict], float]
    order: int = 100


FILTERS: dict[str, FilterDef] = {}
SCORERS: dict[str, ScoreDef] = {}


def hard_filter(
    key: str,
    stage: int,
    label: str,
    *,
    requires: tuple[str, ...] = (),
    order: int = 100,
):
    def decorator(func: Callable[[StockContext, dict], Reason]):
        FILTERS[key] = FilterDef(key, stage, label, requires, func, order)
        return func

    return decorator


def scorer(
    key: str,
    label: str,
    *,
    requires: tuple[str, ...] = (),
    order: int = 100,
):
    def decorator(func: Callable[[StockContext, dict], float]):
        SCORERS[key] = ScoreDef(key, label, requires, func, order)
        return func

    return decorator


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


# ------------------------------------------------------------------ stage 1
@hard_filter("universe_basic", 1, "基础池", order=10)
def _universe_basic(ctx: StockContext, cfg: dict) -> Reason:
    name = ctx.meta.name.replace(" ", "")
    if cfg.get("exclude_st", True) and (ctx.meta.is_st or "ST" in name.upper()):
        return "ST"
    if ctx.meta.is_delisting_risk or "退" in name:
        return "退市风险"
    if ctx.meta.status == "suspended":
        return "停牌"
    if ctx.quote is None or ctx.price <= 0:
        return "无有效行情"
    if ctx.listed_days < int(cfg.get("min_list_days", 60)):
        return f"次新（上市 {ctx.listed_days} 个交易日）"
    return None


# ------------------------------------------------------------------ stage 2
@hard_filter("price_range", 2, "价格区间", order=10)
def _price_range(ctx: StockContext, cfg: dict) -> Reason:
    price = ctx.price
    if price < float(cfg.get("min_price", 2.0)):
        return "股价过低"
    if price > float(cfg.get("max_price", 1000.0)):
        return "股价过高"
    return None


@hard_filter("liquidity", 2, "流动性", order=20)
def _liquidity(ctx: StockContext, cfg: dict) -> Reason:
    progress = max(0.1, float(ctx.extras.get("session_progress", 1.0)))
    min_amount = float(cfg.get("min_amount", 100_000_000)) * progress
    if ctx.amount < min_amount:
        return "成交额不足"
    min_turnover = float(cfg.get("min_turnover", 1.0))
    max_turnover = float(cfg.get("max_turnover", 15.0))
    if ctx.turnover_rate and not (min_turnover <= ctx.turnover_rate <= max_turnover):
        return "换手率异常"
    return None


# ------------------------------------------------------------------ stage 3
@hard_filter("limit_state", 3, "涨跌停状态", order=10)
def _limit_state(ctx: StockContext, cfg: dict) -> Reason:
    if ctx.quote is not None and ctx.quote.high == ctx.quote.low and ctx.pct_chg >= 9.5:
        return "一字涨停"
    return None


@hard_filter(
    "limit_extreme",
    5,
    "极端走势",
    requires=("limit_up_streak", "gap_pct"),
    order=70,
)
def _limit_extreme(ctx: StockContext, cfg: dict) -> Reason:
    streak = ctx.feature("limit_up_streak") or 0
    if streak > int(cfg.get("max_limit_up_streak", 2)):
        return "连续涨停"
    gap = ctx.feature("gap_pct")
    if gap is not None and gap < float(cfg.get("max_gap_down_pct", -5.0)):
        return "大幅低开"
    return None


@hard_filter(
    "ma20_trend",
    3,
    "MA20 向上",
    requires=("ma20", "ma20_slope3"),
    order=20,
)
def _ma20_trend(ctx: StockContext, cfg: dict) -> Reason:
    ma20 = ctx.feature("ma20")
    if ma20 is None:
        return "日线不足"
    prev1 = ctx.feature("ma20", 1)
    prev2 = ctx.feature("ma20", 2)
    slope = ctx.feature("ma20_slope3")
    mode = cfg.get("mode", "turn_up")

    if mode == "turn_up":
        turning = prev1 is not None and prev2 is not None and ma20 >= prev1 and prev1 <= prev2
        if not turning and (slope is None or slope <= 0):
            return "MA20 未拐头"
    else:  # rising
        if prev1 is None or ma20 <= prev1 or slope is None or slope <= 0:
            return "MA20 未向上"

    if cfg.get("require_price_above", True) and ctx.price < ma20 * float(
        cfg.get("price_above_tolerance", 0.97)
    ):
        return "价格低于 MA20"
    return None


@hard_filter("ma_alignment", 3, "均线多头排列", requires=("ma5", "ma10", "ma20"), order=30)
def _ma_alignment(ctx: StockContext, cfg: dict) -> Reason:
    ma5, ma10, ma20 = ctx.feature("ma5"), ctx.feature("ma10"), ctx.feature("ma20")
    if None in (ma5, ma10, ma20):
        return "日线不足"
    if ma5 >= ma10 >= ma20:
        return None
    if cfg.get("strict", True):
        return "均线未多头排列"
    return None if ma5 >= ma20 else "MA5 低于 MA20"


# ------------------------------------------------------------------ stage 4
@hard_filter("volume_ratio", 4, "量比", requires=("volume_ratio",), order=10)
def _volume_ratio(ctx: StockContext, cfg: dict) -> Reason:
    ratio = ctx.feature("volume_ratio")
    if ratio is None:
        return "日线不足"
    low = float(cfg.get("min", 1.2))
    high = float(cfg.get("max", 3.0))
    if ratio < low:
        return "量能不足"
    if ratio > high:
        return "量能过热"
    return None


@hard_filter(
    "breakout_20d",
    4,
    "接近/突破 20 日新高",
    requires=("high20_prev",),
    order=20,
)
def _breakout_20d(ctx: StockContext, cfg: dict) -> Reason:
    high20 = ctx.feature("high20_prev") or ctx.feature("high20")
    if high20 is None or high20 <= 0:
        return "日线不足"
    ratio = ctx.price / high20
    if ratio < float(cfg.get("min_ratio", 0.97)):
        return f"距 20 日新高 {ratio * 100:.1f}%"
    return None


# ------------------------------------------------------------------ stage 5
@hard_filter("rsi12", 5, "RSI12 区间", requires=("rsi12",), order=10)
def _rsi12(ctx: StockContext, cfg: dict) -> Reason:
    rsi = ctx.feature("rsi12")
    if rsi is None:
        return "日线不足"
    if not (float(cfg.get("min", 45.0)) <= rsi <= float(cfg.get("max", 70.0))):
        return "RSI 不在区间"
    return None


@hard_filter(
    "macd_positive",
    5,
    "MACD 多头",
    requires=("macd_hist", "macd_dif", "macd_dea"),
    order=20,
)
def _macd_positive(ctx: StockContext, cfg: dict) -> Reason:
    hist = ctx.feature("macd_hist")
    dif = ctx.feature("macd_dif")
    dea = ctx.feature("macd_dea")
    if None in (hist, dif, dea):
        return "日线不足"
    if hist <= 0 or dif <= dea:
        return "MACD 走弱"
    if cfg.get("require_above_zero", False) and dif < 0:
        return "MACD 位于零轴下方"
    return None


@hard_filter("atr_pct", 5, "ATR 波动区间", requires=("atr_pct",), order=30)
def _atr_pct(ctx: StockContext, cfg: dict) -> Reason:
    atr = ctx.feature("atr_pct")
    if atr is None:
        return "日线不足"
    if not (float(cfg.get("min", 1.5)) <= atr <= float(cfg.get("max", 7.0))):
        return "波动异常"
    return None


@hard_filter("max_gain_20d", 5, "20 日涨幅上限", requires=("ret20",), order=40)
def _max_gain_20d(ctx: StockContext, cfg: dict) -> Reason:
    ret20 = ctx.feature("ret20")
    if ret20 is None:
        return "日线不足"
    if ret20 > float(cfg.get("max", 30.0)):
        return "20 日涨幅过大"
    return None


@hard_filter("max_drawdown_20d", 5, "20 日回撤上限", requires=("drawdown20",), order=50)
def _max_drawdown_20d(ctx: StockContext, cfg: dict) -> Reason:
    drawdown = ctx.feature("drawdown20")
    if drawdown is None:
        return "日线不足"
    if drawdown < -abs(float(cfg.get("max", 15.0))):
        return "回撤过深"
    return None


@hard_filter("gap_down", 5, "低开幅度", requires=("gap_pct",), order=60)
def _gap_down(ctx: StockContext, cfg: dict) -> Reason:
    gap = ctx.feature("gap_pct")
    if gap is None:
        return "日线不足"
    if gap < float(cfg.get("max_pct", -5.0)):
        return "大幅低开"
    return None


# ------------------------------------------------------------------ scores
@scorer("trend", "趋势强度", requires=("ma5", "ma10", "ma20", "ma60", "ma20_slope3"), order=10)
def _trend_score(ctx: StockContext, cfg: dict) -> float:
    ma5, ma10, ma20 = ctx.feature("ma5"), ctx.feature("ma10"), ctx.feature("ma20")
    ma60 = ctx.feature("ma60")
    slope = ctx.feature("ma20_slope3") or 0.0
    if None in (ma5, ma10, ma20):
        return 0.0

    score = 0.0
    if ma5 >= ma10 >= ma20:
        score += 35
    elif ma5 >= ma10:
        score += 18
    score += _clamp(slope * 12, 0, 35)
    if ctx.price >= ma20:
        score += 20
    if ma60 and ctx.price >= ma60:
        score += 10
    return _clamp(score)


@scorer("volume_price", "量价配合", requires=("volume_ratio", "high20_prev", "close_position"), order=20)
def _volume_price_score(ctx: StockContext, cfg: dict) -> float:
    ratio = ctx.feature("volume_ratio")
    high20 = ctx.feature("high20_prev") or ctx.feature("high20")
    position = ctx.feature("close_position")
    if ratio is None or high20 is None or high20 <= 0:
        return 0.0

    excellent = float(cfg.get("ideal_volume_ratio", 1.8))
    volume_score = _clamp(100 - abs(ratio - excellent) * 40)
    breakout = (ctx.price / high20 - 1) * 100
    breakout_score = _clamp(breakout * 20, 0, 100)
    close_score = _clamp((position or 0.0) * 100)
    return _clamp(volume_score * 0.4 + breakout_score * 0.35 + close_score * 0.25)


@scorer("momentum", "动量质量", requires=("ret20", "macd_hist", "rsi12"), order=30)
def _momentum_score(ctx: StockContext, cfg: dict) -> float:
    ret20 = ctx.feature("ret20")
    hist = ctx.feature("macd_hist")
    rsi = ctx.feature("rsi12")
    if None in (ret20, hist, rsi) or ctx.price <= 0:
        return 0.0

    ret_score = _clamp(ret20 * 3)
    macd_score = _clamp(50 + hist / ctx.price * 5000)
    rsi_score = _clamp(100 - abs(rsi - 58) * 4)
    return _clamp(ret_score * 0.5 + macd_score * 0.3 + rsi_score * 0.2)


@scorer("relative_strength", "相对强度", requires=("ret20",), order=40)
def _relative_strength_score(ctx: StockContext, cfg: dict) -> float:
    percentile = ctx.extras.get("rs_percentile")
    if isinstance(percentile, (int, float)):
        return _clamp(float(percentile) * 100)
    ret20 = ctx.feature("ret20") or 0.0
    return _clamp(ret20 * 3)


@scorer("volatility", "波动质量", requires=("atr_pct", "drawdown20"), order=50)
def _volatility_score(ctx: StockContext, cfg: dict) -> float:
    atr = ctx.feature("atr_pct")
    drawdown = ctx.feature("drawdown20")
    if atr is None:
        return 0.0
    ideal = float(cfg.get("ideal_atr_pct", 3.0))
    atr_score = _clamp(100 - abs(atr - ideal) * 20)
    drawdown_score = _clamp(100 + (drawdown or 0.0) * 4)
    return _clamp(atr_score * 0.6 + drawdown_score * 0.4)


__all__ = ["FILTERS", "SCORERS", "FilterDef", "ScoreDef", "hard_filter", "scorer"]
