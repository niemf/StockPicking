"""The intraday monitoring loop.

One tick = collect quotes for watched codes, persist a snapshot, evaluate alert
rules and deliver notifications. The loop is intentionally dumb: it must keep
working when the AI is unavailable, and every step is safe to re-run (rules are
upserted, events carry a per-day dedup key).

The scheduler is single-process. ``scripts/run.ps1`` runs one uvicorn worker; if
you later scale the web tier, run the monitor as a separate process instead of
starting a loop in every worker.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Holding, Quote, Watchlist
from app.providers.base import Quote as QuoteModel
from app.services import alerts
from app.services.calendar import now_local, session_status
from app.services.intraday import build_stats
from app.services.notifier import Notifier
from app.services.quotes import QuoteService

QUOTE_RETENTION_DAYS = 30

LOCAL_QUOTE_SERVICE = QuoteService()

_state_lock = threading.Lock()
last_tick: dict = {
    "at": None,
    "ok": None,
    "reason": "not_started",
    "codes": 0,
    "quotes": 0,
    "stats": 0,
    "events": 0,
    "sent": 0,
}


def _update_state(**values) -> None:
    with _state_lock:
        last_tick.update(values)


def monitor_status() -> dict:
    with _state_lock:
        return dict(last_tick)


def collect_codes(session: Session) -> list[str]:
    watch_codes = session.scalars(select(Watchlist.code).where(Watchlist.status == "active")).all()
    holding_codes = session.scalars(select(Holding.code).where(Holding.status == "open")).all()
    return sorted(set(watch_codes) | set(holding_codes))


def collect_watch_codes(session: Session) -> list[str]:
    """Watchlist only: these get the intraday stats and buy-signal rules."""
    return sorted(
        set(session.scalars(select(Watchlist.code).where(Watchlist.status == "active")).all())
    )


def _update_trailing_highs(session: Session, quotes: dict[str, QuoteModel]) -> int:
    """Track the post-entry high so the trailing-stop rule can tighten."""
    updated = 0
    holdings = list(session.scalars(select(Holding).where(Holding.status == "open")))
    for holding in holdings:
        quote = quotes.get(holding.code)
        if quote is None or quote.price <= 0:
            continue
        high = max(quote.price, quote.high if quote.high > 0 else 0.0)
        baseline = holding.trailing_high or holding.cost_price
        if high > baseline:
            holding.trailing_high = high
            updated += 1
    if updated:
        session.commit()
    return updated


def _persist_quotes(session: Session, quotes: dict[str, QuoteModel], moment: datetime) -> int:
    stored = 0
    for quote in quotes.values():
        if quote.price <= 0:
            continue
        session.merge(
            Quote(
                code=quote.code,
                ts=moment.replace(microsecond=0),
                price=quote.price,
                pre_close=quote.pre_close,
                open=quote.open,
                high=quote.high,
                low=quote.low,
                volume=quote.volume,
                amount=quote.amount,
                pct_chg=quote.pct_chg,
                turnover_rate=quote.turnover_rate,
                limit_up=quote.limit_up or None,
                limit_down=quote.limit_down or None,
                source=quote.source or "unknown",
            )
        )
        stored += 1
    return stored


def prune_quotes(session: Session, *, days: int = QUOTE_RETENTION_DAYS) -> int:
    cutoff = now_local() - timedelta(days=days)
    result = session.execute(delete(Quote).where(Quote.ts < cutoff))
    session.commit()
    return int(result.rowcount or 0)


def run_once(
    session: Session,
    *,
    quote_service: QuoteService | None = None,
    notifier: Notifier | None = None,
    moment: datetime | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    """Run one monitoring tick. ``force`` ignores the session window (manual test)."""
    moment = moment or now_local()
    status = session_status(session, moment)

    if not force:
        if not status["is_trading_day"]:
            summary = {"status": "skipped", "reason": "non_trading_day", **status}
            _update_state(at=status["now"], ok=True, reason="non_trading_day")
            return summary
        if not status["is_polling_time"]:
            summary = {"status": "skipped", "reason": "outside_session", **status}
            _update_state(at=status["now"], ok=True, reason="outside_session")
            return summary

    codes = collect_codes(session)
    if not codes:
        summary = {"status": "skipped", "reason": "empty_portfolio", **status}
        _update_state(at=status["now"], ok=True, reason="empty_portfolio")
        return summary

    try:
        service = quote_service or LOCAL_QUOTE_SERVICE
        quotes = service.get_quotes(codes)
        _update_trailing_highs(session, quotes)
        rules = alerts.ensure_rules(session)

        # Watchlist diagnostics: one small bars read, reused by every intraday
        # rule on this tick. A failure only disables those rules, never the tick.
        watch_stats: dict = {}
        if settings.alert_watch_stats_enabled:
            watch_codes = collect_watch_codes(session)
            if watch_codes:
                try:
                    watch_stats = build_stats(session, watch_codes, quotes, moment=moment)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("intraday stats failed: {}", exc)

        stored = _persist_quotes(session, quotes, moment)
        session.commit()

        events = alerts.evaluate(session, quotes, moment=moment, stats=watch_stats)
        delivery = {"sent": 0, "failed": 0}
        if events and not dry_run:
            delivery = (notifier or Notifier()).notify_pending(session, limit=len(events))

        summary = {
            "status": "ok",
            "reason": "polled",
            "at": moment.isoformat(timespec="seconds"),
            "codes": len(codes),
            "quotes": len(quotes),
            "stats": len([item for item in watch_stats.values() if item.get("available")]),
            "stored": stored,
            "events": [
                {
                    "id": event.id,
                    "code": event.code,
                    "price": event.trigger_price,
                    "rule": (event.condition_snapshot or {}).get("rule_type"),
                    "dedup_key": event.dedup_key,
                    "status": event.status,
                }
                for event in events
            ],
            "sent": delivery["sent"],
            "failed": delivery["failed"],
            "rules": rules,
            "dry_run": dry_run,
        }
        _update_state(
            at=moment.isoformat(timespec="seconds"),
            ok=True,
            reason="polled",
            codes=len(codes),
            quotes=len(quotes),
            stats=summary["stats"],
            events=len(events),
            sent=delivery["sent"],
        )
        logger.info(
            "monitor tick: {} quotes, {} events, {} sent", len(quotes), len(events), delivery["sent"]
        )
        return summary
    except Exception as exc:  # noqa: BLE001 - a failed tick must not kill the loop
        session.rollback()
        logger.exception("monitor tick failed: {}", exc)
        _update_state(at=moment.isoformat(timespec="seconds"), ok=False, reason=str(exc))
        return {
            "status": "error",
            "reason": str(exc),
            "at": moment.isoformat(timespec="seconds"),
        }


async def monitor_loop(stop_event: asyncio.Event) -> None:
    """Poll every ``poll_interval_seconds`` while the market is open."""
    from app.db.base import SessionLocal

    def _bootstrap() -> None:
        from app.providers.akshare_provider import AkShareProvider
        from app.services.calendar import calendar_size, sync_calendar

        with SessionLocal() as session:
            if calendar_size(session) == 0:
                try:
                    sync_calendar(session, provider=AkShareProvider())
                except Exception as exc:  # noqa: BLE001 - weekday fallback still works
                    logger.warning("calendar bootstrap failed: {}", exc)
            try:
                removed = prune_quotes(session)
                if removed:
                    logger.info("pruned {} old quote rows", removed)
            except Exception as exc:  # noqa: BLE001
                logger.warning("quote prune failed: {}", exc)

    def _tick() -> None:
        # Stats + notifications are blocking calls; running the tick in a worker
        # thread keeps page requests responsive while a poll is in flight.
        with SessionLocal() as session:
            run_once(session)
            _sample_market(session)

    def _sample_market(session) -> None:
        """每 5 分钟采一次板块资金，并顺手刷新情绪周期缓存（供仪表盘直接读）。"""
        from app.services.cycle import compute_cycle
        from app.services.sector_flow import sample as sample_sectors

        try:
            written = sample_sectors(session)
        except Exception as exc:  # noqa: BLE001 - 采样失败不影响预警
            logger.warning("sector flow sample failed: {}", exc)
            return
        if not written:
            return
        try:
            compute_cycle(session, refresh=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cycle refresh failed: {}", exc)

    await asyncio.to_thread(_bootstrap)
    logger.info("monitor loop started (interval={}s)", settings.poll_interval_seconds)
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(_tick)
        except Exception:  # noqa: BLE001 - never let the loop die silently
            logger.exception("unhandled monitor loop error")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.poll_interval_seconds)
        except asyncio.TimeoutError:
            continue

    logger.info("monitor loop stopped")
