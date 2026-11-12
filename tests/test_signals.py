"""信号跟踪：记录、T+N 结算、止损/目标判定与分组统计。"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import SignalLog
from app.services.bars import upsert_bars
from app.services.signals import record_candidates, stats, track

T = date(2026, 11, 2)


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db_session:
        yield db_session


def bars_after(day: date, closes: list[float], *, highs=None, lows=None) -> pd.DataFrame:
    """Bars starting the next weekday after ``day``."""
    dates = pd.bdate_range(start=day + timedelta(days=1), periods=len(closes))
    rows = []
    for index, timestamp in enumerate(dates):
        close = closes[index]
        rows.append(
            {
                "trade_date": timestamp.date(),
                "open": close,
                "high": highs[index] if highs else close * 1.01,
                "low": lows[index] if lows else close * 0.99,
                "close": close,
                "volume": 1_000_000.0,
                "amount": close * 1_000_000.0,
                "turnover_rate": 3.0,
            }
        )
    return pd.DataFrame(rows)


def make_signal(session, *, stop=9.5, target=11.0, entry=10.0, phase="ferment", code="600000"):
    session.add(
        SignalLog(
            trade_date=T,
            code=code,
            name="测试",
            source="candidate",
            phase=phase,
            score=70.0,
            rr=2.5,
            entry=entry,
            stop=stop,
            target1=target,
            risk_pct=5.0,
            status="open",
        )
    )
    session.commit()


def test_record_candidates_is_idempotent(session) -> None:
    candidates = [
        {
            "code": "600000",
            "name": "测试",
            "score": 70.0,
            "rr": 2.5,
            "industry": "银行",
            "risk": {"entry": 10.0, "stop": 9.5, "target1": 11.0, "stop_pct": 5.0},
        }
    ]

    assert record_candidates(session, T, candidates, phase="ferment") == 1
    assert record_candidates(session, T, candidates, phase="ferment") == 0
    assert session.scalar(select(func.count()).select_from(SignalLog)) == 1

    row = session.scalar(select(SignalLog))
    assert row.entry == 10.0
    assert row.risk_pct == 5.0
    assert row.phase == "ferment"
    assert row.status == "open"


def test_track_marks_target_when_high_hits(session) -> None:
    make_signal(session, target=10.5)
    upsert_bars(session, "600000", bars_after(T, [10.1, 10.3, 10.6, 10.4]))
    session.commit()

    result = track(session, as_of=T + timedelta(days=8))

    row = session.scalar(select(SignalLog))
    assert result["checked"] == 1
    assert row.status == "target"
    assert row.exit_price == pytest.approx(10.5)
    assert row.t1_ret == pytest.approx(1.0, abs=0.01)
    assert row.mfe is not None and row.mfe > 0


def test_track_prefers_stop_when_both_hit_same_day(session) -> None:
    make_signal(session, stop=9.5, target=10.5)
    upsert_bars(
        session,
        "600000",
        bars_after(T, [9.4, 10.0], highs=[10.6, 10.2], lows=[9.3, 9.8]),
    )
    session.commit()

    track(session, as_of=T + timedelta(days=8))

    row = session.scalar(select(SignalLog))
    assert row.status == "stop"
    assert row.exit_price == pytest.approx(9.5)
    assert row.mae is not None and row.mae < -5


def test_track_expires_after_horizon(session) -> None:
    make_signal(session, stop=8.0, target=13.0)
    upsert_bars(session, "600000", bars_after(T, [10.1, 10.2, 10.15, 10.3, 10.25, 10.4]))
    session.commit()

    track(session, as_of=T + timedelta(days=12))

    row = session.scalar(select(SignalLog))
    assert row.status == "expired"
    assert row.t5_ret == pytest.approx(2.5, abs=0.01)
    assert row.t3_ret is not None
    assert row.r_multiple is not None


def test_track_leaves_short_history_open(session) -> None:
    make_signal(session, stop=8.0, target=13.0)
    upsert_bars(session, "600000", bars_after(T, [10.1, 10.2]))
    session.commit()

    track(session, as_of=T + timedelta(days=12))

    row = session.scalar(select(SignalLog))
    assert row.status == "open"
    assert row.t1_ret is not None
    assert row.t5_ret is None


def test_track_ignores_signals_without_future_bars(session) -> None:
    make_signal(session)
    upsert_bars(session, "600000", bars_after(T, [10.1]))
    session.commit()
    session.execute(SignalLog.__table__.delete())
    session.commit()

    # 只有当天之前的信号才会被结算
    make_signal(session)
    result = track(session, as_of=T)

    assert result["checked"] == 0


def test_stats_reports_win_rate_and_expectancy(session) -> None:
    make_signal(session, code="600000", stop=9.0, target=13.0, phase="ferment")
    make_signal(session, code="600001", stop=9.0, target=13.0, phase="ebb")
    upsert_bars(session, "600000", bars_after(T, [10.5, 10.8, 11.0, 11.2, 11.5]))
    upsert_bars(session, "600001", bars_after(T, [9.6, 9.4, 9.2, 9.1, 9.0]))
    session.commit()

    track(session, as_of=T + timedelta(days=12))
    summary = stats(session, days=10)

    assert summary["total"] == 2
    assert summary["resolved"] == 2
    assert summary["win_rate"] == pytest.approx(50.0)
    assert summary["by_phase"]["ferment"]["count"] == 1
    assert summary["by_phase"]["ebb"]["count"] == 1
    assert summary["by_phase"]["ebb"]["win_rate"] == pytest.approx(0.0)


def test_stats_handles_empty_table(session) -> None:
    summary = stats(session, days=30)
    assert summary["total"] == 0
    assert summary["by_phase"] == {}
