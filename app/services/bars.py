"""Daily bar storage and fetching.

Bootstrap once (``backfill``), then update the last few sessions every day
(``incremental``). Writes happen only on the calling thread; worker threads are
limited to HTTP so SQLite never sees concurrent writers.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import pandas as pd
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import DailyBar
from app.providers.akshare_provider import AkShareProvider
from app.providers.base import Quote
from app.providers.sina import SinaProvider
from app.providers.tencent import TencentProvider

UPSERT_CHUNK = 90  # 10 columns * 90 rows stays below SQLite's variable limit
BAR_COLUMNS = (
    "code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "amount",
    "turnover_rate",
    "adj_factor",
)
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


def _as_float(value, default: float | None = None) -> float | None:
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _frame_rows(code: str, df: pd.DataFrame) -> list[dict]:
    rows: list[dict] = []
    for record in df.to_dict("records"):
        trade_date = record.get("trade_date")
        if trade_date is None:
            continue
        if hasattr(trade_date, "date"):
            trade_date = trade_date.date()
        close = _as_float(record.get("close"))
        if close is None or close <= 0:
            continue
        rows.append(
            {
                "code": code,
                "trade_date": trade_date,
                "open": _as_float(record.get("open"), close),
                "high": _as_float(record.get("high"), close),
                "low": _as_float(record.get("low"), close),
                "close": close,
                "pre_close": _as_float(record.get("pre_close")),
                "volume": _as_float(record.get("volume"), 0.0),
                "amount": _as_float(record.get("amount"), 0.0),
                "turnover_rate": _as_float(record.get("turnover_rate")),
                "adj_factor": _as_float(record.get("adj_factor"), 1.0),
            }
        )
    return rows


def upsert_bars(session: Session, code: str, df: pd.DataFrame) -> int:
    """Insert or update one stock's bars. Returns the number of rows written."""
    if df is None or df.empty:
        return 0
    return _write_rows(session, _frame_rows(code, df))


def _write_rows(session: Session, rows: list[dict]) -> int:
    if not rows:
        return 0

    if session.get_bind().dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        for start in range(0, len(rows), UPSERT_CHUNK):
            chunk = rows[start : start + UPSERT_CHUNK]
            statement = sqlite_insert(DailyBar).values(chunk)
            statement = statement.on_conflict_do_update(
                index_elements=["code", "trade_date"],
                set_={
                    column: getattr(statement.excluded, column)
                    for column in (
                        "open",
                        "high",
                        "low",
                        "close",
                        "pre_close",
                        "volume",
                        "amount",
                        "turnover_rate",
                        "adj_factor",
                    )
                },
            )
            session.execute(statement)
    else:  # portable fallback for PostgreSQL / MySQL during migration
        for row in rows:
            session.merge(DailyBar(**row))
    return len(rows)


def bars_from_snapshot(
    session: Session,
    quotes: dict[str, Quote],
    *,
    trade_date: date | None = None,
) -> int:
    """Append the closing snapshot as today's bar (one batch, no per-stock calls).

    This replaces the daily per-stock history request. It is safe after the
    close; during the session the values are partial and must not be stored.
    A weekly ``backfill --force`` repairs any forward-adjustment drift caused by
    ex-dividend dates.
    """
    rows: list[dict] = []
    day = trade_date or date.today()
    for quote in quotes.values():
        if quote.price <= 0 or quote.open <= 0 or quote.high <= 0 or quote.low <= 0:
            continue
        bar_date = quote.ts.date() if quote.ts else day
        rows.append(
            {
                "code": quote.code,
                "trade_date": bar_date,
                "open": float(quote.open),
                "high": float(quote.high),
                "low": float(quote.low),
                "close": float(quote.price),
                "pre_close": float(quote.pre_close) if quote.pre_close else None,
                "volume": float(quote.volume),
                "amount": float(quote.amount),
                "turnover_rate": float(quote.turnover_rate) if quote.turnover_rate else None,
                "adj_factor": 1.0,
            }
        )
    return _write_rows(session, rows)


def coverage(session: Session, codes: list[str] | None = None) -> dict[str, tuple[int, date | None]]:
    statement = select(
        DailyBar.code,
        func.count().label("total"),
        func.max(DailyBar.trade_date).label("latest"),
    ).group_by(DailyBar.code)
    if codes:
        statement = statement.where(DailyBar.code.in_(codes))
    return {
        row.code: (int(row.total), row.latest)
        for row in session.execute(statement)
    }


