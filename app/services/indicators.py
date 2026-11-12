"""Persisted technical indicators: daily snapshots and weekly bars.

The discovery funnel computes features on demand from 200-odd daily bars. That
is fast but throwaway: after each close we want a *table* of MA5/10/20/30/60,
量比, ATR, 乖离, 上影线 and so on, so later screens, the stock page and the
backtest can read one row instead of re-deriving it.

Everything here is built from the same ``compute_features`` used by the live
funnel, so the table can never drift from the analysis code.

    daily_bar  --compute_features-->  indicator_daily
    daily_bar  --resample weekly-->   weekly_bar (+ weekly moving averages)
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd
from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.analysis.features import compute_features
from app.db.models import DailyBar, IndicatorDaily, Stock, WeeklyBar
from app.providers.base import is_main_board
from app.services.bars import load_bars_map

UPSERT_CHUNK = 60
CODE_CHUNK = 300

DAILY_FIELDS = (
    "close",
    "ma5",
    "ma10",
    "ma20",
    "ma30",
    "ma60",
    "ma20_slope3",
    "vol_ma5",
    "vol_ma20",
    "volume_ratio",
    "macd_dif",
    "macd_dea",
    "macd_hist",
    "rsi12",
    "boll_upper",
    "boll_mid",
    "boll_lower",
    "atr14",
    "atr_pct",
    "high20",
    "high20_prev",
    "low20",
    "high60_prev",
    "resistance_dist",
    "range20_pct",
    "drawdown20",
    "close_position",
    "upper_shadow_pct",
    "lower_shadow_pct",
    "ret5",
    "ret20",
    "up_streak",
)

WEEK_FIELDS = ("open", "high", "low", "close", "volume", "amount", "pct_chg")


# ------------------------------------------------------------------ helpers
def _clean(value) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return round(number, 4)


def _chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _upsert(session: Session, model, rows: list[dict], keys: tuple[str, ...]) -> int:
    """Insert-or-update many rows. SQLite gets a real upsert, others merge."""
    if not rows:
        return 0
    if session.get_bind().dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        for chunk in _chunks(rows, UPSERT_CHUNK):
            statement = sqlite_insert(model).values(chunk)
            updates = {
                column: getattr(statement.excluded, column)
                for column in chunk[0]
                if column not in keys
            }
            statement = statement.on_conflict_do_update(index_elements=list(keys), set_=updates)
            session.execute(statement)
    else:
        for row in rows:
            session.merge(model(**row))
    return len(rows)


def main_board_codes(session: Session) -> list[str]:
    statement = select(Stock.code).where(Stock.status == "normal").order_by(Stock.code)
    return [code for code in session.scalars(statement) if is_main_board(code)]


# ------------------------------------------------------------------ daily
def _daily_rows(code: str, frame: pd.DataFrame) -> list[dict]:
    fields = [field for field in DAILY_FIELDS if field in frame.columns]
    columns = {field: frame[field].tolist() for field in fields}
    dates = frame["trade_date"].tolist()
    rows: list[dict] = []
    for index, trade_date in enumerate(dates):
        row: dict = {"code": code, "trade_date": trade_date}
        for field in fields:
            row[field] = _clean(columns[field][index])
        rows.append(row)
    return rows


def rebuild_daily(
    session: Session,
    codes: list[str] | None = None,
    *,
    days: int = 260,
    progress: bool = True,
) -> dict:
    """(Re)build ``indicator_daily`` for the last ``days`` bars of each stock."""
    codes = codes or main_board_codes(session)
    written = 0
    skipped = 0
    started = pd.Timestamp.now()
    for index, chunk in enumerate(_chunks(codes, CODE_CHUNK), start=1):
        bars = load_bars_map(session, chunk)
        rows: list[dict] = []
        for code, frame in bars.items():
            if frame.empty or len(frame) < 20:
                skipped += 1
                continue
            enriched = compute_features(frame)
            rows.extend(_daily_rows(code, enriched.tail(days)))
        written += _upsert(session, IndicatorDaily, rows, ("code", "trade_date"))
        session.commit()
        if progress and index % 5 == 0:
            done = min(index * CODE_CHUNK, len(codes))
            logger.info("indicator_daily: {}/{} codes, {} rows", done, len(codes), written)

    return {
        "codes": len(codes),
        "rows": written,
        "skipped": skipped,
        "elapsed_sec": round((pd.Timestamp.now() - started).total_seconds(), 1),
    }


# ------------------------------------------------------------------ weekly
def _weekly_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Resample daily bars into calendar weeks (Monday-based)."""
    daily = frame.copy()
    daily["trade_date"] = pd.to_datetime(daily["trade_date"])
    daily["_week"] = daily["trade_date"] - pd.to_timedelta(daily["trade_date"].dt.day_of_week, unit="D")
    grouped = daily.groupby("_week", sort=True)
    weekly = pd.DataFrame(
        {
            "week_start": grouped["trade_date"].min(),
            "week_end": grouped["trade_date"].max(),
            "open": grouped["open"].first(),
            "high": grouped["high"].max(),
            "low": grouped["low"].min(),
            "close": grouped["close"].last(),
            "volume": grouped["volume"].sum(),
            "amount": grouped["amount"].sum(),
        }
    ).reset_index(drop=True)
    weekly["pct_chg"] = weekly["close"].pct_change() * 100
    for window in (5, 10, 20, 30):
        weekly[f"ma{window}"] = weekly["close"].rolling(window, min_periods=window).mean()
    weekly["weeks"] = range(1, len(weekly) + 1)
    weekly["week_end"] = weekly["week_end"].dt.date
    return weekly


