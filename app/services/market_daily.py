"""市场级每日快照：指数、成交额、涨跌停、炸板率、情绪阶段。

个股 K 线回答「这只票怎么走」，这张表回答「今天市场什么温度」。

两个来源：

* ``live``：收盘后用全市场行情快照 + 涨停池算，炸板率/连板高度/昨日涨停溢价都精确；
* ``bars``：从日线反推（历史回填用）。没有盘中数据，所以炸板率为空，
  其余字段（涨跌家数、涨跌停、成交额、中位数、昨日涨停溢价）可近似。

回测按情绪阶段分组时读这张表，不必每天重新计算。
"""

from __future__ import annotations

import time
from datetime import date

import pandas as pd
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DailyBar, MarketDaily, Stock
from app.providers.akshare_provider import AkShareProvider
from app.providers.base import is_main_board
from app.services.cycle import CycleState, classify, temperature

INDEXES = (
    ("sh_close", "sh_pct", "sh000001"),
    ("sz_close", "sz_pct", "sz399001"),
    ("cyb_close", "cyb_pct", "sz399006"),
    ("zz1000_close", "zz1000_pct", "sh000852"),
)

_index_cache: dict = {"ts": 0.0, "data": {}}
INDEX_TTL = 3600


def fetch_index_daily(
    start: date | None = None,
    end: date | None = None,
    *,
    provider: AkShareProvider | None = None,
) -> dict[str, pd.DataFrame]:
    """拉取四大指数的日线，1 小时内复用。失败时返回已拿到的部分。"""
    now = time.monotonic()
    if _index_cache["data"] and now - float(_index_cache["ts"]) < INDEX_TTL:
        cached = _index_cache["data"]
    else:
        provider = provider or AkShareProvider()
        cached = {}
        for _, _, symbol in INDEXES:
            try:
                frame = provider.get_index_daily(symbol, start=start, end=end)
                if frame is not None and not frame.empty:
                    cached[symbol] = frame
            except Exception as exc:  # noqa: BLE001 - 指数缺一块不影响主流程
                logger.warning("index {} unavailable: {}", symbol, exc)
        _index_cache.update({"ts": now, "data": cached})
    if start is None and end is None:
        return cached
    trimmed = {}
    for symbol, frame in cached.items():
        subset = frame
        if start is not None:
            subset = subset[subset["trade_date"] >= start]
        if end is not None:
            subset = subset[subset["trade_date"] <= end]
        trimmed[symbol] = subset
    return trimmed


def index_rows(start: date | None = None, end: date | None = None) -> dict[date, dict]:
    """把指数日线摊平成 {交易日: {sh_close, sh_pct, ...}}。"""
    rows: dict[date, dict] = {}
    for close_col, pct_col, symbol in INDEXES:
        frame = fetch_index_daily(start, end).get(symbol)
        if frame is None or frame.empty:
            continue
        closes = frame["close"].astype(float)
        pcts = closes.pct_change() * 100
        for index, record in enumerate(frame.itertuples()):
            day = record.trade_date
            rows.setdefault(day, {})[close_col] = round(float(closes.iloc[index]), 2)
            rows[day][pct_col] = round(float(pcts.iloc[index]), 2) if index else None
    return rows


# ------------------------------------------------------------------ live
def snapshot(
    session: Session,
    trade_date: date,
    *,
    cycle: CycleState,
    indices: dict[date, dict] | None = None,
) -> MarketDaily | None:
    """用收盘快照 + 情绪周期写当日一行（idempotent）。"""
    metrics = cycle.metrics or {}
    if not metrics.get("available"):
        return None

    indices = indices if indices is not None else index_rows(trade_date, trade_date)
    row = session.get(MarketDaily, trade_date) or MarketDaily(trade_date=trade_date)
    for close_col, pct_col, _ in INDEXES:
        setattr(row, close_col, (indices.get(trade_date) or {}).get(close_col))
        setattr(row, pct_col, (indices.get(trade_date) or {}).get(pct_col))
    row.amount_yi = metrics.get("amount_yi")
    row.total = metrics.get("total")
    row.up = metrics.get("up")
    row.down = metrics.get("down")
    row.flat = metrics.get("flat")
    row.limit_up = metrics.get("limit_up")
    row.limit_down = metrics.get("limit_down")
    row.break_rate = metrics.get("break_rate")
    row.max_streak = metrics.get("max_streak")
    row.prev_premium = metrics.get("prev_premium")
    row.breadth = metrics.get("breadth")
    row.median_pct = metrics.get("median_pct")
    row.phase = cycle.phase
    row.temperature = cycle.temperature
    row.source = "live"
    session.add(row)
    session.commit()
    return row


def _up_from_breadth(metrics: dict) -> int | None:
    breadth = metrics.get("breadth")
    total = metrics.get("total")
    if breadth is None or not total:
        return None
    return int(round(float(breadth) / 100 * total))# ------------------------------------------------------------------ history
