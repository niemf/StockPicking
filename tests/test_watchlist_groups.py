"""Watchlist grouping, tags, pinyin search, bulk import and schema migration."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base, _ensure_columns
from app.db.models import Stock
from app.services.portfolio import (
    add_watch,
    filter_watches,
    import_watches,
    update_watch,
    watch_groups,
    watch_items,
)


@pytest.fixture()
def session():
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
                Stock(code="600036", name="招商银行", exchange="sh", board="沪市主板", industry="银行"),
                Stock(code="000001", name="平安银行", exchange="sz", board="深市主板", industry="银行"),
                Stock(code="002594", name="比亚迪", exchange="sz", board="深市主板", industry="汽车"),
            ]
        )
        db_session.commit()
        yield db_session


def test_group_tags_and_pinyin_search(session) -> None:
    add_watch(session, "600036", group_name="银行", tags=["低波", "红利"])
    add_watch(session, "002594", group_name="新能源车", tags=["成长"])

    items = watch_items(session)
    by_code = {item["code"]: item for item in items}
    assert by_code["600036"]["group"] == "银行"
    assert by_code["600036"]["watch_tags"] == ["低波", "红利"]

    # Full pinyin and initials both match.
    assert [item["code"] for item in filter_watches(items, query="zhaoshang")] == ["600036"]
    assert [item["code"] for item in filter_watches(items, query="zsyh")] == ["600036"]
    assert [item["code"] for item in filter_watches(items, query="比亚迪")] == ["002594"]

    assert [item["code"] for item in filter_watches(items, group="新能源车")] == ["002594"]
    assert len(filter_watches(items, group="全部")) == 2
    assert watch_groups(session) == ["新能源车", "银行"]


def test_update_watch_group_and_tags(session) -> None:
    row = add_watch(session, "600036")
    assert row.group_name == "默认"

    update_watch(session, row.id, group_name="核心", tags=["大金融"])
    item = watch_items(session)[0]
    assert item["group"] == "核心"
    assert item["watch_tags"] == ["大金融"]


def test_import_watches_handles_duplicates_and_errors(session) -> None:
    add_watch(session, "600036")
    result = import_watches(
        session,
        "\n".join(
            [
                "# 代码,分组,备注,标签",
                "600036,银行",             # duplicate
                "000001,银行,平安,低波;红利",
                "300750,创业板",           # not main board
                "002594,,比亚迪",
                "bad-code",
            ]
        ),
        default_group="自选",
    )

    assert result["added"] == ["000001", "002594"]
    assert result["skipped"] == ["600036"]
    assert len(result["errors"]) == 2
    assert result["total"] == 5

    items = {item["code"]: item for item in watch_items(session)}
    assert items["000001"]["group"] == "银行"
    assert items["000001"]["watch_tags"] == ["低波", "红利"]
    assert items["002594"]["group"] == "自选"


def test_schema_migration_adds_watchlist_columns() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE watchlist (id INTEGER PRIMARY KEY, code VARCHAR(6))")
        )

    _ensure_columns(engine)
    columns = {column["name"] for column in inspect(engine).get_columns("watchlist")}
    assert {"group_name", "tags"} <= columns
