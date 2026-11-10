"""Seed the database with the demo watchlist / holdings / plans.

Stage A only: this turns the hard-coded ``app.demo_data`` rows into real table
rows so stage B can read from the database instead. Re-running is safe; rows are
matched by their natural key (stock code / plan number).

Run:
    .venv\\python.exe scripts\\init_db.py --seed
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.demo_data import HOLDINGS, STOCK_DETAILS, WATCHLIST
from app.db.models import Holding, Stock, TradePlan, Watchlist
from app.providers.base import board_label, exchange_for_code


def _plan_source(source: str) -> str:
    return "ai" if "AI" in source else "manual"


def _as_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def _ensure_stock(session: Session, code: str, name: str, industry: str | None) -> bool:
    if session.get(Stock, code) is not None:
        return False
    session.add(
        Stock(
            code=code,
            name=name,
            exchange=exchange_for_code(code),
            board=board_label(code),
            industry=industry,
            status="normal",
        )
    )
    return True


def _ensure_plan(session: Session, plan_no: str, **fields) -> tuple[TradePlan, bool]:
    existing = session.scalar(select(TradePlan).where(TradePlan.plan_no == plan_no))
    if existing is not None:
        return existing, False

    plan = TradePlan(plan_no=plan_no, **fields)
    session.add(plan)
    session.flush()  # assign id for the watchlist / holding link
    return plan, True


def seed_demo_data(session: Session) -> dict[str, int]:
    """Insert demo rows and return per-table insert counts."""
    counts = {"stock": 0, "trade_plan": 0, "watchlist": 0, "holding": 0}

    for index, row in enumerate(WATCHLIST, start=1):
        if _ensure_stock(session, row["code"], row["name"], row.get("industry")):
            counts["stock"] += 1

        detail = STOCK_DETAILS.get(row["code"], {})
        plan, created = _ensure_plan(
            session,
            f"PLAN-{row['plan_date'].replace('-', '')}-W{index:02d}",
            code=row["code"],
            name=row["name"],
            source=_plan_source(row["source"]),
            trade_date=_as_date(row["plan_date"]) or date.today(),
            thesis=row["reason"],
            fundamental_view=detail.get("fundamental_view"),
            technical_view=detail.get("technical_view"),
            catalysts=detail.get("catalysts"),
            risks=detail.get("risks"),
            buy_zone_low=row["buy_low"],
            buy_zone_high=row["buy_high"],
            stop_loss=row["stop_loss"],
            take_profit_1=row["take_profit"],
            take_profit_2=None,
            position_pct=row["position_pct"],
            confidence=row["confidence"],
            rating=row["rating"],
            risk_score=row["risk"],
            tags=row["tags"],
            market_regime="neutral",
            model="demo",
            prompt_version="demo-v0",
            status="confirmed",
            confirmed_at=datetime.now(),
        )
        counts["trade_plan"] += int(created)

        if session.scalar(select(Watchlist).where(Watchlist.code == row["code"])) is None:
            session.add(
                Watchlist(
                    code=row["code"],
                    name=row["name"],
                    source="ai" if _plan_source(row["source"]) == "ai" else "rule",
                    plan_id=plan.id,
                    status="active",
                )
            )
            counts["watchlist"] += 1

    for row in HOLDINGS:
        if _ensure_stock(session, row["code"], row["name"], row.get("industry")):
            counts["stock"] += 1

        detail = STOCK_DETAILS.get(row["code"], {})
        plan, created = _ensure_plan(
            session,
            row["plan_id"],
            code=row["code"],
            name=row["name"],
            source="manual",
            trade_date=_as_date(row["open_date"]) or date.today(),
            thesis=detail.get("thesis"),
            fundamental_view=detail.get("fundamental_view"),
            technical_view=detail.get("technical_view"),
            catalysts=detail.get("catalysts"),
            risks=detail.get("risks"),
            stop_loss=row["stop_loss"],
            take_profit_1=row["take_profit_1"],
            take_profit_2=row["take_profit_2"],
            market_regime="neutral",
            model="demo",
            prompt_version="demo-v0",
            status="confirmed",
            confirmed_at=datetime.now(),
        )
        counts["trade_plan"] += int(created)

        if session.scalar(select(Holding).where(Holding.code == row["code"])) is None:
            session.add(
                Holding(
                    code=row["code"],
                    name=row["name"],
                    cost_price=row["cost"],
                    shares=row["shares"],
                    available_shares=row["shares"],
                    open_date=_as_date(row["open_date"]),
                    plan_id=plan.id,
                    current_stop=row["stop_loss"],
                    trailing_high=row["trailing_high"],
                    status="open",
                )
            )
            counts["holding"] += 1

    session.commit()
    return counts
