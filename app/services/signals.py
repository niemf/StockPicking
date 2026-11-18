"""候选/预警的结果跟踪。

回答的是一个很朴素的问题：**当时推荐的票，后来到底涨没涨？**

- 记录：每次发现把候选写进 ``signal_log``（同一股票同一天只有一行）；
- 结算：用之后的日线逐日推进，算 T+1 / T+3 / T+5 收益、最大浮盈浮亏、
  是否先触及止损或目标；
- 统计：按情绪阶段、RR 档、评分档分组看胜率与期望，给回测和调参提供依据。

口径（故意保守）：

* 入场价 = 信号日收盘价（不加次日跳空优惠）；
* 止损/目标都按日线最高/最低价判断，同一天两者都触及时按**先止损**处理；
* 超过 5 个交易日仍未结算的记为 ``expired``，收益按第 5 日收盘计。
"""

from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Candidate, SignalLog
from app.services.bars import load_bars_map

HORIZON = 5


def record_candidates(
    session: Session,
    trade_date: date,
    candidates: list[dict],
    *,
    phase: str | None = None,
    source: str = "candidate",
) -> int:
    """把当日候选写进跟踪表（重复运行不会产生重复记录）。"""
    if not candidates:
        return 0
    existing = set(
        session.scalars(
            select(SignalLog.code).where(
                SignalLog.trade_date == trade_date, SignalLog.source == source
            )
        )
    )
    added = 0
    for item in candidates:
        code = item.get("code")
        if not code or code in existing:
            continue
        risk = item.get("risk") or {}
        entry = risk.get("entry") or item.get("price")
        stop = risk.get("stop")
        session.add(
            SignalLog(
                trade_date=trade_date,
                code=code,
                name=item.get("name") or code,
                source=source,
                phase=phase,
                score=item.get("score"),
                rr=item.get("rr"),
                entry=entry,
                stop=stop,
                target1=risk.get("target1"),
                risk_pct=risk.get("stop_pct"),
                status="open",
                note=(item.get("industry") or None),
            )
        )
        added += 1
    session.commit()
    return added


def track(session: Session, *, as_of: date | None = None, horizon: int = HORIZON) -> dict:
    """结算所有未完成的信号，返回处理数量。"""
    as_of = as_of or date.today()
    pending = list(
        session.scalars(
            select(SignalLog)
            .where(SignalLog.status == "open", SignalLog.trade_date < as_of)
            .order_by(SignalLog.trade_date)
        )
    )
    if not pending:
        return {"checked": 0, "resolved": 0}

    codes = sorted({row.code for row in pending})
    bars = load_bars_map(session, codes)
    resolved = 0

    for signal in pending:
        frame = bars.get(signal.code)
        if frame is None or frame.empty:
            continue
        future = frame[frame["trade_date"] > signal.trade_date].head(horizon)
        if future.empty:
            continue

        entry = float(signal.entry or 0)
        if entry <= 0:
            continue

        closes = future["close"].astype(float).tolist()
        highs = future["high"].astype(float).tolist()
        lows = future["low"].astype(float).tolist()
        days = future["trade_date"].tolist()

        stop = float(signal.stop) if signal.stop else None
        target = float(signal.target1) if signal.target1 else None

        signal.t1_ret = round((closes[0] / entry - 1) * 100, 2)
        if len(closes) >= 3:
            signal.t3_ret = round((closes[2] / entry - 1) * 100, 2)
        if len(closes) >= 5:
            signal.t5_ret = round((closes[4] / entry - 1) * 100, 2)
        signal.mfe = round((max(highs) / entry - 1) * 100, 2)
        signal.mae = round((min(lows) / entry - 1) * 100, 2)

        status = "open"
        exit_day = None
        exit_price = None
        for index, day in enumerate(days):
            hit_stop = stop is not None and lows[index] <= stop
            hit_target = target is not None and highs[index] >= target
            if hit_stop:  # 同日双触按最坏情况处理
                status, exit_day, exit_price = "stop", day, stop
                break
            if hit_target:
                status, exit_day, exit_price = "target", day, target
                break
        if status == "open" and len(closes) >= horizon:
            status, exit_day, exit_price = "expired", days[-1], closes[-1]

        signal.status = status
        if exit_day is not None:
            signal.exit_date = exit_day
            signal.exit_price = round(float(exit_price), 3)
        signal.tracked_at = datetime.now()

        reference = signal.exit_price if signal.exit_price else closes[-1]
        if signal.risk_pct:
            signal.r_multiple = round((reference / entry - 1) * 100 / float(signal.risk_pct), 2)
        if status != "open":
            resolved += 1

    session.commit()
    return {"checked": len(pending), "resolved": resolved}


