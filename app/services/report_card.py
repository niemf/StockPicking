"""Structured per-stock AI report card.

The trade plan answers "how do I trade this"; the report card answers "what is
this stock doing". It is a separate artefact on purpose:

* one row per ``(trade_date, code, prompt_version)`` so re-running is idempotent;
* numbers (levels, score) come from the model but are validated and clamped
  against the real price, so a bad answer cannot produce a nonsense level;
* the deterministic reference levels (20-day low/high, MA20, ATR) are always
  shown to the model, and kept in ``raw`` for auditing.

Like the plan module, everything here is best-effort: a failed call raises
``AIError`` and leaves the previous card untouched.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pandas as pd
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.context import StockContext, StockMeta
from app.config import settings
from app.db.models import Stock, StockReport, Watchlist
from app.demo_data import MARKET_REGIME
from app.providers.base import Quote
from app.services.ai import AIError, AICallLog, DeepSeekClient
from app.services.discovery import load_bars_map
from app.services.quotes import QuoteService

REPORT_SYSTEM_PROMPT = """你是 A 股主板个股研究员。只根据用户提供的结构化数据写一张研报卡，只输出 JSON，不要 Markdown。
硬性规则：
1. 不得编造输入中不存在的事实（业绩、订单、政策、传闻）。没有依据就写“数据不足”。
2. conclusion 只能是 看多、中性、回避 之一；score 是 0–100 的综合评分（越高越值得关注）。
3. trend 只能是 上升、震荡、下降 之一。
4. support 必须低于现价，resistance 必须高于现价，invalidation 不高于 support。
5. risks / catalysts / checklist 是字符串数组，每项不超过 40 字；checklist 是 2–5 条可执行动作，至少一条说明“跌破 support 怎么办”。
6. summary 不超过 300 字，只讨论量价与趋势，不承诺收益、不给出目标收益率。
JSON 字段：
conclusion, score, trend, trend_note, support, resistance, invalidation, summary, risks, catalysts, checklist"""

CONCLUSIONS = ("看多", "中性", "回避")
TRENDS = ("上升", "震荡", "下降")

CONCLUSION_ALIASES = {
    "看多": "看多",
    "买入": "看多",
    "看好": "看多",
    "增持": "看多",
    "中性": "中性",
    "观察": "中性",
    "持有": "中性",
    "回避": "回避",
    "卖出": "回避",
    "看空": "回避",
    "减持": "回避",
}

TREND_ALIASES = {
    "上升": "上升",
    "上涨": "上升",
    "多头": "上升",
    "up": "上升",
    "震荡": "震荡",
    "横盘": "震荡",
    "整理": "震荡",
    "下降": "下降",
    "下跌": "下降",
    "空头": "下降",
    "down": "下降",
}

FEATURE_KEYS = (
    "ma5",
    "ma10",
    "ma20",
    "ma60",
    "ma20_slope3",
    "boll_lower",
    "boll_upper",
    "high20",
    "high20_prev",
    "rsi12",
    "macd_hist",
    "atr_pct",
    "ret20",
    "drawdown20",
    "volume_ratio",
)

SCORE_LABELS = (
    (80, "强势"),
    (65, "偏强"),
    (45, "中性"),
    (30, "偏弱"),
    (0, "弱势"),
)


# ------------------------------------------------------------------- helpers
def _num(value: Any, digits: int = 3) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return round(number, digits)


def _tech_snapshot(bars: pd.DataFrame, quote: Quote | None, meta: StockMeta) -> dict[str, Any]:
    context = StockContext(meta=meta, quote=quote, bars=bars)
    tech: dict[str, Any] = {}
    for key in FEATURE_KEYS:
        try:
            value = context.feature(key)
        except Exception:  # noqa: BLE001 - a single feature must not kill the card
            value = None
        number = _num(value, 4)
        if number is not None:
            tech[key] = number
    if not bars.empty:
        tail = bars.tail(20)
        tech["low20"] = _num(tail["low"].min())
        tech["high20_bars"] = _num(tail["high"].max())
        tech["bars"] = int(len(bars))
        tech["last_bar_date"] = str(tail["trade_date"].iloc[-1])[:10]
    return tech


def _plan_facts(session: Session, code: str) -> dict[str, Any] | None:
    watch = session.scalar(
        select(Watchlist).where(Watchlist.code == code, Watchlist.status == "active")
    )
    plan = watch.plan if watch is not None else None
    if plan is None:
        return None
    return {
        "status": plan.status,
        "rating": plan.rating,
        "buy_zone_low": _num(plan.buy_zone_low),
        "buy_zone_high": _num(plan.buy_zone_high),
        "stop_loss": _num(plan.stop_loss),
        "take_profit_1": _num(plan.take_profit_1),
        "thesis": (plan.thesis or "")[:200],
    }


def build_payload(
    session: Session,
    code: str,
    *,
    stock: Stock | None = None,
    bars: pd.DataFrame | None = None,
    quote: Quote | None = None,
) -> dict[str, Any]:
    """Assemble the compact, deterministic input for one stock."""
    stock = stock or session.get(Stock, code)
    name = stock.name if stock is not None else code
    industry = stock.industry if stock is not None else None
    meta = StockMeta(
        code=code,
        name=name,
        industry=industry,
        list_date=stock.list_date if stock is not None else None,
        is_st=bool(stock.is_st) if stock is not None else False,
        is_delisting_risk=bool(stock.is_delisting_risk) if stock is not None else False,
        status=stock.status if stock is not None else "normal",
    )

    if bars is None:
        bars = load_bars_map(session, [code]).get(code)
    if bars is None or bars.empty or len(bars) < 30:
        raise AIError(f"{code} 日线不足 30 根，先运行 scripts/backfill_bars.py --code {code}")
    bars = bars.copy()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"]).dt.date

    if quote is None:
        quote = QuoteService().get_quote(code)
    price = _num(quote.price if quote is not None else bars["close"].iloc[-1], 3)
    if not price or price <= 0:
        raise AIError(f"{code} 没有有效现价，无法生成研报卡")

    tech = _tech_snapshot(bars, quote, meta)
    return {
        "stock": {
            "code": code,
            "name": name,
            "industry": industry,
            "price": price,
            "pct_chg": _num(quote.pct_chg, 2) if quote is not None else None,
            "amount_yi": _num(quote.amount / 1e8, 2) if quote is not None else None,
            "turnover_rate": _num(quote.turnover_rate, 2) if quote is not None else None,
            "is_st": meta.is_st,
        },
        "tech": tech,
        "levels_ref": {
            "support_ref": tech.get("low20") or tech.get("boll_lower"),
            "resistance_ref": tech.get("high20_bars") or tech.get("high20_prev"),
            "ma20": tech.get("ma20"),
            "note": "参考位由最近 20 日高低点与布林带计算，不是必须照抄",
        },
        "plan": _plan_facts(session, code),
        "market": {
            "regime": MARKET_REGIME.get("label"),
            "position_advice": MARKET_REGIME.get("position_advice"),
        },
        "constraints": {
            "conclusions": list(CONCLUSIONS),
            "trends": list(TRENDS),
            "score_range": [0, 100],
            "support_below_price": True,
            "resistance_above_price": True,
        },
    }


def validate_report(
    data: dict[str, Any],
    price: float | None,
    *,
    tech: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Clamp model output into something safe to render; return audit notes."""
    notes: list[str] = []
    tech = tech or {}

    raw_conclusion = str(data.get("conclusion") or "").strip()
    conclusion = CONCLUSION_ALIASES.get(raw_conclusion, "中性")
    if raw_conclusion and conclusion != raw_conclusion:
        notes.append(f"结论 {raw_conclusion} → {conclusion}")
    if not raw_conclusion:
        notes.append("缺少结论，默认中性")

    try:
        score = int(round(float(data.get("score"))))
    except (TypeError, ValueError):
        score = 50
        notes.append("评分缺失，默认 50")
    if score < 0 or score > 100:
        score = max(0, min(100, score))
        notes.append("评分夹取到 0–100")

    raw_trend = str(data.get("trend") or "").strip()
    trend = TREND_ALIASES.get(raw_trend, "")
    if not trend:
        for alias, value in TREND_ALIASES.items():
            if alias and alias in raw_trend:
                trend = value
                break
    if not trend:
        trend = "震荡"
        notes.append("趋势缺失，默认震荡")

    def level(key: str) -> float | None:
        return _num(data.get(key), 3)

    support = level("support")
    resistance = level("resistance")
    invalidation = level("invalidation")

    if price and price > 0:
        if support is None or support >= price:
            fallback = tech.get("low20") or tech.get("boll_lower") or price * 0.95
            support = _num(fallback, 3)
            notes.append("支撑位缺失或不低于现价，改用 20 日低点/布林下轨")
        if support and support >= price:
            support = _num(price * 0.95, 3)
        if resistance is None or resistance <= price:
            fallback = tech.get("high20_bars") or tech.get("high20_prev") or price * 1.05
            resistance = _num(fallback, 3)
            notes.append("压力位缺失或不高于现价，改用 20 日高点")
        if resistance and resistance <= price:
            resistance = _num(price * 1.05, 3)
        if invalidation is not None and support is not None and invalidation > support:
            invalidation = _num(support * 0.995, 3)
            notes.append("失效位高于支撑位，下移到支撑位下方")

    def text_list(key: str, limit: int, max_items: int) -> list[str]:
        raw = data.get(key)
        if isinstance(raw, str):
            raw = [part for part in raw.replace("；", ";").split(";")]
        if not isinstance(raw, list):
            return []
        cleaned: list[str] = []
        for item in raw:
            text = str(item or "").strip()
            text = " ".join(text.split())
            if not text or text in cleaned:
                continue
            cleaned.append(text[:limit])
        if len(cleaned) > max_items:
            notes.append(f"{key} 超过 {max_items} 条，已截断")
        return cleaned[:max_items]

    risks = text_list("risks", 60, 5)
    catalysts = text_list("catalysts", 60, 5)
    checklist = text_list("checklist", 60, 5)
    if not risks:
        risks = ["数据不足，注意大盘系统性风险"]
    if not checklist:
        checklist = ["跌破支撑位先减仓，不补仓摊低成本"]

    summary = " ".join(str(data.get("summary") or "").split())
    if len(summary) > 300:
        summary = summary[:300]
        notes.append("摘要超过 300 字，已截断")
    if not summary:
        summary = "模型未给出摘要，请结合图表与成交量自行判断。"
        notes.append("摘要为空，使用兜底文案")

    trend_note = " ".join(str(data.get("trend_note") or "").split())[:120] or None

    return (
        {
            "conclusion": conclusion,
            "score": score,
            "trend": trend,
            "trend_note": trend_note,
            "support": support,
            "resistance": resistance,
            "invalidation": invalidation,
            "summary": summary,
            "risks": risks,
            "catalysts": catalysts,
            "checklist": checklist,
        },
        notes,
    )


