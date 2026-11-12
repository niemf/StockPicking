"""Staged discovery pipeline.

The funnel is intentionally ordered from cheap to expensive:

    1. universe (DB fields only)
    2. price / liquidity (snapshot only)
    3. trend (needs daily bars)
    4. volume + breakout
    5. momentum / volatility / risk
    6. reward-risk gate -> weighted score -> top N

Every stage records how many stocks it rejected and why. A stock that fails an
early test never loads bars nor computes a feature.
"""

from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd
import yaml

from app.analysis.context import StockContext
from app.analysis.factors import FILTERS, SCORERS
from app.analysis.risk import build_plan, rr_bonus

PROFILE_DIR = Path(__file__).resolve().parent / "profiles"
DEFAULT_PROFILE_PATH = PROFILE_DIR / "default.yaml"


# ------------------------------------------------------------------ profile
def _deep_merge(base: dict, override: dict) -> dict:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_profile(path: str | Path | None = None, overrides: dict | None = None) -> dict:
    profile_path = Path(path) if path else DEFAULT_PROFILE_PATH
    with profile_path.open("r", encoding="utf-8") as handle:
        profile = yaml.safe_load(handle) or {}
    if overrides:
        profile = _deep_merge(profile, overrides)
    return profile


# ------------------------------------------------------------------ result types
@dataclass
class StageStat:
    stage: int
    name: str
    input_count: int
    passed: int
    rejections: dict[str, int] = field(default_factory=dict)
    elapsed_ms: float = 0.0

    def as_dict(self) -> dict:
        return {
            "stage": self.stage,
            "name": self.name,
            "input": self.input_count,
            "passed": self.passed,
            "rejections": dict(sorted(self.rejections.items(), key=lambda item: -item[1])),
            "elapsed_ms": round(self.elapsed_ms, 2),
        }


@dataclass
class PipelineResult:
    candidates: list[dict]
    stages: list[StageStat]
    total_input: int
    survivors: int
    elapsed_ms: float

    def summary(self) -> dict:
        return {
            "total_input": self.total_input,
            "survivors": self.survivors,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "stages": [stage.as_dict() for stage in self.stages],
            "candidates": len(self.candidates),
        }


STAGE_NAMES = {
    1: "基础池",
    2: "价格与流动性",
    3: "趋势结构",
    4: "量价与突破",
    5: "动量与风险",
    6: "盈亏比闸门",
}


def _enabled(defs: dict, stage: int, filters_cfg: dict) -> list:
    selected = []
    for definition in defs.values():
        if definition.stage != stage:
            continue
        config = filters_cfg.get(definition.key, {})
        if config.get("enabled", True):
            selected.append(definition)
    return sorted(selected, key=lambda item: (item.order, item.key))