def _weekly_rows(code: str, weekly: pd.DataFrame, keep: int) -> list[dict]:
    fields = list(WEEK_FIELDS) + ["ma5", "ma10", "ma20", "ma30", "weeks"]
    tail = weekly.tail(keep)
    columns = {field: tail[field].tolist() for field in fields}
    dates = tail["week_end"].tolist()
    rows: list[dict] = []
    for index, week_end in enumerate(dates):
        row: dict = {"code": code, "week_end": week_end}
        for field in fields:
            value = columns[field][index]
            row[field] = int(value) if field == "weeks" else _clean(value)
        rows.append(row)
    return rows


def rebuild_weekly(
    session: Session,
    codes: list[str] | None = None,
    *,
    keep_weeks: int = 12,
) -> dict:
    """(Re)build ``weekly_bar`` for the last ``keep_weeks`` weeks of each stock."""
    codes = codes or main_board_codes(session)
    written = 0
    skipped = 0
    for chunk in _chunks(codes, CODE_CHUNK):
        bars = load_bars_map(session, chunk)
        rows: list[dict] = []
        for code, frame in bars.items():
            if frame.empty or len(frame) < 25:
                skipped += 1
                continue
            rows.extend(_weekly_rows(code, _weekly_frame(frame), keep_weeks))
        written += _upsert(session, WeeklyBar, rows, ("code", "week_end"))
        session.commit()
    return {"codes": len(codes), "rows": written, "skipped": skipped}


# ------------------------------------------------------------------ readers
def latest_indicator_date(session: Session) -> date | None:
    return session.scalar(select(func.max(IndicatorDaily.trade_date)))


def load_indicator_map(
    session: Session,
    codes: list[str],
    *,
    trade_date: date | None = None,
) -> dict[str, dict]:
    """Latest indicator row per code (or the row for ``trade_date``)."""
    if not codes:
        return {}
    if trade_date is not None:
        statement = select(IndicatorDaily).where(
            IndicatorDaily.code.in_(codes), IndicatorDaily.trade_date == trade_date
        )
        return {row.code: _row_dict(row) for row in session.scalars(statement)}

    # No explicit date: keep the newest row per code, but never scan the whole
    # history (3195 codes x 260 sessions is a lot of rows for 15 candidates).
    newest = session.scalar(
        select(func.max(IndicatorDaily.trade_date)).where(IndicatorDaily.code.in_(codes))
    )
    if newest is None:
        return {}
    statement = (
        select(IndicatorDaily)
        .where(
            IndicatorDaily.code.in_(codes),
            IndicatorDaily.trade_date >= newest - timedelta(days=10),
        )
        .order_by(IndicatorDaily.code, IndicatorDaily.trade_date.desc())
    )
    result: dict[str, dict] = {}
    for row in session.scalars(statement):
        result.setdefault(row.code, _row_dict(row))
    return result


def load_weekly_map(
    session: Session,
    codes: list[str],
    *,
    lookback_days: int = 21,
    today: date | None = None,
) -> dict[str, dict]:
    """Latest weekly bar per code plus a coarse weekly trend label."""
    if not codes:
        return {}
    today = today or date.today()
    statement = (
        select(WeeklyBar)
        .where(
            WeeklyBar.code.in_(codes),
            WeeklyBar.week_end >= today - timedelta(days=lookback_days),
        )
        .order_by(WeeklyBar.code, WeeklyBar.week_end.desc())
    )
    latest: dict[str, WeeklyBar] = {}
    for row in session.scalars(statement):
        latest.setdefault(row.code, row)

    result: dict[str, dict] = {}
    for code, row in latest.items():
        result[code] = {
            "week_end": row.week_end.isoformat(),
            "close": row.close,
            "ma5": row.ma5,
            "ma10": row.ma10,
            "ma20": row.ma20,
            "ma30": row.ma30,
            "pct_chg": row.pct_chg,
            "trend": weekly_trend(row.ma5, row.ma20, row.close),
        }
    return result


def weekly_trend(ma5: float | None, ma20: float | None, close: float | None) -> str | None:
    """``up`` / ``down`` / ``flat`` from the weekly MA structure."""
    if not ma5 or not ma20 or not close:
        return None
    if ma5 >= ma20 and close >= ma20:
        return "up"
    if ma5 < ma20 and close < ma20:
        return "down"
    return "flat"


def _row_dict(row: IndicatorDaily) -> dict:
    data = {field: getattr(row, field) for field in DAILY_FIELDS}
    data["trade_date"] = row.trade_date.isoformat()
    return data


def purge_indicators(session: Session, before: date) -> int:
    deleted = session.execute(delete(IndicatorDaily).where(IndicatorDaily.trade_date < before))
    session.commit()
    return int(deleted.rowcount or 0)


__all__ = [
    "DAILY_FIELDS",
    "latest_indicator_date",
    "load_indicator_map",
    "load_weekly_map",
    "main_board_codes",
    "purge_indicators",
    "rebuild_daily",
    "rebuild_weekly",
    "weekly_trend",
]
