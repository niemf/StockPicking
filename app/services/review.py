"""Daily review: deterministic structured summary plus an optional AI narrative."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import AlertEvent, Candidate, Holding, ReviewReport
from app.demo_data import INDICES, MARKET_REGIME
from app.services.ai import AIError, DeepSeekClient
from app.services.portfolio import holding_items, watch_items
from app.services.quotes import QuoteService

REVIEW_SYSTEM_PROMPT = """你是 A 股复盘助手。只根据输入的结构化数据写复盘，不得编造数据。
只输出 JSON，字段：
market_narrative, watch_narrative, holding_narrative, candidates_narrative,
tomorrow_focus（字符串数组，最多 5 条）。
语言简洁，每段 100–200 字，给出明确的风险提示。"""


def _quote_map(codes: list[str]) -> dict:
    return QuoteService().get_quotes(codes) if codes else {}


def build_review_data(session: Session, trade_date: date | None = None) -> dict[str, Any]:
    trade_date = trade_date or date.today()

    index_quotes = QuoteService().get_index_quotes([row["code"] for row in INDICES])
    indices = []
    for row in INDICES:
        quote = index_quotes.get(row["code"])
        indices.append(
            {
                "name": row["name"],
                "price": round(quote.price, 2) if quote else row["fallback_price"],
                "pct_chg": round(quote.pct_chg, 2) if quote else row["fallback_pct"],
            }
        )

    watches = watch_items(session)
    watch_quotes = _quote_map([row["code"] for row in watches])
    watch_rows = []
    for row in watches:
        quote = watch_quotes.get(row["code"])
        price = quote.price if quote else row["fallback_price"]
        watch_rows.append(
            {
                "code": row["code"],
                "name": row["name"],
                "price": round(price, 2),
                "pct_chg": round(quote.pct_chg if quote else 0.0, 2),
                "has_plan": row["has_plan"],
                "in_buy_zone": bool(
                    row["buy_low"] and row["buy_high"] and row["buy_low"] <= price <= row["buy_high"]
                ),
            }
        )

    holdings = holding_items(session)
    holding_quotes = _quote_map([row["code"] for row in holdings])
    holding_rows = []
    total_value = total_cost = today_pnl = 0.0
    for row in holdings:
        quote = holding_quotes.get(row["code"])
        price = quote.price if quote else row["fallback_price"]
        market_value = price * row["shares"]
        cost_value = row["cost"] * row["shares"]
        pnl = market_value - cost_value
        total_value += market_value
        total_cost += cost_value
        today_pnl += (price - (quote.pre_close if quote else price)) * row["shares"]
        holding_rows.append(
            {
                "code": row["code"],
                "name": row["name"],
                "price": round(price, 2),
                "cost": row["cost"],
                "shares": row["shares"],
                "pnl": round(pnl, 2),
                "pnl_pct": round((price / row["cost"] - 1) * 100, 2) if row["cost"] else 0.0,
                "stop_loss": row["stop_loss"],
            }
        )

    latest = session.scalar(
        select(Candidate.trade_date).order_by(Candidate.trade_date.desc()).limit(1)
    )
    candidate_rows = []
    candidate_total = 0
    if latest is not None:
        candidate_total = int(
            session.scalar(
                select(func.count()).select_from(Candidate).where(Candidate.trade_date == latest)
            )
            or 0
        )
        for row in session.scalars(
            select(Candidate).where(Candidate.trade_date == latest).order_by(Candidate.rank).limit(10)
        ):
            candidate_rows.append(
                {
                    "code": row.code,
                    "name": row.name,
                    "score": row.score,
                    "rank": row.rank,
                }
            )

    day_start = datetime.combine(trade_date, datetime.min.time())
    alerts = list(
        session.scalars(
            select(AlertEvent)
            .where(AlertEvent.trigger_ts >= day_start)
            .order_by(AlertEvent.trigger_ts)
        )
    )
    alert_rows = [
        {
            "code": event.code,
            "rule": (event.condition_snapshot or {}).get("rule_type"),
            "price": event.trigger_price,
            "status": event.status,
        }
        for event in alerts
    ]

    return {
        "trade_date": trade_date.isoformat(),
        "market": {
            "regime": MARKET_REGIME.get("label"),
            "score": MARKET_REGIME.get("score"),
            "advice": MARKET_REGIME.get("position_advice"),
            "indices": indices,
        },
        "watchlist": watch_rows,
        "holdings": holding_rows,
        "holding_totals": {
            "market_value": round(total_value, 2),
            "pnl": round(total_value - total_cost, 2),
            "pnl_pct": round((total_value / total_cost - 1) * 100, 2) if total_cost else 0.0,
            "today_pnl": round(today_pnl, 2),
        },
        "candidates": candidate_rows,
        "candidate_total": candidate_total,
        "alerts": alert_rows,
    }


def _summaries(data: dict) -> dict[str, str]:
    indices = data["market"]["indices"]
    index_text = "，".join(f"{item['name']} {item['pct_chg']:+.2f}%" for item in indices)
    market_summary = (
        f"市场风格：{data['market']['regime']}（{data['market']['score']}）。{index_text}。"
        f"{data['market']['advice']}"
    )

    watches = data["watchlist"]
    in_zone = [row["name"] for row in watches if row["in_buy_zone"]]
    watch_summary = f"自选 {len(watches)} 只，进入建仓区 {len(in_zone)} 只"
    if in_zone:
        watch_summary += "：" + "、".join(in_zone[:5])
    watch_summary += "。"

    totals = data["holding_totals"]
    holding_summary = (
        f"持仓 {len(data['holdings'])} 只，市值 {totals['market_value']:,.2f} 元，"
        f"浮动盈亏 {totals['pnl']:+,.2f} 元（{totals['pnl_pct']:+.2f}%），"
        f"今日盈亏 {totals['today_pnl']:+,.2f} 元。"
    )

    candidates = data["candidates"]
    if candidates:
        top = "、".join(f"{row['name']}（{row['score']:.1f}）" for row in candidates[:3])
        candidates_summary = f"候选池 {data.get('candidate_total', len(candidates))} 只，评分前三：{top}。"
    else:
        candidates_summary = "今日无候选。"
    return {
        "market_summary": market_summary,
        "watch_summary": watch_summary,
        "holding_summary": holding_summary,
        "candidates_summary": candidates_summary,
    }


def generate_review(
    session: Session,
    *,
    trade_date: date | None = None,
    use_ai: bool = True,
    client: DeepSeekClient | None = None,
    force: bool = False,
) -> ReviewReport:
    trade_date = trade_date or date.today()
    existing = session.scalar(select(ReviewReport).where(ReviewReport.trade_date == trade_date))
    if existing is not None and existing.ai_narrative and not force:
        return existing

    data = build_review_data(session, trade_date)
    summaries = _summaries(data)
    ai_narrative = None
    raw: dict[str, Any] = {"data": data, "summaries": summaries}

    client = client or DeepSeekClient()
    if use_ai and not client.api_key:
        use_ai = False

    if use_ai:
        payload = {
            "trade_date": data["trade_date"],
            "market": data["market"],
            "holding_totals": data["holding_totals"],
            "watchlist": data["watchlist"][:10],
            "holdings": data["holdings"],
            "candidates": data["candidates"],
            "alerts": data["alerts"][:10],
        }
        try:
            result, log = client.call(
                session,
                purpose="review",
                prompt_version=settings.ai_review_prompt_version,
                system=REVIEW_SYSTEM_PROMPT,
                user=json.dumps(payload, ensure_ascii=False),
                code=None,
                max_tokens=1200,
            )
            ai_narrative = str(result.get("market_narrative") or "").strip()
            raw["ai"] = result
            raw["usage"] = {
                "input_tokens": log.input_tokens,
                "output_tokens": log.output_tokens,
                "cost": log.cost,
            }
        except AIError as exc:
            raw["ai_error"] = str(exc)
            logger.warning("review AI skipped: {}", exc)

    report = existing or ReviewReport(trade_date=trade_date)
    report.market_summary = summaries["market_summary"]
    report.watch_summary = summaries["watch_summary"]
    report.holding_summary = summaries["holding_summary"]
    report.candidates_summary = summaries["candidates_summary"]
    report.ai_narrative = ai_narrative
    report.raw = raw
    if existing is None:
        session.add(report)
    session.commit()
    return report


def recent_reports(session: Session, limit: int = 30) -> list[ReviewReport]:
    return list(
        session.scalars(
            select(ReviewReport).order_by(ReviewReport.trade_date.desc()).limit(limit)
        )
    )


__all__ = ["build_review_data", "generate_review", "recent_reports"]
