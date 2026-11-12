"""Whole-market stock discovery: load data, run the funnel, persist candidates."""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any

import pandas as pd
from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.analysis.context import StockContext, StockMeta
from app.analysis.pipeline import PipelineResult, load_profile, run_pipeline
from app.config import settings
from app.db.models import Candidate, DailyBar, JobRun, Stock
from app.providers.base import is_main_board
from app.services.calendar import market_session
from app.services.quotes import QuoteService
from app.services.universe import listed_trading_days, open_dates

BAR_CHUNK = 400


def load_bars_map(session: Session, codes: list[str]) -> dict[str, pd.DataFrame]:
    """Load bars for many stocks in bounded chunks (one query per chunk)."""
    result: dict[str, pd.DataFrame] = {}
    columns = (
        DailyBar.code,
        DailyBar.trade_date,
        DailyBar.open,
        DailyBar.high,
        DailyBar.low,
        DailyBar.close,
        DailyBar.volume,
        DailyBar.amount,
        DailyBar.turnover_rate,
    )
    for start in range(0, len(codes), BAR_CHUNK):
        chunk = codes[start : start + BAR_CHUNK]
        statement = (
            select(*columns)
            .where(DailyBar.code.in_(chunk))
            .order_by(DailyBar.code, DailyBar.trade_date)
        )
        # pandas' reader is roughly twice as fast as building the frame from
        # SQLAlchemy Row tuples for ~100k rows.
        frame = pd.read_sql_query(statement, session.connection())
        if frame.empty:
            continue
        for code, group in frame.groupby("code", sort=False):
            result[str(code)] = group.reset_index(drop=True)
    return result


def session_progress(moment: datetime | None = None) -> float:
    """Fraction of the 4-hour session that has elapsed, used to scale liquidity."""
    moment = moment or datetime.now()
    session = market_session(moment)
    if session in {"after_close", "closed"}:
        return 1.0
    if session == "morning":
        elapsed = (moment.hour * 60 + moment.minute) - (9 * 60 + 30)
        return min(1.0, max(0.1, (elapsed + 1) / 240))
    if session == "afternoon":
        elapsed = 120 + (moment.hour * 60 + moment.minute) - (13 * 60)
        return min(1.0, max(0.1, (elapsed + 1) / 240))
    return 0.1


def build_contexts(
    session: Session,
    stocks: list[Stock],
    quotes: dict,
    *,
    today: date,
    moment: datetime | None = None,
) -> list[StockContext]:
    calendar = open_dates(session)
    progress = session_progress(moment)
    contexts: list[StockContext] = []
    for stock in stocks:
        quote = quotes.get(stock.code)
        meta = StockMeta(
            code=stock.code,
            name=stock.name,
            industry=stock.industry,
            list_date=stock.list_date,
            is_st=bool(stock.is_st),
            is_delisting_risk=bool(stock.is_delisting_risk),
            status=stock.status or "normal",
        )
        contexts.append(
            StockContext(
                meta=meta,
                quote=quote,
                listed_days=listed_trading_days(stock.list_date, today, calendar),
                extras={"session_progress": progress},
            )
        )
    return contexts


def _persist_result(
    session: Session,
    result: PipelineResult,
    *,
    trade_date: date,
    moment: datetime,
    extra_stats: dict[str, Any],
) -> None:
    session.execute(delete(Candidate).where(Candidate.trade_date == trade_date))
    for item in result.candidates:
        snapshot = dict(item.get("indicator_snapshot") or {})
        snapshot["score_breakdown"] = item.get("score_breakdown") or {}
        snapshot["price_at_discovery"] = item.get("price")
        snapshot["pct_chg_at_discovery"] = item.get("pct_chg")
        snapshot["industry"] = item.get("industry")
        snapshot["risk"] = item.get("risk")
        session.add(
            Candidate(
                trade_date=trade_date,
                code=item["code"],
                name=item["name"],
                score=float(item["score"]),
                rank=int(item["rank"]),
                rule_hits=list(item.get("rule_hits") or []),
                indicator_snapshot=snapshot,
                status="pending",
            )
        )

    stats = {**result.summary(), **extra_stats}
    job = JobRun(
        job_name="discovery",
        trade_date=trade_date,
        scheduled_at=moment.replace(microsecond=0),
        started_at=moment,
        finished_at=datetime.now(),
        status="success",
        stats=stats,
    )
    session.add(job)
    session.commit()


