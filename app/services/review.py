"""Daily review: market, watchlist, holdings, sector flow, LHB and operations.

Structured numbers come from local data; AI only writes the narrative. The
candidate pool is intentionally excluded from this report.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import AlertEvent, Holding, HoldingTransaction, ReviewReport
from app.demo_data import INDICES, MARKET_REGIME
from app.services.ai import AIError, DeepSeekClient
from app.services.market_flow import get_lhb, get_sector_flow
from app.services.portfolio import holding_items, watch_items
from app.services.quotes import QuoteService

REVIEW_SYSTEM_PROMPT = """你是 A 股复盘助手。只根据输入的结构化数据写复盘，不得编造数据。
只输出 JSON，字段：
market_narrative, watch_narrative, holding_narrative, sector_narrative,
lhb_narrative, operation_narrative,
tomorrow_focus（字符串数组，最多 5 条）。
语言简洁，每段 100–200 字；重点说明资金流向、个人持仓风险与明日操作，不推荐候选股。"""


def _quote_map(codes: list[str]) -> dict:
    return QuoteService().get_quotes(codes) if codes else {}


def _suggestion(row: dict, price: float) -> str:
    if row.get("available_shares") == 0:
        return "今日买入，T+1 明日才可卖出，先按计划观察"
    stop = row.get("stop_loss")
    take_profit = row.get("take_profit_1")
    pnl_pct = row.get("pnl_pct") or 0.0
    if stop and price <= float(stop) * 1.02:
        return "接近止损，明日优先减仓或离场"
    if take_profit and price >= float(take_profit):
        return "到达止盈一，考虑分批止盈并上移止损"
    if pnl_pct >= 8:
        return "浮盈较厚，止损上移至成本价保护利润"
    if pnl_pct <= -5:
        return "浮亏扩大，检查 MA20 与量能，必要时减仓"
    return "持有观察，按计划止损"


def _personal_operations(session: Session, trade_date: date, holdings: list[dict]) -> dict:
    rows = list(
        session.execute(
            select(HoldingTransaction, Holding)
            .join(Holding, Holding.id == HoldingTransaction.holding_id)
            .where(HoldingTransaction.trade_date == trade_date)
            .order_by(HoldingTransaction.id)
        ).all()
    )
    trades = []
    realized = 0.0
    buy_amount = 0.0
    for transaction, holding in rows:
        amount = transaction.price * transaction.shares
        pnl = None
        if transaction.action == "sell":
            pnl = (transaction.price - holding.cost_price) * transaction.shares - transaction.fee
            realized += pnl
        else:
            buy_amount += amount
        trades.append(
            {
                "code": holding.code,
                "name": holding.name,
                "action": "买入" if transaction.action == "buy" else "卖出",
                "price": round(transaction.price, 3),
                "shares": transaction.shares,
                "amount": round(amount, 2),
                "fee": round(transaction.fee, 2),
                "pnl": round(pnl, 2) if pnl is not None else None,
            }
        )

    suggestions = [
        {
            "code": row["code"],
            "name": row["name"],
            "price": row["price"],
            "pnl_pct": row["pnl_pct"],
            "suggestion": _suggestion(row, row["price"]),
        }
        for row in holdings
    ]
    return {
        "trades": trades,
        "buy_count": sum(1 for item in trades if item["action"] == "买入"),
        "sell_count": sum(1 for item in trades if item["action"] == "卖出"),
        "realized_pnl": round(realized, 2),
        "buy_amount": round(buy_amount, 2),
        "suggestions": suggestions,
    }


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
                "available_shares": row["available_shares"],
                "pnl": round(pnl, 2),
                "pnl_pct": round((price / row["cost"] - 1) * 100, 2) if row["cost"] else 0.0,
                "stop_loss": row["stop_loss"],
                "take_profit_1": row["take_profit_1"],
                "suggestion": _suggestion(row, price),
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

    sectors = get_sector_flow(trade_date)
    lhb = get_lhb(trade_date)

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
        "sectors": sectors,
        "lhb": lhb,
        "operations": _personal_operations(session, trade_date, holding_rows),
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

    sectors = data["sectors"]
    if sectors.get("available"):
        inflow = "、".join(
            f"{item['name']} {item['net_inflow'] / 1e8:+.2f}亿" for item in sectors["inflow"][:3]
        )
        outflow = "、".join(
            f"{item['name']} {item['net_inflow'] / 1e8:+.2f}亿" for item in sectors["outflow"][:3]
        )
        sector_summary = f"资金流入前三：{inflow}；流出前三：{outflow}。"
    else:
        top = "、".join(
            f"{item['name']} {item['pct_chg']:+.2f}%" for item in sectors.get("performance_top", [])[:3]
        )
        bottom = "、".join(
            f"{item['name']} {item['pct_chg']:+.2f}%" for item in sectors.get("performance_bottom", [])[:3]
        )
        sector_summary = f"资金流数据暂不可用；板块涨幅前三：{top}；跌幅前三：{bottom}。"

    lhb = data["lhb"]
    if lhb.get("available"):
        names = "、".join(item["name"] for item in lhb["net_buy"][:3])
        lhb_summary = f"龙虎榜 {lhb['total']} 只，净买入靠前：{names}。"
    else:
        lhb_summary = "龙虎榜数据暂不可用。"

    operations = data["operations"]
    operation_summary = (
        f"今日买入 {operations['buy_count']} 笔、卖出 {operations['sell_count']} 笔，"
        f"已实现盈亏 {operations['realized_pnl']:+,.2f} 元。"
    )

    return {
        "market_summary": market_summary,
        "watch_summary": watch_summary,
        "holding_summary": holding_summary,
        "sector_summary": sector_summary,
        "lhb_summary": lhb_summary,
        "operation_summary": operation_summary,
    }


def _ai_narrative_from(result: dict) -> str:
    parts = [
        ("大盘", result.get("market_narrative")),
        ("自选", result.get("watch_narrative")),
        ("持仓", result.get("holding_narrative")),
        ("板块资金", result.get("sector_narrative")),
        ("龙虎榜", result.get("lhb_narrative")),
        ("个人操作", result.get("operation_narrative")),
    ]
    text = "\n\n".join(f"【{label}】{value}" for label, value in parts if value)
    focus = result.get("tomorrow_focus")
    if isinstance(focus, list) and focus:
        text += "\n\n【明日关注】" + "；".join(str(item) for item in focus[:5])
    return text


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
    data["summaries"] = summaries
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
            "sectors": data["sectors"],
            "lhb": data["lhb"],
            "operations": data["operations"],
        }
        try:
            result, log = client.call(
                session,
                purpose="review",
                prompt_version=settings.ai_review_prompt_version,
                system=REVIEW_SYSTEM_PROMPT,
                user=json.dumps(payload, ensure_ascii=False),
                code=None,
                max_tokens=1400,
            )
            ai_narrative = _ai_narrative_from(result)
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
    report.candidates_summary = None
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
