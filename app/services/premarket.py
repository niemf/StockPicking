"""Pre-market briefing (09:15, after the auction opens).

Three inputs, all local and AI-free:

* yesterday's review (market / sector / 龙虎榜 summaries already stored);
* the active watchlist with confirmed plans (buy zone, stop, take profit);
* the last stored close for each watch item, used to judge the distance to the
  buy zone before the open.

The briefing is a plain list of sections so the same text can be printed by the
CLI, returned by the API and pushed to Feishu without duplicating formatting.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from loguru import logger
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import DailyBar, ReviewReport
from app.demo_data import MARKET_REGIME
from app.services.calendar import is_trading_day
from app.services.notifier import Notifier
from app.services.portfolio import watch_items

WEEKDAYS = "一二三四五六日"

DISCIPLINE = (
    "不追高开，等回踩或放量确认再动手",
    "跌破计划止损直接执行，不在盘中改计划",
    "单票不超过 15%，总仓位跟随市场风格",
)


def _last_closes(session: Session, codes: list[str]) -> dict[str, float]:
    if not codes:
        return {}
    latest = (
        select(DailyBar.code, func.max(DailyBar.trade_date).label("day"))
        .where(DailyBar.code.in_(codes))
        .group_by(DailyBar.code)
        .subquery()
    )
    rows = session.execute(
        select(DailyBar.code, DailyBar.close).join(
            latest,
            and_(DailyBar.code == latest.c.code, DailyBar.trade_date == latest.c.day),
        )
    ).all()
    return {code: float(close) for code, close in rows if close is not None}


def _review_lines(report: ReviewReport | None, day: date) -> list[str]:
    if report is None:
        return [
            f"- 未找到今日之前的复盘记录；当前市场风格：{MARKET_REGIME.get('label')}"
            f"（{MARKET_REGIME.get('position_advice')}）。"
        ]

    summaries = (report.raw or {}).get("summaries") or {}
    candidates = [
        summaries.get("market_summary") or report.market_summary,
        summaries.get("sector_summary"),
        summaries.get("lhb_summary"),
        summaries.get("watch_summary") or report.watch_summary,
    ]
    lines = [f"- {text.strip()}" for text in candidates if text and str(text).strip()]
    if not lines:
        lines = ["- 昨日复盘没有可读摘要，按既定计划执行，不临时加仓。"]
    return lines[:3]


def _zone_item(row: dict[str, Any], close: float | None) -> dict[str, Any] | None:
    buy_low = row.get("buy_low")
    buy_high = row.get("buy_high")
    if not buy_low or not buy_high:
        return None
    price = close or row.get("fallback_price")
    if not price:
        return None

    if price > buy_high:
        zone_status = "高于建仓区"
        distance = (price - buy_high) / buy_high * 100
    elif price < buy_low:
        zone_status = "低于建仓区"
        distance = (price - buy_low) / buy_low * 100
    else:
        zone_status = "进入建仓区"
        distance = 0.0

    return {
        "code": row["code"],
        "name": row["name"],
        "price": round(float(price), 2),
        "buy_low": buy_low,
        "buy_high": buy_high,
        "stop_loss": row.get("stop_loss"),
        "take_profit": row.get("take_profit"),
        "zone_status": zone_status,
        "distance": round(distance, 2),
        "in_zone": zone_status == "进入建仓区",
    }


def build_briefing(
    session: Session,
    *,
    day: date | None = None,
    holdings_enabled: bool | None = None,
) -> dict[str, Any]:
    """Assemble the briefing dict; does not send anything."""
    day = day or date.today()
    holdings_enabled = (
        settings.feature_holdings if holdings_enabled is None else holdings_enabled
    )

    previous = session.scalar(
        select(ReviewReport)
        .where(ReviewReport.trade_date < day)
        .order_by(ReviewReport.trade_date.desc())
        .limit(1)
    )

    rows = watch_items(session)
    closes = _last_closes(session, [row["code"] for row in rows])
    items = [item for item in (_zone_item(row, closes.get(row["code"])) for row in rows) if item]
    focus = [item for item in items if item["in_zone"] or abs(item["distance"]) <= 3]
    focus.sort(key=lambda item: (not item["in_zone"], abs(item["distance"])))

    sections: list[dict[str, Any]] = []

    sections.append({"title": "昨日复盘", "lines": _review_lines(previous, day)})

    if focus:
        focus_lines = [
            f"- {item['name']}（{item['code']}）收 {item['price']:.2f} · "
            f"{item['zone_status']}"
            + ("" if item["in_zone"] else f"（{item['distance']:+.2f}%）")
            for item in focus[:5]
        ]
    else:
        focus_lines = ["- 暂无贴近建仓区的自选，今天以观察为主，不追高。"]
    sections.append({"title": "今日关注", "lines": focus_lines})

    if items:
        level_lines = [
            f"- {item['name']}（{item['code']}）建仓 {item['buy_low']:.2f}–{item['buy_high']:.2f}"
            + (f" · 止损 {item['stop_loss']:.2f}" if item.get("stop_loss") else "")
            + (f" · 止盈 {item['take_profit']:.2f}" if item.get("take_profit") else "")
            for item in items[:10]
        ]
    else:
        level_lines = ["- 自选里还没有带计划的股票，先去生成并确认一份交易计划。"]
    sections.append({"title": "自选关键价位", "lines": level_lines})

    if holdings_enabled and previous is not None:
        summaries = (previous.raw or {}).get("summaries") or {}
        text = summaries.get("holding_summary") or previous.holding_summary
        if text:
            sections.append({"title": "持仓", "lines": [f"- {text.strip()}"]})

    advice = MARKET_REGIME.get("position_advice")
    sections.append(
        {
            "title": "纪律提醒",
            "lines": [f"- {item}" for item in DISCIPLINE]
            + ([f"- 当前风格：{MARKET_REGIME.get('label')}，{advice}"] if advice else []),
        }
    )

    title = f"盘前简报 · {day:%m-%d}（周{WEEKDAYS[day.weekday()]}）"
    return {
        "date": day.isoformat(),
        "title": title,
        "trading_day": is_trading_day(session, day),
        "previous_review": previous.trade_date.isoformat() if previous is not None else None,
        "sections": sections,
        "focus": focus[:5],
        "levels": items[:10],
        "text": render_text(title, sections),
    }


def render_text(title: str, sections: list[dict[str, Any]]) -> str:
    parts = [title]
    for section in sections:
        parts.append("")
        parts.append(f"【{section['title']}】")
        parts.extend(section["lines"])
    return "\n".join(parts)


def send_briefing(
    session: Session,
    briefing: dict[str, Any] | None = None,
    *,
    notifier: Notifier | None = None,
    force: bool = False,
    day: date | None = None,
) -> dict[str, Any]:
    """Push the briefing to Feishu. Non-trading days are skipped unless forced."""
    briefing = briefing or build_briefing(session, day=day)
    if not briefing["trading_day"] and not force:
        return {"sent": False, "reason": "non_trading_day", "briefing": briefing}

    notifier = notifier or Notifier()
    lines: list[str] = []
    for section in briefing["sections"]:
        lines.append(f"**{section['title']}**")
        lines.extend(section["lines"])
        lines.append("")
    log = notifier.send_text(
        session,
        briefing["title"],
        lines,
        template="blue",
        footer="TradeAI 盘前简报 · 数据来自昨日收盘与本地计划，不构成投资建议。",
    )
    logger.info("premarket briefing {}: {}", briefing["date"], log.status)
    return {
        "sent": log.status == "success",
        "status": log.status,
        "response": log.response,
        "briefing": briefing,
    }


__all__ = ["build_briefing", "render_text", "send_briefing"]
