"""Watchlist / holding business logic.

The service layer is the only place that writes portfolio data. Views call the
``*_items`` helpers to get template-ready dictionaries; the REST API calls the
mutation helpers. Keeping both on the same functions avoids the classic bug
where the UI and the API disagree about validation rules.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DailyBar, Holding, HoldingTransaction, Stock, TradePlan, Watchlist
from app.providers.base import board_label, exchange_for_code, is_main_board, normalize_code

SOURCE_LABELS = {"ai": "AI 候选", "rule": "规则候选", "manual": "手动添加"}


class PortfolioError(ValueError):
    """Validation or business-rule failure, safe to show to the user."""


# --------------------------------------------------------------------- helpers
def _normalize(code: str) -> str:
    try:
        value = normalize_code(code)
    except ValueError as exc:
        raise PortfolioError(f"股票代码格式不正确：{code}") from exc
    if not is_main_board(value):
        raise PortfolioError("仅支持沪深主板（600/601/603/605、000/001/002/003）")
    return value


def _ensure_stock(session: Session, code: str) -> Stock:
    """Return the stock row, creating a minimal one when it is missing."""
    stock = session.get(Stock, code)
    if stock is not None:
        return stock

    name = code
    try:
        # Only hit the network when the master list has not been synced yet.
        from app.services.quotes import QuoteService

        quote = QuoteService().get_quote(code)
        if quote is not None and quote.name:
            name = quote.name
    except Exception as exc:  # noqa: BLE001 - naming should never block a write
        logger.warning("quote lookup failed for {}: {}", code, exc)

    stock = Stock(
        code=code,
        name=name,
        exchange=exchange_for_code(code),
        board=board_label(code),
        status="normal",
    )
    session.add(stock)
    session.flush()
    return stock


def _fallback_price(session: Session, code: str, plan: TradePlan | None, cost: float = 0.0) -> float:
    bar = session.scalar(
        select(DailyBar).where(DailyBar.code == code).order_by(DailyBar.trade_date.desc()).limit(1)
    )
    if bar is not None:
        return float(bar.close)
    if plan is not None and plan.buy_zone_high:
        return float(plan.buy_zone_high)
    return float(cost)


def _iso(value: date | datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()[:10]


# --------------------------------------------------------------------- reads
def watch_items(session: Session) -> list[dict[str, Any]]:
    """Return active watchlist rows shaped like the demo data the templates use."""
    rows = list(
        session.scalars(
            select(Watchlist)
            .where(Watchlist.status == "active")
            .order_by(Watchlist.added_at.desc(), Watchlist.id.desc())
        )
    )
    if not rows:
        return []

    codes = [row.code for row in rows]
    stocks = {s.code: s for s in session.scalars(select(Stock).where(Stock.code.in_(codes)))}

    items: list[dict[str, Any]] = []
    for row in rows:
        plan = row.plan
        stock = stocks.get(row.code)
        items.append(
            {
                "id": row.id,
                "code": row.code,
                "name": row.name,
                "industry": stock.industry if stock and stock.industry else "—",
                "fallback_price": _fallback_price(session, row.code, plan),
                "fallback_pct": 0.0,
                "plan_no": plan.plan_no if plan else None,
                "buy_low": plan.buy_zone_low if plan else None,
                "buy_high": plan.buy_zone_high if plan else None,
                "stop_loss": plan.stop_loss if plan else None,
                "take_profit": plan.take_profit_1 if plan else None,
                "position_pct": plan.position_pct if plan else None,
                "rating": (plan.rating if plan and plan.rating else "观察"),
                "confidence": plan.confidence if plan and plan.confidence is not None else 0,
                "risk": plan.risk_score if plan and plan.risk_score is not None else 0,
                "plan_date": _iso(plan.trade_date) if plan else _iso(row.added_at),
                "source": SOURCE_LABELS.get(row.source, row.source),
                "reason": (plan.thesis if plan and plan.thesis else "手动添加，尚未生成交易计划。"),
                "tags": (plan.tags if plan and plan.tags else []),
                "has_plan": plan is not None,
                "note": row.note,
                "group": row.group_name or "默认",
                "watch_tags": row.tags or [],
                "search": _search_blob(row.code, row.name, row.group_name, row.tags, row.note),
            }
        )
    return items


def _search_blob(
    code: str,
    name: str,
    group: str | None = None,
    tags: list[str] | None = None,
    note: str | None = None,
) -> str:
    """Lower-cased haystack: code, name, full pinyin, initials, group, tags."""
    parts = [code, name or "", group or "", note or ""]
    parts.extend(tags or [])
    if name:
        try:
            from pypinyin import Style, lazy_pinyin

            parts.extend(lazy_pinyin(name))
            parts.append("".join(lazy_pinyin(name)))
            parts.append("".join(lazy_pinyin(name, style=Style.FIRST_LETTER)))
        except Exception:  # noqa: BLE001 - search must work without pypinyin
            pass
    return " ".join(part.lower() for part in parts if part)


def filter_watches(
    items: list[dict[str, Any]],
    *,
    query: str | None = None,
    group: str | None = None,
) -> list[dict[str, Any]]:
    result = items
    if group and group != "全部":
        result = [item for item in result if item.get("group") == group]
    if query:
        needle = query.strip().lower()
        if needle:
            result = [item for item in result if needle in item.get("search", "")]
    return result


def holding_items(session: Session) -> list[dict[str, Any]]:
    """Return open holdings shaped like the demo data the templates use."""
    rows = list(
        session.scalars(select(Holding).where(Holding.status == "open").order_by(Holding.open_date))
    )
    if not rows:
        return []

    codes = [row.code for row in rows]
    stocks = {s.code: s for s in session.scalars(select(Stock).where(Stock.code.in_(codes)))}
    plans = {
        plan.id: plan
        for plan in session.scalars(
            select(TradePlan).where(TradePlan.id.in_([row.plan_id for row in rows if row.plan_id]))
        )
    }

    items: list[dict[str, Any]] = []
    for row in rows:
        plan = plans.get(row.plan_id) if row.plan_id else None
        stock = stocks.get(row.code)
        stop_loss = row.current_stop or (plan.stop_loss if plan else None)
        items.append(
            {
                "id": row.id,
                "code": row.code,
                "name": row.name,
                "industry": stock.industry if stock and stock.industry else "—",
                "cost": row.cost_price,
                "shares": row.shares,
                "available_shares": row.available_shares,
                "fallback_price": _fallback_price(session, row.code, plan, cost=row.cost_price),
                "fallback_pct": 0.0,
                "stop_loss": stop_loss,
                "take_profit_1": plan.take_profit_1 if plan else None,
                "take_profit_2": plan.take_profit_2 if plan else None,
                "trailing_high": row.trailing_high,
                "open_date": _iso(row.open_date),
                "plan_id": plan.plan_no if plan else "—",
                "note": row.note,
            }
        )
    return items


def find_watch(session: Session, code: str) -> Watchlist | None:
    return session.scalar(select(Watchlist).where(Watchlist.code == code))


def find_holding(session: Session, code: str) -> Holding | None:
    return session.scalar(select(Holding).where(Holding.status == "open", Holding.code == code))


# --------------------------------------------------------------------- mutations
def add_watch(
    session: Session,
    code: str,
    source: str = "manual",
    note: str | None = None,
    *,
    group_name: str | None = None,
    tags: list[str] | None = None,
) -> Watchlist:
    value = _normalize(code)
    if find_watch(session, value) is not None:
        raise PortfolioError(f"{value} 已在自选列表中")

    stock = _ensure_stock(session, value)
    row = Watchlist(
        code=value,
        name=stock.name,
        source=source,
        note=note,
        status="active",
        group_name=(group_name or "默认").strip()[:32] or "默认",
        tags=[tag[:16] for tag in (tags or []) if str(tag).strip()][:8] or None,
    )
    session.add(row)
    session.commit()
    return row


def remove_watch(session: Session, watch_id: int) -> None:
    row = session.get(Watchlist, watch_id)
    if row is None:
        raise PortfolioError("自选股不存在")
    session.delete(row)
    session.commit()


def update_watch(
    session: Session,
    watch_id: int,
    *,
    group_name: str | None = None,
    tags: list[str] | None = None,
    note: str | None = None,
) -> Watchlist:
    row = session.get(Watchlist, watch_id)
    if row is None:
        raise PortfolioError("自选股不存在")
    if group_name is not None:
        row.group_name = group_name.strip()[:32] or "默认"
    if tags is not None:
        row.tags = [tag[:16] for tag in tags if str(tag).strip()][:8] or None
    if note is not None:
        row.note = note.strip()[:500] or None
    session.commit()
    return row


_LINE_SPLIT = re.compile(r"[,，\t]")
_TAG_SPLIT = re.compile(r"[;；/]")


def import_watches(
    session: Session,
    text: str,
    *,
    default_group: str = "默认",
) -> dict[str, Any]:
    """Bulk import ``code,group,note,tags`` lines; duplicates are skipped."""
    added: list[str] = []
    skipped: list[str] = []
    errors: list[dict[str, Any]] = []

    for index, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in _LINE_SPLIT.split(line)]
        code = parts[0]
        try:
            value = _normalize(code)
        except PortfolioError as exc:
            errors.append({"line": index, "value": code, "error": str(exc)})
            continue
        if find_watch(session, value) is not None:
            skipped.append(value)
            continue

        group = parts[1] if len(parts) > 1 and parts[1] else default_group
        note = parts[2] if len(parts) > 2 and parts[2] else None
        tags = (
            [tag.strip() for tag in _TAG_SPLIT.split(parts[3])]
            if len(parts) > 3 and parts[3]
            else []
        )
        try:
            add_watch(session, value, group_name=group, note=note, tags=tags)
            added.append(value)
        except PortfolioError as exc:
            errors.append({"line": index, "value": code, "error": str(exc)})

    return {
        "added": added,
        "skipped": skipped,
        "errors": errors,
        "total": len(added) + len(skipped) + len(errors),
    }


def watch_groups(session: Session) -> list[str]:
    rows = session.scalars(
        select(Watchlist.group_name)
        .where(Watchlist.status == "active")
        .distinct()
        .order_by(Watchlist.group_name)
    )
    return [row for row in rows if row] or ["默认"]


def add_holding(
    session: Session,
    *,
    code: str,
    cost_price: float,
    shares: int,
    stop_loss: float,
    take_profit_1: float | None = None,
    take_profit_2: float | None = None,
    open_date: date | None = None,
    note: str | None = None,
) -> Holding:
    value = _normalize(code)
    if find_holding(session, value) is not None:
        raise PortfolioError(f"{value} 已有持仓记录，请用加仓/减仓")
    if cost_price <= 0:
        raise PortfolioError("成本价必须大于 0")
    if shares <= 0 or shares % 100:
        raise PortfolioError("股数必须是 100 的正整数倍")
    if stop_loss <= 0 or stop_loss >= cost_price:
        raise PortfolioError("止损价必须介于 0 和成本价之间")
    for target in (take_profit_1, take_profit_2):
        if target is not None and target <= cost_price:
            raise PortfolioError("止盈价必须高于成本价")

    stock = _ensure_stock(session, value)
    plan = TradePlan(
        plan_no=f"HOLD-{datetime.now():%Y%m%d%H%M%S}-{value}",
        code=value,
        name=stock.name,
        source="manual",
        trade_date=open_date or date.today(),
        stop_loss=stop_loss,
        take_profit_1=take_profit_1,
        take_profit_2=take_profit_2,
        status="confirmed",
        confirmed_at=datetime.now(),
    )
    session.add(plan)
    session.flush()

    holding = Holding(
        code=value,
        name=stock.name,
        cost_price=float(cost_price),
        shares=int(shares),
        available_shares=int(shares),
        open_date=open_date or date.today(),
        plan_id=plan.id,
        current_stop=stop_loss,
        trailing_high=cost_price,
        status="open",
        note=note,
    )
    session.add(holding)
    session.flush()
    session.add(
        HoldingTransaction(
            holding_id=holding.id,
            trade_date=open_date or date.today(),
            action="buy",
            price=float(cost_price),
            shares=int(shares),
        )
    )
    session.commit()
    return holding


def update_holding(
    session: Session,
    holding_id: int,
    *,
    current_stop: float | None = None,
    trailing_high: float | None = None,
    note: str | None = None,
) -> Holding:
    holding = session.get(Holding, holding_id)
    if holding is None:
        raise PortfolioError("持仓不存在")
    if current_stop is not None:
        if current_stop <= 0:
            raise PortfolioError("止损价必须大于 0")
        if current_stop >= holding.cost_price:
            raise PortfolioError("止损价必须低于成本价")
        holding.current_stop = float(current_stop)
        if holding.plan_id:
            plan = session.get(TradePlan, holding.plan_id)
            if plan is not None:
                plan.stop_loss = float(current_stop)
    if trailing_high is not None:
        holding.trailing_high = float(trailing_high)
    if note is not None:
        holding.note = note
    session.commit()
    return holding


def remove_holding(session: Session, holding_id: int) -> None:
    holding = session.get(Holding, holding_id)
    if holding is None:
        raise PortfolioError("持仓不存在")
    session.delete(holding)  # transactions cascade
    session.commit()


def record_transaction(
    session: Session,
    holding_id: int,
    *,
    action: str,
    price: float,
    shares: int,
    fee: float = 0.0,
    trade_date: date | None = None,
) -> Holding:
    """Apply a buy/sell to the holding and keep cost basis consistent."""
    if action not in {"buy", "sell"}:
        raise PortfolioError("action 只能是 buy 或 sell")
    if price <= 0:
        raise PortfolioError("成交价必须大于 0")
    if shares <= 0:
        raise PortfolioError("股数必须大于 0")

    holding = session.get(Holding, holding_id)
    if holding is None:
        raise PortfolioError("持仓不存在")

    if action == "buy":
        if shares % 100:
            raise PortfolioError("买入股数必须是 100 的整数倍")
        total_cost = holding.cost_price * holding.shares + price * shares + fee
        holding.shares += shares
        holding.available_shares += shares
        holding.cost_price = round(total_cost / holding.shares, 4)
    else:
        if shares > holding.available_shares:
            raise PortfolioError("可卖数量不足")
        realized = (price - holding.cost_price) * shares - fee
        holding.shares -= shares
        holding.available_shares -= shares
        holding.realized_pnl = round((holding.realized_pnl or 0.0) + realized, 2)
        if holding.shares == 0:
            holding.status = "closed"
            holding.closed_at = datetime.now()

    session.add(
        HoldingTransaction(
            holding_id=holding.id,
            trade_date=trade_date or date.today(),
            action=action,
            price=float(price),
            shares=int(shares),
            fee=float(fee),
        )
    )
    session.commit()
    return holding