def backfill_from_candidates(session: Session, *, days: int = 60) -> int:
    """把历史候选补录进跟踪表（功能上线前的候选也能参与统计）。"""
    cutoff = date.today() - timedelta(days=days)
    existing = {
        (row[0], row[1], row[2])
        for row in session.execute(
            select(SignalLog.trade_date, SignalLog.code, SignalLog.source)
        )
    }
    job_phase = {}
    added = 0
    for row in session.scalars(
        select(Candidate).where(Candidate.trade_date >= cutoff).order_by(Candidate.trade_date)
    ):
        strategy = row.strategy or "candidate"
        if (row.trade_date, row.code, strategy) in existing:
            continue
        snapshot = row.indicator_snapshot or {}
        risk = snapshot.get("risk") or {}
        entry = risk.get("entry") or snapshot.get("price_at_discovery")
        if not entry:
            continue
        phase = job_phase.get(row.trade_date)
        if phase is None:
            phase = _phase_for(session, row.trade_date)
            job_phase[row.trade_date] = phase
        session.add(
            SignalLog(
                trade_date=row.trade_date,
                code=row.code,
                name=row.name,
                source=strategy,
                phase=phase,
                score=row.score,
                rr=risk.get("rr"),
                entry=entry,
                stop=risk.get("stop"),
                target1=risk.get("target1"),
                risk_pct=risk.get("stop_pct"),
                status="open",
                note=snapshot.get("industry"),
            )
        )
        added += 1
    session.commit()
    return added


def _phase_for(session: Session, trade_date: date) -> str | None:
    from app.db.models import JobRun

    job = session.scalar(
        select(JobRun)
        .where(JobRun.job_name == "discovery", JobRun.trade_date == trade_date)
        .order_by(JobRun.id.desc())
        .limit(1)
    )
    stats = (job.stats or {}) if job else {}
    cycle = stats.get("cycle") or {}
    return cycle.get("phase")


def stats(session: Session, *, days: int = 60, source: str | None = None) -> dict:
    """近 N 天信号的表现：胜率、平均收益、期望 R、按阶段拆分。"""
    statement = select(SignalLog).order_by(SignalLog.trade_date.desc()).limit(days * 40)
    if source:
        statement = statement.where(SignalLog.source == source)
    else:
        statement = statement.where(SignalLog.source.in_(("candidate", "indicator", "youshi")))
    rows = list(session.scalars(statement))
    if not rows:
        return {"total": 0, "resolved": 0, "by_phase": {}, "by_source": {}}

    resolved = [row for row in rows if row.status != "open"]
    returns = [row.t5_ret for row in resolved if row.t5_ret is not None]
    r_multiples = [row.r_multiple for row in resolved if row.r_multiple is not None]

    by_phase: dict[str, dict] = {}
    for row in resolved:
        bucket = by_phase.setdefault(row.phase or "—", {"count": 0, "wins": 0, "returns": [], "rs": []})
        bucket["count"] += 1
        if row.t5_ret is not None:
            bucket["returns"].append(row.t5_ret)
            if row.t5_ret > 0:
                bucket["wins"] += 1
        if row.r_multiple is not None:
            bucket["rs"].append(row.r_multiple)

    summary = {
        "total": len(rows),
        "resolved": len(resolved),
        "open": len(rows) - len(resolved),
        "win_rate": round(sum(1 for value in returns if value > 0) / len(returns) * 100, 1) if returns else None,
        "avg_t5": round(statistics.fmean(returns), 2) if returns else None,
        "expectancy_r": round(statistics.fmean(r_multiples), 2) if r_multiples else None,
        "avg_mfe": round(statistics.fmean([row.mfe for row in resolved if row.mfe is not None]), 2)
        if resolved
        else None,
        "avg_mae": round(statistics.fmean([row.mae for row in resolved if row.mae is not None]), 2)
        if resolved
        else None,
        "by_phase": {
            phase: {
                "count": bucket["count"],
                "win_rate": round(bucket["wins"] / len(bucket["returns"]) * 100, 1)
                if bucket["returns"]
                else None,
                "avg_t5": round(statistics.fmean(bucket["returns"]), 2) if bucket["returns"] else None,
                "expectancy_r": round(statistics.fmean(bucket["rs"]), 2) if bucket["rs"] else None,
            }
            for phase, bucket in by_phase.items()
        },
    }

    by_source: dict[str, dict] = {}
    for row in resolved:
        key = row.source or "candidate"
        bucket = by_source.setdefault(key, {"count": 0, "returns": [], "rs": []})
        bucket["count"] += 1
        if row.t5_ret is not None:
            bucket["returns"].append(row.t5_ret)
        if row.r_multiple is not None:
            bucket["rs"].append(row.r_multiple)
    summary["by_source"] = {
        key: {
            "count": bucket["count"],
            "win_rate": round(
                sum(1 for value in bucket["returns"] if value > 0) / len(bucket["returns"]) * 100, 1
            )
            if bucket["returns"]
            else None,
            "avg_t5": round(statistics.fmean(bucket["returns"]), 2) if bucket["returns"] else None,
            "expectancy_r": round(statistics.fmean(bucket["rs"]), 2) if bucket["rs"] else None,
        }
        for key, bucket in by_source.items()
    }
    return summary


def recent(session: Session, *, limit: int = 40) -> list[SignalLog]:
    return list(
        session.scalars(
            select(SignalLog).order_by(SignalLog.trade_date.desc(), SignalLog.id.desc()).limit(limit)
        )
    )


__all__ = ["HORIZON", "backfill_from_candidates", "recent", "record_candidates", "stats", "track"]
