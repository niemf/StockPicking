"""Trade plan generation, validation, human confirmation and watchlist binding."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Candidate, Stock, TradePlan, Watchlist
from app.demo_data import MARKET_REGIME
from app.services.ai import AICallLog, AIError, DeepSeekClient
from app.services.quotes import QuoteService

PLAN_SYSTEM_PROMPT = """你是 A 股主板交易计划助手。只根据用户提供的结构化数据生成计划，只输出 JSON，不要 Markdown。
硬性规则：
1. 不得编造输入中不存在的数据。
2. 止损价必须低于买入区间下沿；止盈一必须高于买入区间上沿；止盈二不得低于止盈一。
3. 单票仓位 3%–15%；置信度 0–100；风险评分 0–100（越高越危险）。
4. 评级只能是 看多、中性、观察 三者之一。
5. 持有周期写成 “N-M个交易日”。
JSON 字段：
thesis, fundamental_view, technical_view, catalysts, risks,
buy_zone_low, buy_zone_high, stop_loss, take_profit_1, take_profit_2,
position_pct, confidence, holding_horizon, invalid_conditions,
rating, risk_score, tags
其中 catalysts / risks / invalid_conditions / tags 是字符串数组。"""

RATINGS = {"看多", "中性", "观察"}

SNAPSHOT_KEYS = (
    "ma5",
    "ma10",
    "ma20",
    "ma60",
    "volume_ratio",
    "rsi12",
    "macd_hist",
    "atr_pct",
    "ret20",
    "high20_prev",
    "drawdown20",
)


def _compact_snapshot(snapshot: dict | None) -> dict:
    snapshot = snapshot or {}
    result = {key: snapshot.get(key) for key in SNAPSHOT_KEYS if snapshot.get(key) is not None}
    result["volume_ratio"] = snapshot.get("volume_ratio")
    result["atr_pct"] = snapshot.get("atr_pct")
    return result


def build_plan_payload(candidate: Candidate, stock: Stock | None, quote) -> dict:
    snapshot = candidate.indicator_snapshot or {}
    price = quote.price if quote is not None else snapshot.get("price_at_discovery")
    payload = {
        "stock": {
            "code": candidate.code,
            "name": candidate.name,
            "industry": stock.industry if stock else snapshot.get("industry"),
            "price": round(float(price), 3) if price else None,
            "pct_chg": round(float(quote.pct_chg), 2) if quote is not None else snapshot.get("pct_chg_at_discovery"),
        },
        "screen": {
            "score": candidate.score,
            "rank": candidate.rank,
            "score_breakdown": snapshot.get("score_breakdown") or {},
            "indicators": _compact_snapshot(snapshot),
        },
        "market": {
            "regime": MARKET_REGIME.get("label"),
            "position_advice": MARKET_REGIME.get("position_advice"),
        },
        "constraints": {
            "max_position_pct": 15,
            "min_position_pct": 3,
            "min_risk_reward": 1.2,
        },
    }
    return payload


def validate_plan(data: dict, price: float | None) -> tuple[dict, list[str]]:
    """Repair relationships the model got wrong; return notes for the audit log."""
    notes: list[str] = []

    def number(key: str) -> float | None:
        value = data.get(key)
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    base = price or number("buy_zone_high") or 10.0
    buy_low = number("buy_zone_low") or base * 0.98
    buy_high = number("buy_zone_high") or base * 1.02
    if buy_low >= buy_high:
        buy_low, buy_high = min(buy_low, buy_high), max(buy_low, buy_high)
        notes.append("买入区间上下沿已交换")
    if buy_low <= 0:
        buy_low, buy_high = base * 0.98, base * 1.02
        notes.append("买入区间缺失，已按现价生成")

    stop = number("stop_loss") or buy_low * 0.96
    if stop >= buy_low:
        stop = buy_low * 0.96
        notes.append("止损被调整到买入区间下沿之下")
    if stop < buy_low * 0.85:
        stop = buy_low * 0.92
        notes.append("止损距离过远，已收紧")

    tp1 = number("take_profit_1") or buy_high * 1.06
    if tp1 <= buy_high:
        tp1 = buy_high * 1.06
        notes.append("止盈一被调整到买入区间上沿之上")

    entry = (buy_low + buy_high) / 2
    risk = entry - stop
    if risk > 0 and (tp1 - entry) / risk < 1.2:
        tp1 = max(entry + 1.2 * risk, buy_high * 1.01)
        notes.append("止盈一按最小盈亏比 1.2 调整")

    tp2 = number("take_profit_2")
    if tp2 is None or tp2 < tp1:
        tp2 = tp1 * 1.08
        notes.append("止盈二被调整到止盈一之上")

    position = number("position_pct") or 8.0
    position = max(3.0, min(15.0, position))
    confidence = max(0.0, min(100.0, number("confidence") or 60.0))
    risk_score = max(0.0, min(100.0, number("risk_score") or 50.0))

    rating = str(data.get("rating") or "观察")
    if rating not in RATINGS:
        rating = "观察"
        notes.append("评级不在允许集合内，降级为观察")

    def text(key: str, limit: int = 2000) -> str:
        value = data.get(key)
        return str(value).strip()[:limit] if value is not None else ""

    def string_list(key: str) -> list[str]:
        value = data.get(key)
        if not isinstance(value, list):
            return []
        return [str(item).strip()[:120] for item in value if str(item).strip()][:6]

    cleaned = {
        "thesis": text("thesis"),
        "fundamental_view": text("fundamental_view") or None,
        "technical_view": text("technical_view") or None,
        "catalysts": string_list("catalysts") or None,
        "risks": string_list("risks") or None,
        "buy_zone_low": round(buy_low, 3),
        "buy_zone_high": round(buy_high, 3),
        "stop_loss": round(stop, 3),
        "take_profit_1": round(tp1, 3),
        "take_profit_2": round(tp2, 3),
        "position_pct": round(position, 2),
        "confidence": round(confidence, 1),
        "holding_horizon": text("holding_horizon", 32) or None,
        "invalid_conditions": string_list("invalid_conditions") or None,
        "rating": rating,
        "risk_score": round(risk_score, 1),
        "tags": string_list("tags") or None,
    }
    return cleaned, notes


def _next_plan_no(session: Session, trade_date: date, code: str) -> str:
    base = f"PLAN-{trade_date:%Y%m%d}-{code}-AI"
    if session.scalar(select(TradePlan).where(TradePlan.plan_no == base)) is None:
        return base
    for index in range(2, 100):
        candidate = f"{base}-{index}"
        if session.scalar(select(TradePlan).where(TradePlan.plan_no == candidate)) is None:
            return candidate
    raise AIError("无法生成唯一计划编号")


def find_cached_plan(
    session: Session,
    code: str,
    trade_date: date,
    prompt_version: str,
) -> TradePlan | None:
    return session.scalar(
        select(TradePlan)
        .where(
            TradePlan.code == code,
            TradePlan.trade_date == trade_date,
            TradePlan.prompt_version == prompt_version,
            TradePlan.status.in_(["draft", "confirmed"]),
        )
        .order_by(TradePlan.id.desc())
        .limit(1)
    )


def generate_plan(
    session: Session,
    candidate: Candidate,
    *,
    client: DeepSeekClient | None = None,
    quote=None,
    force: bool = False,
    trade_date: date | None = None,
) -> tuple[TradePlan, bool, list[str], AICallLog | None]:
    """Generate one draft plan. Returns ``(plan, cached, notes, log)``."""
    trade_date = trade_date or date.today()
    client = client or DeepSeekClient()
    if not force:
        cached = find_cached_plan(session, candidate.code, trade_date, settings.ai_prompt_version)
        if cached is not None:
            return cached, True, [], None

    if quote is None:
        quote = QuoteService().get_quote(candidate.code)

    stock = session.get(Stock, candidate.code)
    payload = build_plan_payload(candidate, stock, quote)
    if payload["stock"]["price"] is None:
        raise AIError(f"{candidate.code} 没有有效现价，无法生成计划")

    data, log = client.call(
        session,
        purpose="plan",
        prompt_version=settings.ai_prompt_version,
        system=PLAN_SYSTEM_PROMPT,
        user=json.dumps(payload, ensure_ascii=False),
        code=candidate.code,
        max_tokens=1400,
    )
    cleaned, notes = validate_plan(data, payload["stock"]["price"])

    plan = TradePlan(
        plan_no=_next_plan_no(session, trade_date, candidate.code),
        code=candidate.code,
        name=candidate.name,
        source="ai",
        trade_date=trade_date,
        thesis=cleaned["thesis"],
        fundamental_view=cleaned["fundamental_view"],
        technical_view=cleaned["technical_view"],
        catalysts=cleaned["catalysts"],
        risks=cleaned["risks"],
        buy_zone_low=cleaned["buy_zone_low"],
        buy_zone_high=cleaned["buy_zone_high"],
        stop_loss=cleaned["stop_loss"],
        take_profit_1=cleaned["take_profit_1"],
        take_profit_2=cleaned["take_profit_2"],
        position_pct=cleaned["position_pct"],
        confidence=cleaned["confidence"],
        holding_horizon=cleaned["holding_horizon"],
        invalid_conditions=cleaned["invalid_conditions"],
        market_regime=MARKET_REGIME.get("regime"),
        model=log.model,
        prompt_version=settings.ai_prompt_version,
        raw_response=json.dumps(
            {"model_output": data, "validation_notes": notes}, ensure_ascii=False
        ),
        status="draft",
    )
    session.add(plan)
    session.commit()
    return plan, False, notes, log


def generate_plans(
    session: Session,
    *,
    limit: int | None = None,
    force: bool = False,
    client: DeepSeekClient | None = None,
    trade_date: date | None = None,
) -> dict[str, Any]:
    """Generate draft plans for the latest top candidates."""
    trade_date = trade_date or date.today()
    limit = limit or settings.ai_max_plans
    client = client or DeepSeekClient()

    latest_date = session.scalar(
        select(Candidate.trade_date).order_by(Candidate.trade_date.desc()).limit(1)
    )
    if latest_date is None:
        return {"created": 0, "cached": 0, "failed": [], "plans": [], "message": "候选池为空"}

    candidates = list(
        session.scalars(
            select(Candidate)
            .where(Candidate.trade_date == latest_date)
            .order_by(Candidate.rank)
            .limit(limit)
        )
    )

    created = cached = 0
    failed: list[dict] = []
    plans: list[TradePlan] = []
    for candidate in candidates:
        try:
            plan, was_cached, _notes, _log = generate_plan(
                session,
                candidate,
                client=client,
                force=force,
                trade_date=trade_date,
            )
            plans.append(plan)
            cached += int(was_cached)
            created += int(not was_cached)
        except AIError as exc:
            failed.append({"code": candidate.code, "error": str(exc)})
            if "预算" in str(exc) or "DEEPSEEK_API_KEY" in str(exc):
                break
            logger.warning("plan generation failed for {}: {}", candidate.code, exc)

    return {
        "created": created,
        "cached": cached,
        "failed": failed,
        "plans": plans,
        "message": f"生成 {created} 份，缓存 {cached} 份，失败 {len(failed)} 份",
    }


def confirm_plan(session: Session, plan_id: int, *, add_to_watch: bool = True) -> TradePlan:
    plan = session.get(TradePlan, plan_id)
    if plan is None:
        raise AIError("交易计划不存在")
    if plan.status == "confirmed":
        return plan

    plan.status = "confirmed"
    plan.confirmed_at = datetime.now()

    if add_to_watch:
        watch = session.scalar(select(Watchlist).where(Watchlist.code == plan.code))
        if watch is None:
            watch = Watchlist(
                code=plan.code,
                name=plan.name,
                source="ai",
                plan_id=plan.id,
                status="active",
            )
            session.add(watch)
        else:
            watch.plan_id = plan.id
            watch.source = "ai"
            watch.status = "active"

    candidate = session.scalar(
        select(Candidate).where(
            Candidate.trade_date == plan.trade_date, Candidate.code == plan.code
        )
    )
    if candidate is not None:
        candidate.status = "confirmed"

    session.commit()
    return plan


def reject_plan(session: Session, plan_id: int) -> TradePlan:
    plan = session.get(TradePlan, plan_id)
    if plan is None:
        raise AIError("交易计划不存在")
    plan.status = "rejected"
    # Unlink the watch entry so the rejected plan's buy zone / stop stops
    # generating alerts. The stock itself stays in the watchlist.
    linked = list(session.scalars(select(Watchlist).where(Watchlist.plan_id == plan.id)))
    for watch in linked:
        watch.plan_id = None
    candidate = session.scalar(
        select(Candidate).where(
            Candidate.trade_date == plan.trade_date, Candidate.code == plan.code
        )
    )
    if candidate is not None:
        candidate.status = "rejected"
    session.commit()
    return plan


def plan_items(session: Session, *, status: str | None = None, limit: int = 100) -> list[dict]:
    statement = select(TradePlan).where(TradePlan.source == "ai")
    if status:
        statement = statement.where(TradePlan.status == status)
    plans = list(session.scalars(statement.order_by(TradePlan.created_at.desc()).limit(limit)))
    return [
        {
            "id": plan.id,
            "plan_no": plan.plan_no,
            "code": plan.code,
            "name": plan.name,
            "trade_date": plan.trade_date.isoformat(),
            "status": plan.status,
            "rating": plan.rating or "观察",
            "buy_low": plan.buy_zone_low,
            "buy_high": plan.buy_zone_high,
            "stop_loss": plan.stop_loss,
            "take_profit_1": plan.take_profit_1,
            "take_profit_2": plan.take_profit_2,
            "position_pct": plan.position_pct,
            "confidence": plan.confidence,
            "risk_score": plan.risk_score,
            "thesis": plan.thesis,
            "holding_horizon": plan.holding_horizon,
            "created_at": plan.created_at,
        }
        for plan in plans
    ]