# ------------------------------------------------------------------ pipeline
def run_pipeline(
    contexts: list[StockContext],
    profile: dict,
    *,
    bars_loader=None,
    today: date | None = None,
) -> PipelineResult:
    started = time.perf_counter()
    universe_cfg = profile.get("universe", {})
    filters_cfg = profile.get("filters", {})
    scoring_cfg = profile.get("scoring", {})
    top_n = int(profile.get("output", {}).get("top_n", 30))

    survivors = list(contexts)
    total_input = len(survivors)
    stages: list[StageStat] = []

    for stage in range(1, 6):
        stage_started = time.perf_counter()
        stage_input = len(survivors)
        rejections: dict[str, int] = {}
        stage_defs = _enabled(FILTERS, stage, filters_cfg)

        if stage == 3 and bars_loader is not None:
            waiting = [ctx.code for ctx in survivors if ctx.bars is None]
            if waiting:
                loaded = bars_loader(waiting)
                for ctx in survivors:
                    if ctx.bars is None:
                        ctx.bars = loaded.get(ctx.code)

        kept: list[StockContext] = []
        for ctx in survivors:
            if stage == 3 and (ctx.bars is None or len(ctx.bars) < 60):
                rejections["日线数据不足"] = rejections.get("日线数据不足", 0) + 1
                continue

            reason = None
            for definition in stage_defs:
                config = universe_cfg if definition.key == "universe_basic" else filters_cfg.get(
                    definition.key, {}
                )
                reason = definition.fn(ctx, config)
                if reason:
                    rejections[reason] = rejections.get(reason, 0) + 1
                    break
            if reason is None:
                kept.append(ctx)

        stages.append(
            StageStat(
                stage=stage,
                name=STAGE_NAMES[stage],
                input_count=stage_input,
                passed=len(kept),
                rejections=rejections,
                elapsed_ms=(time.perf_counter() - stage_started) * 1000,
            )
        )
        survivors = kept
        if not survivors:
            break

    # Relative strength is cross-sectional: rank the survivors once.
    if survivors:
        ret20_values = pd.Series(
            [ctx.feature("ret20") if ctx.feature("ret20") is not None else -999 for ctx in survivors]
        )
        percentiles = ret20_values.rank(pct=True)
        for ctx, percentile in zip(survivors, percentiles.tolist()):
            ctx.extras["rs_percentile"] = percentile

    scored: list[tuple[float, float, StockContext, dict[str, float], object]] = []
    weights = {
        key: float(config.get("weight", 0))
        for key, config in scoring_cfg.items()
        if isinstance(config, dict) and float(config.get("weight", 0)) > 0
    }
    total_weight = sum(weights.values()) or 1.0

    for ctx in survivors:
        breakdown: dict[str, float] = {}
        weighted = 0.0
        for key, weight in weights.items():
            definition = SCORERS.get(key)
            if definition is None:
                continue
            config = scoring_cfg.get(key, {})
            score = max(0.0, min(100.0, definition.fn(ctx, config)))
            breakdown[key] = round(score, 2)
            weighted += score * weight
        ctx.extras["score_breakdown"] = breakdown
        scored.append((weighted / total_weight, weighted / total_weight, ctx, breakdown, None))

    # Stage 6: the reward-risk gate. A stock can be strong and still be a bad
    # trade at today's price; this stage rewrites the ranking around "how much
    # do I make if right versus lose if wrong".
    risk_cfg = profile.get("risk", {}) or {}
    if risk_cfg.get("enabled", True) and scored:
        stage_started = time.perf_counter()
        gate_rejections: dict[str, int] = {}
        gated: list[tuple[float, float, StockContext, dict[str, float], object]] = []
        for base_score, _, ctx, breakdown, _plan in scored:
            plan, reason = build_plan(ctx, risk_cfg)
            if plan is None:
                label = reason or "无有效计划"
                gate_rejections[label] = gate_rejections.get(label, 0) + 1
                continue
            bonus = rr_bonus(plan.rr, risk_cfg)
            gated.append((base_score + bonus, base_score, ctx, breakdown, plan))
        stages.append(
            StageStat(
                stage=6,
                name=STAGE_NAMES[6],
                input_count=len(scored),
                passed=len(gated),
                rejections=gate_rejections,
                elapsed_ms=(time.perf_counter() - stage_started) * 1000,
            )
        )
        scored = gated

    scored.sort(key=lambda item: item[0], reverse=True)
    selected = scored[:top_n]

    passed_labels = [
        definition.label
        for stage in range(1, 6)
        for definition in _enabled(FILTERS, stage, filters_cfg)
    ]

    candidates: list[dict] = []
    for rank, (rank_score, base_score, ctx, breakdown, plan) in enumerate(selected, start=1):
        risk = plan.as_dict() if plan is not None else None
        candidates.append(
            {
                "rank": rank,
                "code": ctx.code,
                "name": ctx.name,
                "industry": ctx.meta.industry,
                "price": round(ctx.price, 3),
                "pct_chg": round(ctx.pct_chg, 2),
                "score": round(base_score, 2),
                "rank_score": round(rank_score, 2),
                "rr": round(plan.rr, 2) if plan is not None else None,
                "risk": risk,
                "rule_hits": passed_labels,
                "score_breakdown": breakdown,
                "indicator_snapshot": _snapshot(ctx),
            }
        )

    return PipelineResult(
        candidates=candidates,
        stages=stages,
        total_input=total_input,
        survivors=len(survivors),
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _snapshot(ctx: StockContext) -> dict:
    keys = (
        "ma5",
        "ma10",
        "ma20",
        "ma60",
        "ma20_slope3",
        "volume_ratio",
        "rsi12",
        "macd_hist",
        "atr_pct",
        "ret5",
        "ret20",
        "up_streak",
        "high20_prev",
        "high60_prev",
        "resistance_dist",
        "upper_shadow_pct",
        "drawdown20",
        "close_position",
    )
    snapshot: dict[str, float | None] = {
        key: (round(value, 4) if (value := ctx.feature(key)) is not None else None)
        for key in keys
    }
    snapshot.update(
        {
            "amount": round(ctx.amount, 2),
            "turnover_rate": round(ctx.turnover_rate, 2),
            "listed_days": ctx.listed_days,
        }
    )
    return snapshot


__all__ = [
    "PipelineResult",
    "StageStat",
    "load_profile",
    "run_pipeline",
]
