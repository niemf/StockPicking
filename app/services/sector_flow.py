"""板块资金分时采样与曲线数据。

数据源：``market_flow.get_sector_flow(refresh=True)``，返回的是**当日累计**
主力净流入（亿元），所以每 5 分钟存一次，就是第二张图里那种「多板块累计线」。

采样只在交易时段进行，由监控循环驱动；仪表盘只读这张表，不发请求。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from loguru import logger
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.models import SectorFlowSample
from app.services.calendar import is_polling_time, now_local
from app.services.market_flow import get_sector_flow

SAMPLE_MINUTES = 5
KEEP_DAYS = 20

# 曲线配色：流入暖色、流出冷色，和参考图一致
COLORS = ["#f0533f", "#f08a3f", "#f0b13f", "#ef6a86", "#c95fd0",
          "#2f9bd6", "#37b8a8", "#6a8be0", "#8a7bd8", "#59a86a"]


def last_sample(session: Session, day: date) -> datetime | None:
    return session.scalar(
        select(SectorFlowSample.ts)
        .where(SectorFlowSample.trade_date == day)
        .order_by(desc(SectorFlowSample.ts))
        .limit(1)
    )


def sample(
    session: Session,
    *,
    moment: datetime | None = None,
    force: bool = False,
) -> int:
    """采一次板块资金快照；非交易时段或未到间隔则跳过。"""
    moment = moment or now_local()
    if not force and not is_polling_time(moment):
        return 0

    day = moment.date()
    previous = last_sample(session, day)
    if previous is not None and not force:
        if moment - previous < timedelta(minutes=SAMPLE_MINUTES, seconds=-30):
            return 0

    payload = get_sector_flow(day, refresh=True)
    if not payload.get("available"):
        return 0

    ts = moment.replace(second=0, microsecond=0)
    written = 0
    for row in payload.get("rows") or []:
        value = row.get("net_inflow_yi")
        if value is None:
            continue
        session.merge(
            SectorFlowSample(
                trade_date=day,
                ts=ts,
                sector=str(row.get("name") or "")[:32],
                net_inflow=float(value),
                pct_chg=float(row["pct_chg"]) if row.get("pct_chg") is not None else None,
            )
        )
        written += 1
    session.commit()
    if written:
        logger.info("sector flow sampled: {} sectors at {}", written, ts.strftime("%H:%M"))
    return written


def prune(session: Session, *, keep_days: int = KEEP_DAYS) -> int:
    from sqlalchemy import delete

    cutoff = date.today() - timedelta(days=keep_days)
    result = session.execute(delete(SectorFlowSample).where(SectorFlowSample.trade_date < cutoff))
    session.commit()
    return int(result.rowcount or 0)


def intraday_series(
    session: Session,
    day: date | None = None,
    *,
    top: int = 5,
    bottom: int = 5,
) -> dict:
    """返回 ``{times, series}``：流入前 N 与流出前 N 的累计净流入曲线。"""
    day = day or date.today()
    rows = list(
        session.scalars(
            select(SectorFlowSample)
            .where(SectorFlowSample.trade_date == day)
            .order_by(SectorFlowSample.ts)
        )
    )
    if not rows:
        return {"available": False, "times": [], "series": []}

    times: list[str] = []
    values: dict[str, dict[datetime, float]] = {}
    for row in rows:
        stamp = row.ts
        label = stamp.strftime("%H:%M")
        if not times or times[-1] != label:
            times.append(label)
        values.setdefault(row.sector, {})[stamp] = float(row.net_inflow or 0.0)

    stamps = sorted({row.ts for row in rows})
    latest = {name: series.get(stamps[-1], 0.0) for name, series in values.items()}
    inflow = sorted(latest.items(), key=lambda item: -item[1])[:top]
    outflow = sorted(latest.items(), key=lambda item: item[1])[:bottom]
    picked = [name for name, _ in inflow] + [name for name, _ in outflow]

    series = []
    for index, name in enumerate(picked):
        series.append(
            {
                "name": name,
                "kind": "inflow" if name in dict(inflow) else "outflow",
                "color": COLORS[index % len(COLORS)],
                "value": round(latest[name], 2),
                "data": [
                    round(values[name][stamp], 2) if stamp in values[name] else None
                    for stamp in stamps
                ],
            }
        )

    return {
        "available": True,
        "date": day.isoformat(),
        "times": [stamp.strftime("%H:%M") for stamp in stamps],
        "series": series,
        "inflow": [{"name": name, "value": round(value, 2)} for name, value in inflow],
        "outflow": [{"name": name, "value": round(value, 2)} for name, value in outflow],
        "samples": len(stamps),
    }


__all__ = ["COLORS", "SAMPLE_MINUTES", "intraday_series", "last_sample", "prune", "sample"]
