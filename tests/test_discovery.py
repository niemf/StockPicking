"""Discovery pipeline + bar storage integration tests (in-memory DB, fake quotes)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import Candidate, DailyBar, JobRun, Stock
from app.providers.base import Quote
from app.services.bars import upsert_bars
from app.services.discovery import load_bars_map, run_discovery, session_progress

TODAY = date(2026, 11, 10)
MOMENT = datetime(2026, 11, 10, 15, 30)


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
        db_session.add_all(
            [
                Stock(
                    code="600000",
                    name="测试股份",
                    exchange="sh",
                    board="沪市主板",
                    industry="银行",
                    list_date=date(2010, 1, 1),
                    status="normal",
                ),
                Stock(
                    code="600001",
                    name="弱势股份",
                    exchange="sh",
                    board="沪市主板",
                    industry="钢铁",
                    list_date=date(2010, 1, 1),
                    status="normal",
                ),
            ]
        )
        db_session.commit()
        yield db_session


def _bars(code: str, *, rising: bool = True, days: int = 120) -> pd.DataFrame:
    pattern = [0.8, -0.3, 0.5, -0.2, 0.9, -0.4]
    closes = [100.0]
    for index in range(1, days):
        if rising:
            change = pattern[(index - (days - 40)) % len(pattern)] / 100 if index >= days - 40 else (
                0.001 if index % 2 else -0.001
            )
        else:
            change = -0.004
        closes.append(closes[-1] * (1 + change))

    dates = pd.date_range(end=TODAY, periods=days, freq="B")
    rows = []
    for index, close in enumerate(closes):
        volume = 1_000_000.0 if index < days - 1 else 1_800_000.0
        rows.append(
            {
                "trade_date": dates[index].date(),
                "open": close * 0.999,
                "high": close * 1.012,
                "low": close * 0.988,
                "close": close,
                "volume": volume,
                "amount": close * volume,
                "turnover_rate": 3.0,
            }
        )
    return pd.DataFrame(rows)


def _quote(code: str, price: float, name: str) -> Quote:
    return Quote(
        code=code,
        name=name,
        price=price,
        pre_close=price * 0.995,
        open=price * 0.998,
        high=price * 1.01,
        low=price * 0.99,
        volume=1_800_000.0,
        amount=200_000_000.0,
        pct_chg=0.5,
        change=price * 0.005,
        turnover_rate=3.0,
        ts=MOMENT,
        source="test",
    )


class FakeQuoteService:
    def __init__(self, quotes):
        self._quotes = quotes

    def get_quotes(self, codes):
        return {code: self._quotes[code] for code in codes if code in self._quotes}


def test_upsert_bars_is_idempotent(session) -> None:
    frame = _bars("600000")
    first = upsert_bars(session, "600000", frame)
    session.commit()
    second = upsert_bars(session, "600000", frame)
    session.commit()

    assert first == second == len(frame)
    assert session.scalar(select(func.count()).select_from(DailyBar)) == len(frame)

    updated = frame.copy()
    updated.loc[updated.index[-1], "close"] = 123.45
    upsert_bars(session, "600000", updated)
    session.commit()
    latest = session.scalar(
        select(DailyBar.close).where(DailyBar.code == "600000").order_by(DailyBar.trade_date.desc()).limit(1)
    )
    assert latest == pytest.approx(123.45)


def test_load_bars_map_groups_by_code(session) -> None:
    upsert_bars(session, "600000", _bars("600000"))
    upsert_bars(session, "600001", _bars("600001", rising=False))
    session.commit()

    frames = load_bars_map(session, ["600000", "600001", "999999"])
    assert set(frames) == {"600000", "600001"}
    assert len(frames["600000"]) == 120
    assert frames["600000"]["trade_date"].is_monotonic_increasing


def test_session_progress() -> None:
    assert session_progress(datetime(2026, 11, 10, 15, 30)) == 1.0
    assert session_progress(datetime(2026, 11, 10, 9, 30)) == pytest.approx(0.1)
    assert session_progress(datetime(2026, 11, 10, 11, 30)) < 0.55
    assert session_progress(datetime(2026, 11, 10, 13, 0)) > 0.5


def test_run_discovery_persists_candidates(session) -> None:
    upsert_bars(session, "600000", _bars("600000"))
    upsert_bars(session, "600001", _bars("600001", rising=False))
    session.commit()

    quotes = {
        "600000": _quote("600000", float(_bars("600000")["close"].iloc[-1]), "测试股份"),
        "600001": _quote("600001", float(_bars("600001", rising=False)["close"].iloc[-1]), "弱势股份"),
    }

    output = run_discovery(
        session,
        quote_service=FakeQuoteService(quotes),
        today=TODAY,
        moment=MOMENT,
    )

    summary = output["summary"]
    assert summary["total_input"] == 2
    assert summary["survivors"] == 1
    assert len(output["candidates"]) == 1
    assert output["candidates"][0]["code"] == "600000"

    rows = list(session.scalars(select(Candidate)))
    assert len(rows) == 1
    assert rows[0].code == "600000"
    assert rows[0].indicator_snapshot["price_at_discovery"] > 0

    job = session.scalar(select(JobRun).where(JobRun.job_name == "discovery"))
    assert job is not None and job.status == "success"
    assert job.stats["stages"][0]["stage"] == 1


def test_rerun_replaces_candidates_for_the_same_day(session) -> None:
    upsert_bars(session, "600000", _bars("600000"))
    upsert_bars(session, "600001", _bars("600001", rising=False))
    session.commit()
    quotes = {
        "600000": _quote("600000", float(_bars("600000")["close"].iloc[-1]), "测试股份"),
        "600001": _quote("600001", float(_bars("600001", rising=False)["close"].iloc[-1]), "弱势股份"),
    }

    run_discovery(session, quote_service=FakeQuoteService(quotes), today=TODAY, moment=MOMENT)
    run_discovery(
        session,
        quote_service=FakeQuoteService(quotes),
        today=TODAY,
        moment=MOMENT + timedelta(minutes=5),
    )

    assert session.scalar(select(func.count()).select_from(Candidate)) == 1
    jobs = session.scalar(select(func.count()).select_from(JobRun))
    assert jobs == 2


def test_rerun_with_insufficient_bars_yields_no_candidates(session) -> None:
    quotes = {
        "600000": _quote("600000", 100.0, "测试股份"),
        "600001": _quote("600001", 100.0, "弱势股份"),
    }
    output = run_discovery(
        session, quote_service=FakeQuoteService(quotes), today=TODAY, moment=MOMENT
    )
    assert output["candidates"] == []
    assert output["summary"]["bars_loaded"] == 2