def backfill_from_bars(
    session: Session, *, days: int = 260, today: date | None = None
) -> dict:
    """从日线反推历史市场数据（近似），用于回测分组与历史回看。

    ``today`` 那天的数据由收盘快照写入（``source='live'``），这里跳过，
    避免用近似值覆盖精确值。
    """
    today = today or date.today()
    started = time.perf_counter()
    statement = (
        select(DailyBar.code, DailyBar.trade_date, DailyBar.close, DailyBar.amount)
        .order_by(DailyBar.code, DailyBar.trade_date)
    )
    frame = pd.read_sql_query(statement, session.connection())
    if frame.empty:
        return {"rows": 0, "days": 0}

    codes = [
        stock.code
        for stock in session.scalars(select(Stock).where(Stock.status == "normal"))
        if is_main_board(stock.code)
    ]
    frame = frame[frame["code"].isin(codes)].copy()
    frame["pct"] = frame.groupby("code")["close"].pct_change() * 100
    frame["limit_up"] = frame["pct"] >= 9.7
    frame["limit_down"] = frame["pct"] <= -9.7
    frame["up"] = frame["pct"] > 0
    frame["down"] = frame["pct"] < 0

    # 连板高度：按股票累计连续涨停天数
    groups = (~frame["limit_up"]).groupby(frame["code"]).cumsum()
    frame["streak"] = frame.groupby([frame["code"], groups])["limit_up"].cumsum()
    # 昨日涨停今日溢价：该股票上一根 K 线是否涨停
    frame["prev_limit"] = frame.groupby("code")["limit_up"].shift(1).fillna(False)

    aggregated = frame.groupby("trade_date").agg(
        total=("code", "count"),
        up=("up", "sum"),
        down=("down", "sum"),
        limit_up=("limit_up", "sum"),
        limit_down=("limit_down", "sum"),
        max_streak=("streak", "max"),
        amount=("amount", "sum"),
        median_pct=("pct", "median"),
    )
    premium = (
        frame[frame["prev_limit"]].groupby("trade_date")["pct"].mean().rename("prev_premium")
    )
    aggregated = aggregated.join(premium)
    aggregated = aggregated.tail(days)

    indices = index_rows(aggregated.index.min(), aggregated.index.max())
    written = 0
    for day, row in aggregated.iterrows():
        day = day if isinstance(day, date) else day.date()
        if day == today:
            continue  # 当天用精确快照覆盖
        total = int(row["total"]) or 1
        metrics = {
            "available": True,
            "total": total,
            "up": int(row["up"]),
            "down": int(row["down"]),
            "limit_up": int(row["limit_up"]),
            "limit_down": int(row["limit_down"]),
            "max_streak": int(row["max_streak"]),
            "break_rate": None,
            "prev_premium": _round(row["prev_premium"]),
            "breadth": round(int(row["up"]) / total * 100, 1),
            "median_pct": _round(row["median_pct"]),
            "amount_yi": round(float(row["amount"]) / 1e8, 1),
        }
        phase = classify(metrics)
        existing = session.get(MarketDaily, day)
        record = existing or MarketDaily(trade_date=day)
        record.sh_close = (indices.get(day) or {}).get("sh_close")
        record.sh_pct = (indices.get(day) or {}).get("sh_pct")
        record.sz_close = (indices.get(day) or {}).get("sz_close")
        record.sz_pct = (indices.get(day) or {}).get("sz_pct")
        record.cyb_close = (indices.get(day) or {}).get("cyb_close")
        record.cyb_pct = (indices.get(day) or {}).get("cyb_pct")
        record.zz1000_close = (indices.get(day) or {}).get("zz1000_close")
        record.zz1000_pct = (indices.get(day) or {}).get("zz1000_pct")
        record.amount_yi = metrics["amount_yi"]
        record.total = total
        record.up = metrics["up"]
        record.down = metrics["down"]
        record.flat = total - metrics["up"] - metrics["down"]
        record.limit_up = metrics["limit_up"]
        record.limit_down = metrics["limit_down"]
        record.max_streak = metrics["max_streak"]
        record.prev_premium = metrics["prev_premium"]
        record.breadth = metrics["breadth"]
        record.median_pct = metrics["median_pct"]
        record.phase = phase
        record.temperature = temperature(metrics)
        if existing is None:
            record.source = "bars"
        elif record.source != "live":
            record.source = "bars"
        session.add(record)
        written += 1
    session.commit()
    elapsed = round(time.perf_counter() - started, 1)
    logger.info("market_daily backfilled: {} days in {}s", written, elapsed)
    return {"rows": written, "days": len(aggregated), "elapsed_sec": elapsed}


def _round(value) -> float | None:
    if value is None or pd.isna(value):
        return None
    return round(float(value), 2)


# ------------------------------------------------------------------ readers
def load_history(session: Session, start: date | None = None, days: int = 60) -> list[MarketDaily]:
    statement = select(MarketDaily).order_by(MarketDaily.trade_date.desc())
    if start is not None:
        statement = statement.where(MarketDaily.trade_date >= start)
    rows = list(session.scalars(statement.limit(days)))
    return list(reversed(rows))


def latest(session: Session, before: date | None = None) -> MarketDaily | None:
    statement = select(MarketDaily).order_by(MarketDaily.trade_date.desc()).limit(1)
    if before is not None:
        statement = statement.where(MarketDaily.trade_date < before)
    return session.scalar(statement)


def phase_counts(session: Session, days: int = 120) -> dict[str, int]:
    rows = load_history(session, days=days)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.phase or "—"] = counts.get(row.phase or "—", 0) + 1
    return counts


__all__ = [
    "INDEXES",
    "backfill_from_bars",
    "fetch_index_daily",
    "index_rows",
    "latest",
    "load_history",
    "phase_counts",
    "snapshot",
]
