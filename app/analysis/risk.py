"""Reward-risk gate for discovery candidates.

The funnel can already find "strong" stocks. This module answers a different
question: **at the current price, is the trade worth taking?**

A stock 14% above its MA20 with the prior high 1% overhead is strong, but both
the stop and the target are wrong, so it must not be recommended. The numbers
here are deterministic — daily bars plus the live quote, no model call.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from app.analysis.context import StockContext


@dataclass(frozen=True)
class RiskPlan:
    """Concrete entry / stop / target for one candidate."""

    entry: float
    stop: float
    target1: float
    target2: float
    rr: float
    stop_pct: float
    upside_pct: float
    resistance: float
    atr: float
    atr_pct: float

    def as_dict(self) -> dict:
        return {key: round(value, 3) for key, value in asdict(self).items()}


def _nearest_level(ctx: StockContext, price: float, floor_pct: float) -> float | None:
    """Closest overhead level that is at least ``floor_pct`` above the price."""
    levels = [
        level
        for level in (ctx.feature("high20_prev"), ctx.feature("high60_prev"))
        if level and level > price * (1 + floor_pct / 100)
    ]
    return min(levels) if levels else None


def build_plan(
    ctx: StockContext,
    cfg: dict | None = None,
) -> tuple[RiskPlan | None, str | None]:
    """Build a trade plan and return ``(plan, rejection_reason)``.

    Rejections are intentionally blunt: no plan is better than a plan with a
    wide stop or no room to the next resistance.
    """
    cfg = cfg or {}
    price = float(ctx.price or 0.0)
    atr = ctx.feature("atr14")
    atr_pct = ctx.feature("atr_pct")
    ma20 = ctx.feature("ma20")
    if price <= 0 or not atr or atr <= 0 or atr_pct is None:
        return None, "缺少波动数据"

    atr_mult = float(cfg.get("atr_mult", 1.2))
    ma20_buffer = float(cfg.get("ma20_buffer", 0.01))
    min_stop_pct = float(cfg.get("min_stop_pct", 1.5))
    max_stop_pct = float(cfg.get("max_stop_pct", 8.0))
    min_upside_pct = float(cfg.get("min_upside_pct", 3.0))
    min_rr = float(cfg.get("min_reward_risk", 2.0))
    max_target_pct = float(cfg.get("max_target_pct", 25.0))

    # Structural stop: below MA20 (thesis invalidated) or 1.2 ATR, whichever is
    # farther away. Chasing a stock far above MA20 therefore produces a wide
    # stop and is rejected instead of being bought with a tight noise stop.
    stops = [price - atr_mult * atr]
    if ma20:
        stops.append(ma20 * (1 - ma20_buffer))
    stop = min(stops)
    if stop <= 0 or stop >= price:
        return None, "止损无效"
    stop_pct = (price - stop) / price * 100
    if stop_pct > max_stop_pct:
        return None, f"止损过宽 {stop_pct:.1f}%"
    if stop_pct < min_stop_pct:
        return None, "止损过近"

    # A level right above the price caps the upside no matter how strong the
    # trend looks; the measured move is only used when there is open space.
    real_level = _nearest_level(ctx, price, min_upside_pct)
    range_pct = ctx.feature("range20_pct")
    measured_pct = max(range_pct or 0.0, atr_pct * 2.5, min_upside_pct)
    measured_pct = min(measured_pct, max_target_pct)
    if real_level is not None:
        upside_pct = (real_level / price - 1) * 100
        target1 = real_level
        resistance = real_level
    else:
        upside_pct = measured_pct
        target1 = price * (1 + measured_pct / 100)
        resistance = target1

    if upside_pct < min_upside_pct:
        return None, f"上方空间不足 {upside_pct:.1f}%"
    if upside_pct > max_target_pct:
        upside_pct = max_target_pct
        target1 = price * (1 + upside_pct / 100)
        resistance = target1

    rr = upside_pct / stop_pct
    if rr < min_rr:
        return None, f"盈亏比不足 {rr:.1f}"

    target2 = price + (target1 - price) * float(cfg.get("target2_extension", 1.618))
    return (
        RiskPlan(
            entry=price,
            stop=stop,
            target1=target1,
            target2=target2,
            rr=rr,
            stop_pct=stop_pct,
            upside_pct=upside_pct,
            resistance=resistance,
            atr=atr,
            atr_pct=atr_pct,
        ),
        None,
    )


def rr_bonus(rr: float, cfg: dict | None = None) -> float:
    """Small ranking bonus for a better reward-risk ratio (0..cap)."""
    cfg = cfg or {}
    min_rr = float(cfg.get("min_reward_risk", 2.0))
    cap = float(cfg.get("rr_bonus_cap", 10.0))
    spread = max(0.5, 4.0 - min_rr)
    return max(0.0, min(cap, (rr - min_rr) / spread * cap))


__all__ = ["RiskPlan", "build_plan", "rr_bonus"]