def find_cached_report(
    session: Session,
    code: str,
    trade_date: date,
    prompt_version: str,
) -> StockReport | None:
    return session.scalar(
        select(StockReport).where(
            StockReport.code == code,
            StockReport.trade_date == trade_date,
            StockReport.prompt_version == prompt_version,
        )
    )


def latest_report(session: Session, code: str) -> StockReport | None:
    return session.scalar(
        select(StockReport)
        .where(StockReport.code == code)
        .order_by(StockReport.trade_date.desc(), StockReport.id.desc())
        .limit(1)
    )


def generate_report(
    session: Session,
    code: str,
    *,
    client: DeepSeekClient | None = None,
    force: bool = False,
    trade_date: date | None = None,
    quote: Quote | None = None,
) -> tuple[StockReport, bool, list[str], AICallLog | None]:
    """Generate (or refresh) one report card. Returns ``(row, cached, notes, log)``."""
    trade_date = trade_date or date.today()
    client = client or DeepSeekClient()

    if not force:
        cached = find_cached_report(session, code, trade_date, settings.ai_prompt_version)
        if cached is not None:
            return cached, True, [], None

    payload = build_payload(session, code, quote=quote)
    price = payload["stock"]["price"]

    data, log = client.call(
        session,
        purpose="report",
        prompt_version=settings.ai_prompt_version,
        system=REPORT_SYSTEM_PROMPT,
        user=json.dumps(payload, ensure_ascii=False),
        code=code,
        max_tokens=1200,
    )
    cleaned, notes = validate_report(data, price, tech=payload["tech"])

    row = find_cached_report(session, code, trade_date, settings.ai_prompt_version)
    if row is None:
        row = StockReport(
            code=code,
            name=payload["stock"]["name"],
            trade_date=trade_date,
            prompt_version=settings.ai_prompt_version,
        )
        session.add(row)

    row.conclusion = cleaned["conclusion"]
    row.score = cleaned["score"]
    row.trend = cleaned["trend"]
    row.trend_note = cleaned["trend_note"]
    row.support = cleaned["support"]
    row.resistance = cleaned["resistance"]
    row.invalidation = cleaned["invalidation"]
    row.summary = cleaned["summary"]
    row.risks = cleaned["risks"]
    row.catalysts = cleaned["catalysts"]
    row.checklist = cleaned["checklist"]
    row.model = log.model
    row.prompt_version = settings.ai_prompt_version
    row.raw = {
        "model_output": data,
        "validation_notes": notes,
        "payload": payload,
    }
    session.commit()
    logger.info("report card {} {}: {} {}", code, trade_date, row.conclusion, row.score)
    return row, False, notes, log


