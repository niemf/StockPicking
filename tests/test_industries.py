"""Industry label normalization and bulk sync (fake fetcher, no network)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import Stock
from app.services import industries


def test_short_industry_mapping_and_fallback() -> None:
    assert industries.short_industry("J66货币金融服务") == "银行"
    assert industries.short_industry("C32有色金属冶炼和压延加工业") == "有色金属"
    assert industries.short_industry("C36汽车制造业") == "汽车"
    # Unknown code: strip the prefix, keep a short tail.
    assert industries.short_industry("X99超长行业名称示例") == "超长行业名称示例"
    assert industries.short_industry(None) is None
    assert industries.short_industry("  ") is None


@pytest.fixture()
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(industries, "CACHE_FILE", tmp_path / "industry_sync.json")

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db_session:
        db_session.add_all(
            [
                Stock(code="600036", name="招商银行", exchange="sh", board="沪市主板", industry="股份制银行"),
                Stock(code="600641", name="先导基电", exchange="sh", board="沪市主板", industry=None),
                Stock(code="002594", name="比亚迪", exchange="sz", board="深市主板", industry="汽车整车"),
            ]
        )
        db_session.commit()
        yield db_session


def test_sync_industries_fills_and_normalizes(session) -> None:
    rows = [
        {"code": "sh.600036", "industry": "J66货币金融服务"},
        {"code": "sh.600641", "industry": "C32有色金属冶炼和压延加工业"},
        {"code": "sz.002594", "industry": "C36汽车制造业"},
    ]

    stats = industries.sync_industries(session, fetcher=lambda: rows, force=True, today=date(2026, 11, 11))

    assert stats["updated"] == 3
    assert session.get(Stock, "600036").industry == "银行"
    assert session.get(Stock, "600641").industry == "有色金属"
    assert session.get(Stock, "002594").industry == "汽车"
    assert industries.last_sync() == date(2026, 11, 11)


def test_sync_skips_within_seven_days(session) -> None:
    industries.sync_industries(
        session,
        fetcher=lambda: [{"code": "sh.600641", "industry": "C32有色金属冶炼和压延加工业"}],
        force=True,
        today=date(2026, 11, 11),
    )

    def boom():
        raise AssertionError("should not fetch inside the cache window")

    skipped = industries.sync_industries(session, fetcher=boom, today=date(2026, 11, 15))
    assert skipped["skipped"] is True

    fresh = industries.needs_sync(today=date(2026, 11, 19))
    assert fresh is True