def _fetch_one(
    provider: AkShareProvider,
    fallbacks: list,
    code: str,
    start: date,
    end: date,
    state: dict,
) -> pd.DataFrame:
    """Fetch one stock; fall back to Tencent/Sina when Eastmoney rate-limits us."""
    if not state["use_fallback"]:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                time.sleep(0.02)
                frame = provider.get_daily_bars(code, start=start, end=end)
                state["ak_failures"] = 0
                if frame is not None and not frame.empty:
                    return frame
            except Exception as exc:  # noqa: BLE001 - retry transient provider errors
                last_error = exc
                state["ak_failures"] += 1
                time.sleep(0.3 * (attempt + 1))
        if state["ak_failures"] >= 15:
            state["use_fallback"] = True
            logger.warning(
                "akshare failed {} times in a row (last: {}); switching to fallback providers",
                state["ak_failures"],
                last_error,
            )

    errors: list[str] = []
    for fallback in fallbacks:
        if fallback.name in state["down"]:
            continue
        try:
            frame = fallback.get_daily_bars(code, start=start, end=end)
            if frame is not None and not frame.empty:
                state["strikes"][fallback.name] = 0
                return frame
            raise RuntimeError("no rows returned")
        except Exception as exc:  # noqa: BLE001 - try the next provider
            errors.append(f"{fallback.name}: {exc}")
            strikes = state["strikes"].get(fallback.name, 0) + 1
            state["strikes"][fallback.name] = strikes
            if strikes >= 10:
                state["down"].add(fallback.name)
                logger.warning("fallback provider {} disabled for this batch: {}", fallback.name, exc)
    raise RuntimeError(f"{code}: " + ("; ".join(errors) or "no rows returned"))


def _run_batch(
    session: Session,
    codes: list[str],
    *,
    start: date,
    end: date,
    workers: int,
    label: str,
    prefer_fallback: bool = False,
) -> dict:
    provider = AkShareProvider()
    fallbacks = [TencentProvider(), SinaProvider()]
    state = {
        "ak_failures": 0,
        "use_fallback": prefer_fallback,
        "strikes": {},
        "down": set(),
    }
    stats = {"requested": len(codes), "written": 0, "failed": []}
    started = time.perf_counter()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_fetch_one, provider, fallbacks, code, start, end, state): code
            for code in codes
        }
        for index, future in enumerate(as_completed(futures), start=1):
            code = futures[future]
            try:
                df = future.result()
                written = upsert_bars(session, code, df)
                session.commit()
                stats["written"] += written
            except Exception as exc:  # noqa: BLE001 - a single stock must not stop the batch
                session.rollback()
                stats["failed"].append(code)
                logger.warning("{} failed {}: {}", label, code, exc)
            if index % 100 == 0 or index == len(codes):
                logger.info(
                    "{} progress {}/{} ({} failed, {} rows)",
                    label,
                    index,
                    len(codes),
                    len(stats["failed"]),
                    stats["written"],
                )

    stats["elapsed_sec"] = round(time.perf_counter() - started, 1)
    return stats


def backfill_bars(
    session: Session,
    codes: list[str],
    *,
    days: int | None = None,
    workers: int | None = None,
    force: bool = False,
    prefer_fallback: bool = False,
) -> dict:
    """Fetch history for stocks that do not have enough cached bars."""
    days = days or settings.bar_backfill_days
    workers = workers or settings.bar_fetch_workers
    end = date.today()
    start = end - timedelta(days=days)

    existing = coverage(session, codes)
    todo: list[str] = []
    skipped = 0
    for code in codes:
        total, latest = existing.get(code, (0, None))
        fresh = latest is not None and latest >= end - timedelta(days=7)
        if not force and total >= 60 and fresh:
            skipped += 1
            continue
        todo.append(code)

    logger.info(
        "backfill: {} requested, {} already fresh, {} to fetch ({}..{})",
        len(codes),
        skipped,
        len(todo),
        start,
        end,
    )
    stats = _run_batch(
        session,
        todo,
        start=start,
        end=end,
        workers=workers,
        label="backfill",
        prefer_fallback=prefer_fallback,
    )
    stats["skipped"] = skipped
    return stats


def incremental_bars(
    session: Session,
    codes: list[str],
    *,
    days: int | None = None,
    workers: int | None = None,
    prefer_fallback: bool = False,
) -> dict:
    """Update the most recent sessions; stocks without history are left alone."""
    days = days or settings.bar_incremental_days
    workers = workers or settings.bar_fetch_workers
    end = date.today()
    start = end - timedelta(days=days)

    existing = coverage(session, codes)
    todo = [code for code in codes if existing.get(code, (0, None))[0] > 0]
    missing = len(codes) - len(todo)
    if missing:
        logger.warning("incremental: {} stocks have no history; run backfill first", missing)

    logger.info("incremental: {} stocks, {}..{}", len(todo), start, end)
    stats = _run_batch(
        session,
        todo,
        start=start,
        end=end,
        workers=workers,
        label="incremental",
        prefer_fallback=prefer_fallback,
    )
    stats["missing_history"] = missing
    return stats