def bars_freshness(
    session: Session,
    *,
    today: date,
    moment: datetime | None = None,
) -> dict[str, Any]:
    """Compare the newest stored daily bar with the newest *expected* one.

    Discovery happily appends today's live quote to whatever the last stored
    bar is. If a whole session is missing, every moving average and every
    volume ratio is silently wrong, so the funnel reports it instead of
    pretending the data is complete.
    """
    latest = session.scalar(select(func.max(DailyBar.trade_date)))
    if latest is None:
        return {"bars_latest": None, "expected_bar_date": None, "bars_stale": True, "bars_stale_days": None}

    moment = moment or datetime.now()
    session_name = market_session(moment)
    dates = open_dates(session)
    # Before the close, today's bar is not written yet; the live quote is merged
    # into the frame at analysis time instead.
    usable = [day for day in dates if day < today] if session_name != "after_close" else [
        day for day in dates if day <= today
    ]
    expected = max(usable) if usable else None
    missed = [day for day in usable if latest < day <= (expected or latest)]
    return {
        "bars_latest": latest.isoformat(),
        "expected_bar_date": expected.isoformat() if expected else None,
        "bars_stale": expected is not None and latest < expected,
        "bars_stale_days": len(missed),
    }


def run_discovery(
    session: Session,
    *,
    quote_service: QuoteService | None = None,
    quotes: dict | None = None,
    profile: dict | None = None,
    today: date | None = None,
    moment: datetime | None = None,
    persist: bool = True,
    limit: int | None = None,
) -> dict[str, Any]:
    """Run one full-market screen. Returns the funnel summary and top candidates.

    ``quotes`` can be passed in to reuse a snapshot already fetched for the
    closing-bar update, avoiding a second full-market request.
    """
    today = today or date.today()
    moment = moment or datetime.now()
    profile = profile or load_profile()
    started = time.perf_counter()

    statement = select(Stock).where(Stock.status == "normal").order_by(Stock.code)
    stocks = [stock for stock in session.scalars(statement) if is_main_board(stock.code)]
    if limit:
        stocks = stocks[:limit]

    codes = [stock.code for stock in stocks]
    quote_started = time.perf_counter()
    if quotes is None:
        quotes = (quote_service or QuoteService()).get_quotes(codes)
    quote_seconds = time.perf_counter() - quote_started

    contexts = build_contexts(session, stocks, quotes, today=today, moment=moment)
    loaded_codes: set[str] = set()
    bars_seconds = {"value": 0.0}

    def bars_loader(wanted: list[str]) -> dict[str, pd.DataFrame]:
        loaded_codes.update(wanted)
        started_bars = time.perf_counter()
        try:
            return load_bars_map(session, wanted)
        finally:
            bars_seconds["value"] += time.perf_counter() - started_bars

    bars_started = time.perf_counter()
    result = run_pipeline(contexts, profile, bars_loader=bars_loader, today=today)
    pipeline_seconds = time.perf_counter() - bars_started
    bars_load_seconds = bars_seconds["value"]
    compute_seconds = max(0.0, pipeline_seconds - bars_load_seconds)

    elapsed = time.perf_counter() - started
    extra = {
        "quotes": len(quotes),
        "codes": len(codes),
        "bars_loaded": len(loaded_codes),
        "quote_sec": round(quote_seconds, 2),
        "bars_load_sec": round(bars_load_seconds, 2),
        "compute_sec": round(compute_seconds, 2),
        "pipeline_sec": round(pipeline_seconds, 2),
        "total_sec": round(elapsed, 2),
        "trade_date": today.isoformat(),
    }
    freshness = bars_freshness(session, today=today, moment=moment)
    extra.update(freshness)
    if freshness["bars_stale"]:
        logger.warning(
            "daily bars are stale: latest={} expected={} ({} sessions missing); "
            "indicators may be wrong — run scripts/backfill_bars.py --incremental",
            freshness["bars_latest"],
            freshness["expected_bar_date"],
            freshness["bars_stale_days"],
        )
    if persist:
        _persist_result(session, result, trade_date=today, moment=moment, extra_stats=extra)

    summary = result.summary()
    summary.update(extra)
    logger.info(
        "discovery done: {} stocks -> {} candidates in {:.1f}s (quotes {:.1f}s, pipeline {:.1f}s)",
        len(codes),
        len(result.candidates),
        elapsed,
        quote_seconds,
        pipeline_seconds,
    )
    return {
        "summary": summary,
        "candidates": result.candidates,
        "stages": [stage.as_dict() for stage in result.stages],
    }


def latest_candidates(session: Session, limit: int = 100) -> tuple[date | None, list[Candidate]]:
    latest = session.scalar(select(Candidate.trade_date).order_by(Candidate.trade_date.desc()).limit(1))
    if latest is None:
        return None, []
    rows = list(
        session.scalars(
            select(Candidate)
            .where(Candidate.trade_date == latest)
            .order_by(Candidate.rank)
            .limit(limit)
        )
    )
    return latest, rows


def latest_job(session: Session) -> JobRun | None:
    return session.scalar(
        select(JobRun).where(JobRun.job_name == "discovery").order_by(JobRun.started_at.desc()).limit(1)
    )


__all__ = [
    "bars_freshness",
    "build_contexts",
    "latest_candidates",
    "latest_job",
    "load_bars_map",
    "run_discovery",
    "session_progress",
]
