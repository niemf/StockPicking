"""每日收盘后的回测验证：用最近 N 个交易日滚动评估两套选股。

只做两件事：

* 用同一份面板数据，把「指标选股」和「游资选股」各回测一遍；
* 把结果写进 ``job_run``（job_name = ``backtest_validate``），页面只读这张表。

这样每天都能看到策略有没有衰减，而不必等到"亏了才发现"。
注意：面板加载约 700MB 内存，只在盘后脚本进程里跑，不在 Web 进程里跑。
"""

from __future__ import annotations

from datetime import date, datetime

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.pipeline import load_profile
from app.db.models import JobRun, MarketDaily
from app.services.discovery import STRATEGY_PROFILES

FIELDS = (
    "trades",
    "win_rate",
    "expectancy_r",
    "total_r",
    "max_drawdown_r",
    "avg_fwd5",
    "edge_fwd5",
    "stop_rate",
    "target_rate",
)


def latest_validation(session: Session) -> dict:
    """最近一次验证结果，按策略分组。"""
    job = session.scalar(
        select(JobRun)
        .where(JobRun.job_name == "backtest_validate")
        .order_by(JobRun.id.desc())
        .limit(1)
    )
    if job is None or not job.stats:
        return {}
    stats = dict(job.stats)
    stats.pop("stages", None)
    return stats


def run_validation(
    session: Session,
    *,
    days: int = 60,
    max_hold: int = 5,
    moment=None,
) -> dict:
    """滚动回测两套策略并落库。"""
    from app.backtest import load_panels, run_backtest

    recent = list(
        session.scalars(
            select(MarketDaily.trade_date).order_by(MarketDaily.trade_date.desc()).limit(days)
        )
    )
    start = min(recent) if recent else None
    trade_date = date.today()

    panels, by_date = load_panels(session)
    stats: dict = {"window": days, "start": start.isoformat() if start else None}
    for strategy, path in STRATEGY_PROFILES.items():
        profile = load_profile(path)
        result = run_backtest(
            session,
            start=start,
            profile=profile,
            max_hold=max_hold,
            panels=panels,
            by_date=by_date,
        )
        summary = result["summary"]
        stats[strategy] = {key: summary.get(key) for key in FIELDS} | {
            "sessions": summary.get("sessions"),
            "avg_picks": summary.get("avg_picks"),
        }

    session.add(
        JobRun(
            job_name="backtest_validate",
            trade_date=trade_date,
            scheduled_at=(moment or datetime.now()).replace(microsecond=0),
            started_at=moment or datetime.now(),
            finished_at=datetime.now(),
            status="success",
            stats=stats,
        )
    )
    session.commit()
    logger.info("validation done: {}", {key: value for key, value in stats.items() if key != "window"})
    return stats


__all__ = ["latest_validation", "run_validation"]
