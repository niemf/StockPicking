"""Web form flow tests (stage B): add / update / delete through the HTTP layer.

The quote provider is never called: every code used here already exists in the
in-memory ``stock`` table, and these routes do not render quote-bearing pages.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base, get_db
from app.db.models import AlertRule, Holding, HoldingTransaction, Stock, Watchlist
from app.main import app


@pytest.fixture()
def client():
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
    session = factory()
    session.add_all(
        [
            Stock(code="600036", name="招商银行", exchange="sh", board="沪市主板", industry="银行"),
            Stock(code="601988", name="中国银行", exchange="sh", board="沪市主板", industry="银行"),
        ]
    )
    session.commit()

    def _override():
        yield session

    app.dependency_overrides[get_db] = _override
    with TestClient(app, follow_redirects=False) as test_client:
        test_client.db = session  # type: ignore[attr-defined]
        yield test_client
    app.dependency_overrides.clear()
    session.close()


def _location(response) -> str:
    return response.headers["location"]


def test_watchlist_add_and_duplicate(client) -> None:
    response = client.post("/watchlist/add", data={"code": "601988", "note": "测试"})
    assert response.status_code == 303
    assert "ok=" in _location(response)

    row = client.db.scalar(select(Watchlist).where(Watchlist.code == "601988"))
    assert row is not None and row.source == "manual" and row.note == "测试"

    duplicate = client.post("/watchlist/add", data={"code": "601988"})
    assert duplicate.status_code == 303
    assert "error=" in _location(duplicate)


def test_watchlist_add_rejects_non_main_board(client) -> None:
    response = client.post("/watchlist/add", data={"code": "300750"})
    assert response.status_code == 303
    assert "error=" in _location(response)
    assert client.db.scalar(select(func.count()).select_from(Watchlist)) == 0


def test_watchlist_delete(client) -> None:
    client.post("/watchlist/add", data={"code": "601988"})
    row = client.db.scalar(select(Watchlist).where(Watchlist.code == "601988"))

    response = client.post(f"/watchlist/{row.id}/delete")
    assert response.status_code == 303
    assert "ok=" in _location(response)
    assert client.db.get(Watchlist, row.id) is None


def test_holding_add_update_stop_and_delete(client) -> None:
    created = client.post(
        "/holdings/add",
        data={
            "code": "601988",
            "cost_price": "4.5",
            "shares": "100",
            "stop_loss": "4.2",
            "take_profit_1": "5.2",
            "take_profit_2": "",
        },
    )
    assert created.status_code == 303
    holding = client.db.scalar(select(Holding).where(Holding.code == "601988"))
    assert holding is not None
    assert holding.shares == 100
    assert client.db.scalar(select(func.count()).select_from(HoldingTransaction)) == 1

    updated = client.post(f"/holdings/{holding.id}/stop", data={"current_stop": "4.4"})
    assert updated.status_code == 303
    assert holding.current_stop == pytest.approx(4.4)

    bad_stop = client.post(f"/holdings/{holding.id}/stop", data={"current_stop": "9.9"})
    assert "error=" in _location(bad_stop)

    deleted = client.post(f"/holdings/{holding.id}/delete")
    assert deleted.status_code == 303
    assert client.db.get(Holding, holding.id) is None


def test_holding_add_rejects_bad_share_lot(client) -> None:
    response = client.post(
        "/holdings/add",
        data={"code": "600036", "cost_price": "38", "shares": "150", "stop_loss": "36"},
    )
    assert response.status_code == 303
    assert "error=" in _location(response)
    assert client.db.scalar(select(func.count()).select_from(Holding)) == 0


def test_watchlist_import_export_and_inline_update(client) -> None:
    imported = client.post(
        "/watchlist/import",
        data={"codes": "600036,银行,招行,低波;红利\n601988", "group_name": "自选"},
    )
    assert imported.status_code == 303
    assert "ok=" in _location(imported)

    rows = {row.code: row for row in client.db.scalars(select(Watchlist))}
    assert rows["600036"].group_name == "银行"
    assert rows["600036"].tags == ["低波", "红利"]
    assert rows["601988"].group_name == "自选"

    export = client.get("/watchlist/export.csv")
    assert export.status_code == 200
    text = export.content.decode("utf-8-sig")
    assert "code,name,group,note,tags" in text
    assert "600036,招商银行,银行,招行,低波;红利" in text

    updated = client.post(
        f"/watchlist/{rows['601988'].id}/update",
        data={"group_name": "核心", "tags": "大金融", "note": "观察"},
    )
    assert updated.status_code == 303
    refreshed = client.db.get(Watchlist, rows["601988"].id)
    assert refreshed.group_name == "核心"
    assert refreshed.tags == ["大金融"]
    assert refreshed.note == "观察"


def test_watchlist_card_shows_tech_and_latest_report(client, monkeypatch) -> None:
    from app.providers.base import Quote

    client.post("/watchlist/add", data={"code": "600036"})

    quote = Quote(
        code="600036",
        name="招商银行",
        price=43.1,
        pre_close=43.0,
        open=43.0,
        high=43.3,
        low=42.8,
        volume=1e6,
        amount=4e8,
        pct_chg=0.2,
        change=0.1,
        source="test",
    )
    monkeypatch.setattr(
        "app.web.routes.quote_service.get_quotes", lambda codes: {"600036": quote}
    )
    monkeypatch.setattr(
        "app.web.routes.snapshot_many",
        lambda db, codes, quotes=None: {
            "600036": {
                "available": True,
                "code": "600036",
                "price": 43.1,
                "pct_chg": 0.2,
                "amount_yi": 4.0,
                "turnover_rate": 1.2,
                "ma5": 43.2,
                "ma10": 43.0,
                "ma20": 42.5,
                "ma30": 41.8,
                "ma60": 40.9,
                "ma20_dist": 1.4,
                "ma20_slope3": 0.6,
                "trend": "多头排列",
                "trend_class": "up",
                "volume_ratio": 1.32,
                "volume_state": "量能正常",
                "rsi12": 62.0,
                "atr_pct": 2.1,
                "support": 42.2,
                "resistance": 44.0,
                "support_dist": 2.1,
                "resistance_dist": 2.1,
                "dense_low": 42.8,
                "dense_high": 43.6,
                "score": 6,
                "verdict": "看多",
                "verdict_class": "up",
                "strength": 80,
                "macd_hist": 0.12,
                "key_signals": [
                    {"label": "站上 MA20", "detail": "+1.4%", "weight": 2},
                    {"label": "MA20 向上", "detail": "+0.6%", "weight": 2},
                    {"label": "接近压力位", "detail": "距压力 2.1%", "weight": -1},
                ],
                "bullish": [{"label": "站上 MA20", "detail": "+1.4%", "weight": 2}],
                "bearish": [{"label": "接近压力位", "detail": "距压力 2.1%", "weight": -1}],
                "summary": "现价 43.10；均线多头排列；综合评分 +6 → 看多。",
            }
        },
    )
    monkeypatch.setattr(
        "app.web.routes.latest_reports_for",
        lambda db, codes: {
            "600036": {
                "code": "600036",
                "badge": "success",
                "conclusion": "看多",
                "score": 72,
                "score_label": "偏强",
                "trade_date": "2026-11-11",
                "summary": "站上 MA20，量能温和，注意 44 附近压力。",
            }
        },
    )

    response = client.get("/watchlist")
    assert response.status_code == 200
    assert "看多" in response.text
    assert "+6 · 多头排列" in response.text
    assert "MA20" in response.text
    # 精简卡片只保留核心信息，支撑/压力/密集区已移到个股详情页
    assert "详情 →" in response.text
    assert "密集区" not in response.text
    assert "AI 研报卡：看多" in response.text
    assert "站上 MA20" in response.text
    assert "站上 MA20，量能温和" in response.text
    assert "重新生成" in response.text
    assert "分组 / 标签" not in response.text
    assert "银行" in response.text


def test_watchlist_analyze_endpoint(client, monkeypatch) -> None:
    from types import SimpleNamespace

    client.post("/watchlist/add", data={"code": "600036"})

    fake_row = SimpleNamespace(conclusion="看多", score=70)
    fake_log = SimpleNamespace(cost=0.0049)
    calls: list[dict] = []

    def fake_generate(db, code, **kwargs):
        calls.append(kwargs)
        return fake_row, False, [], fake_log

    monkeypatch.setattr("app.web.routes.generate_report", fake_generate)

    response = client.post("/watchlist/600036/analyze", data={"refresh": "1"})
    assert response.status_code == 303
    location = response.headers["location"]
    assert "ok=" in location and "70" in location
    assert calls[0]["force"] is True

    monkeypatch.setattr(
        "app.web.routes.generate_report",
        lambda db, code, **kwargs: (fake_row, True, [], None),
    )
    cached = client.post("/watchlist/600036/analyze")
    assert cached.status_code == 303
    assert "ok=" in cached.headers["location"]


def test_alerts_custom_rule_create_and_delete(client) -> None:
    client.post("/watchlist/add", data={"code": "600036"})

    created = client.post(
        "/alerts/rules",
        data={
            "code": "600036",
            "logic": "OR",
            "metric_a": "pct_chg",
            "op_a": ">=",
            "value_a": "5",
            "metric_b": "amount_yi",
            "op_b": ">=",
            "value_b": "2",
            "channel": "both",
            "max_per_day": "3",
            "expires_on": "",
        },
    )
    assert created.status_code == 303
    assert "error=" not in _location(created)

    rule = client.db.scalar(select(AlertRule).where(AlertRule.rule_type == "COMPOSITE"))
    assert rule is not None
    assert rule.params["logic"] == "OR"
    assert rule.params["channel"] == "both"
    assert rule.params["max_per_day"] == 3
    assert len(rule.params["conditions"]) == 2

    deleted = client.post(f"/alerts/rules/{rule.id}/delete")
    assert deleted.status_code == 303
    assert client.db.get(AlertRule, rule.id) is None


def test_report_card_api_reports_missing(client) -> None:
    response = client.get("/api/reports/600036")
    assert response.status_code == 200
    assert response.json() == {"available": False, "code": "600036"}

    bad = client.get("/api/reports/300750")
    assert bad.status_code == 404


def test_liveness_endpoint(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["app"]


def test_premarket_preview_api(client) -> None:
    response = client.get("/api/premarket")
    assert response.status_code == 200
    data = response.json()
    assert data["sections"]
    assert data["date"]
    assert "【昨日复盘】" in data["text"]

    bad = client.get("/api/premarket?day=2026-13-01")
    assert bad.status_code == 400


def test_premarket_page_renders(client) -> None:
    response = client.get("/premarket")
    assert response.status_code == 200
    assert "盘前简报" in response.text
    assert "推送到飞书" in response.text
    assert "纪律提醒" in response.text


def test_limit_board_page_renders(client, monkeypatch) -> None:
    pool = {
        "date": "2026-11-11",
        "available": True,
        "total": 2,
        "max_streak": 3,
        "first_boards": 1,
        "ladder": [
            {
                "streak": 3,
                "label": "3 板",
                "count": 1,
                "stocks": [
                    {
                        "code": "603366",
                        "name": "日出东方",
                        "industry": "厨卫电器",
                        "seal_yi": 7.8,
                        "breaks": 0,
                        "streak": 3,
                    }
                ],
            }
        ],
        "stocks": [
            {
                "code": "603366",
                "name": "日出东方",
                "industry": "厨卫电器",
                "pct_chg": 10.01,
                "seal_yi": 7.8,
                "first_seal": "09:25:01",
                "breaks": 0,
                "stat": "3/3",
                "streak": 3,
            }
        ],
    }
    seats = {
        "date": "2026-11-10",
        "available": True,
        "count": 1,
        "buy": [
            {
                "seat": "沪股通专用",
                "net_yi": 16.0,
                "buy_yi": 53.0,
                "sell_yi": 37.0,
                "stocks": ["金健米业"],
            }
        ],
        "sell": [],
    }
    monkeypatch.setattr("app.web.routes.get_limit_pool", lambda *args, **kwargs: pool)
    monkeypatch.setattr("app.web.routes.get_lhb_seats", lambda *args, **kwargs: seats)

    response = client.get("/limit-board")
    assert response.status_code == 200
    assert "涨停梯队" in response.text
    assert "日出东方" in response.text
    assert "09:25:01" in response.text
    assert "沪股通专用" in response.text
    assert "龙虎榜活跃席位" in response.text


def test_limit_board_page_handles_unavailable(client, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.web.routes.get_limit_pool",
        lambda *args, **kwargs: {"date": "2026-11-11", "available": False, "error": "限流", "stocks": []},
    )
    monkeypatch.setattr(
        "app.web.routes.get_lhb_seats",
        lambda *args, **kwargs: {"available": False, "error": "限流", "buy": [], "sell": []},
    )

    response = client.get("/limit-board")
    assert response.status_code == 200
    assert "涨停池暂不可用" in response.text
    assert "限流" in response.text


def test_analysis_api_single_and_batch(client, monkeypatch) -> None:
    def fake_snapshot(db, codes, quotes=None):
        return {
            code: {
                "available": True,
                "code": code,
                "verdict": "看多",
                "verdict_class": "up",
                "score": 5,
            }
            for code in codes
        }

    monkeypatch.setattr("app.web.routes.snapshot_many", fake_snapshot)
    monkeypatch.setattr("app.web.routes.quote_service.get_quote", lambda code: None)
    monkeypatch.setattr("app.web.routes.quote_service.get_quotes", lambda codes: {})

    single = client.get("/api/analysis/600036")
    assert single.status_code == 200
    assert single.json()["verdict"] == "看多"

    batch = client.get("/api/analysis?codes=600036,601318,300750")
    assert batch.status_code == 200
    payload = batch.json()
    assert payload["count"] == 2
    assert {item["code"] for item in payload["items"]} == {"600036", "601318"}

    bad = client.get("/api/analysis/300750")
    assert bad.status_code == 404


def test_analysis_api_reports_missing_bars(client, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.web.routes.snapshot_many",
        lambda db, codes, quotes=None: {code: {"available": False, "code": code} for code in codes},
    )
    monkeypatch.setattr("app.web.routes.quote_service.get_quote", lambda code: None)

    response = client.get("/api/analysis/600036")
    assert response.status_code == 404


def test_stock_detail_renders_report_panel(client, monkeypatch) -> None:
    from app.providers.base import Quote

    quote = Quote(
        code="600036",
        name="招商银行",
        price=13.0,
        pre_close=12.9,
        open=13.0,
        high=13.1,
        low=12.8,
        volume=1e6,
        amount=2e8,
        pct_chg=0.8,
        change=0.1,
        source="test",
    )
    monkeypatch.setattr("app.web.routes.quote_service.get_quote", lambda code: quote)
    monkeypatch.setattr(
        "app.web.routes.get_stock_fund_flow",
        lambda code: {
            "code": code,
            "date": "2026-11-10",
            "available": True,
            "latest": {
                "date": "2026-11-10",
                "main_yi": 1.1,
                "main_ratio": 6.31,
                "super_yi": 0.6,
                "big_yi": 0.5,
                "mid_yi": -0.9,
                "small_yi": -0.2,
            },
            "history": [
                {"date": "2026-11-09", "main_yi": -1.5},
                {"date": "2026-11-10", "main_yi": 1.1},
            ],
            "main_5d_yi": -0.4,
            "days": 5,
        },
    )

    monkeypatch.setattr(
        "app.web.routes.get_announcements",
        lambda code: {
            "code": code,
            "available": True,
            "days": 45,
            "count": 1,
            "entries": [
                {
                    "code": code,
                    "date": "2026-11-02",
                    "title": "关于回购股份进展的公告",
                    "type": "股东",
                    "url": "http://static.cninfo.com.cn/finalpage/2026-11-02/2.PDF",
                }
            ],
        },
    )

    response = client.get("/stock/600036")
    assert response.status_code == 200
    assert "AI 研报卡" in response.text
    assert "生成研报卡" in response.text
    assert "资金流" in response.text
    assert "主力（当日）" in response.text
    assert "巨潮资讯" in response.text
    assert "关于回购股份进展的公告" in response.text


def test_sectors_page_renders_table(client, monkeypatch) -> None:
    row = {
        "name": "半导体",
        "rank": 1,
        "pct_chg": 3.2,
        "net_inflow": 5.2e9,
        "net_inflow_yi": 52.0,
        "net_ratio": 8.1,
        "leader": "中芯国际",
    }
    flow = {
        "date": "2026-11-10",
        "available": True,
        "rows": [row],
        "count": 1,
        "up": 1,
        "down": 0,
        "inflow": [row],
        "outflow": [row],
        "source": "eastmoney",
    }
    monkeypatch.setattr("app.web.routes.get_sector_flow", lambda *args, **kwargs: flow)

    response = client.get("/sectors")
    assert response.status_code == 200
    assert "板块资金" in response.text
    assert "半导体" in response.text
    assert "中芯国际" in response.text
    assert "全部板块（1）" in response.text