# --------------------------------------------------------------------- views
def score_label(score: int) -> str:
    for threshold, label in SCORE_LABELS:
        if score >= threshold:
            return label
    return "弱势"


def report_dict(row: StockReport) -> dict[str, Any]:
    conclusion_class = {"看多": "up", "回避": "down"}.get(row.conclusion, "flat")
    return {
        "id": row.id,
        "code": row.code,
        "name": row.name,
        "trade_date": row.trade_date.isoformat() if row.trade_date else None,
        "conclusion": row.conclusion,
        "conclusion_class": conclusion_class,
        "badge": {"看多": "success", "回避": "danger"}.get(row.conclusion, "warning"),
        "score": row.score,
        "score_label": score_label(row.score),
        "trend": row.trend,
        "trend_note": row.trend_note,
        "support": row.support,
        "resistance": row.resistance,
        "invalidation": row.invalidation,
        "summary": row.summary,
        "risks": row.risks or [],
        "catalysts": row.catalysts or [],
        "checklist": row.checklist or [],
        "model": row.model,
        "created_at": row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else None,
    }


def recent_cards(session: Session, limit: int = 20) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(StockReport).order_by(StockReport.created_at.desc()).limit(limit)
    )
    return [report_dict(row) for row in rows]


def latest_reports_for(session: Session, codes: list[str]) -> dict[str, dict[str, Any]]:
    """Latest report card per code, for the watchlist cards."""
    if not codes:
        return {}
    rows = session.scalars(
        select(StockReport)
        .where(StockReport.code.in_(codes))
        .order_by(StockReport.created_at.desc())
    )
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.code not in result:
            result[row.code] = report_dict(row)
    return result


__all__ = [
    "CONCLUSIONS",
    "REPORT_SYSTEM_PROMPT",
    "TRENDS",
    "build_payload",
    "find_cached_report",
    "generate_report",
    "latest_report",
    "latest_reports_for",
    "recent_cards",
    "report_dict",
    "score_label",
    "validate_report",
]
