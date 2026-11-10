"""AI client, plan validation, confirmation and review tests (no network)."""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db.base import Base
from app.db.models import AICallLog, Candidate, ReviewReport, Stock, TradePlan, Watchlist
from app.providers.base import Quote
from app.services.ai import AIError, DeepSeekClient, usage_summary
from app.services.plans import confirm_plan, generate_plans, validate_plan
from app.services.review import generate_review
from app.services.quotes import QuoteService

TRADE_DATE = date(2026, 11, 10)
MOMENT = datetime(2026, 11, 10, 15, 30)


def fake_quote(code: str = "600000", price: float = 10.0) -> Quote:
    return Quote(
        code=code,
        name="测试股份",
        price=price,
        pre_close=price * 0.99,
        open=price * 0.995,
        high=price * 1.01,
        low=price * 0.99,
        volume=1_000_000.0,
        amount=100_000_000.0,
        pct_chg=0.5,
        change=0.05,
        turnover_rate=3.0,
        ts=MOMENT,
        source="test",
    )


PLAN_JSON = {
    "thesis": "均线多头，量价配合。",
    "fundamental_view": "估值合理。",
    "technical_view": "MA20 向上，MACD 多头。",
    "catalysts": ["行业景气"],
    "risks": ["大盘调整"],
    "buy_zone_low": 9.7,
    "buy_zone_high": 10.2,
    "stop_loss": 9.3,
    "take_profit_1": 11.0,
    "take_profit_2": 11.8,
    "position_pct": 8,
    "confidence": 72,
    "holding_horizon": "5-10个交易日",
    "invalid_conditions": ["收盘跌破 MA20"],
    "rating": "看多",
    "risk_score": 42,
    "tags": ["趋势", "放量"],
}


def plan_transport(_url, _headers, _payload):
    content = json.dumps(PLAN_JSON, ensure_ascii=False)
    body = {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 450},
    }
    return 200, json.dumps(body)


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
        db_session.add(
            Stock(
                code="600000",
                name="测试股份",
                exchange="sh",
                board="沪市主板",
                industry="银行",
                list_date=date(2010, 1, 1),
                status="normal",
            )
        )
        db_session.add(
            Candidate(
                trade_date=TRADE_DATE,
                code="600000",
                name="测试股份",
                score=82.5,
                rank=1,
                rule_hits=["MA20 向上"],
                indicator_snapshot={
                    "price_at_discovery": 10.0,
                    "pct_chg_at_discovery": 0.5,
                    "ma20": 9.8,
                    "volume_ratio": 1.6,
                    "rsi12": 61.0,
                    "macd_hist": 0.12,
                    "atr_pct": 2.8,
                    "ret20": 6.5,
                    "high20_prev": 10.2,
                    "drawdown20": -1.0,
                    "score_breakdown": {"trend": 80, "volume_price": 75},
                },
                status="pending",
            )
        )
        db_session.commit()
        yield db_session


def test_complete_json_parses_response() -> None:
    client = DeepSeekClient(api_key="test", transport=plan_transport, sleep=lambda _: None)
    data, usage = client.complete_json("system", "user")
    assert data["thesis"].startswith("均线")
    assert usage.input_tokens == 1200
    assert usage.output_tokens == 450


def test_missing_api_key_is_reported() -> None:
    client = DeepSeekClient(api_key="", transport=plan_transport)
    with pytest.raises(AIError, match="DEEPSEEK_API_KEY"):
        client.complete_json("system", "user")


def test_budget_blocks_call(session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "ai_daily_budget_cny", 0.001)
    session.add(
        AICallLog(
            purpose="plan",
            model="deepseek-chat",
            cost=1.0,
            status="success",
            created_at=datetime.now(),
        )
    )
    session.commit()
    client = DeepSeekClient(api_key="test", transport=plan_transport)
    with pytest.raises(AIError, match="预算"):
        client.call(
            session,
            purpose="plan",
            prompt_version="test",
            system="s",
            user="u",
            code="600000",
        )


def test_validate_plan_repairs_invalid_relationships() -> None:
    cleaned, notes = validate_plan(
        {
            "buy_zone_low": 12,
            "buy_zone_high": 10,
            "stop_loss": 13,
            "take_profit_1": 9,
            "take_profit_2": 8,
            "position_pct": 99,
            "confidence": 150,
            "risk_score": -5,
            "rating": "强烈买入",
        },
        price=11,
    )
    assert cleaned["buy_zone_low"] < cleaned["buy_zone_high"]
    assert cleaned["stop_loss"] < cleaned["buy_zone_low"]
    assert cleaned["take_profit_1"] > cleaned["buy_zone_high"]
    assert cleaned["take_profit_2"] >= cleaned["take_profit_1"]
    assert cleaned["position_pct"] == 15
    assert cleaned["confidence"] == 100
    assert cleaned["risk_score"] == 0
    assert cleaned["rating"] == "观察"
    assert notes


def test_generate_and_confirm_plan(session, monkeypatch) -> None:
    monkeypatch.setattr(QuoteService, "get_quote", lambda self, code: fake_quote(code))
    client = DeepSeekClient(api_key="test", transport=plan_transport, sleep=lambda _: None)

    result = generate_plans(session, limit=1, client=client, trade_date=TRADE_DATE)
    assert result["created"] == 1
    assert result["failed"] == []

    plan = session.scalar(select(TradePlan).where(TradePlan.code == "600000"))
    assert plan is not None
    assert plan.status == "draft"
    assert plan.source == "ai"
    assert plan.stop_loss < plan.buy_zone_low
    assert plan.take_profit_1 > plan.buy_zone_high
    assert session.scalar(select(func.count()).select_from(AICallLog)) == 1

    # A second run without force uses the cached draft and spends nothing.
    cached = generate_plans(session, limit=1, client=client, trade_date=TRADE_DATE)
    assert cached["cached"] == 1
    assert cached["created"] == 0
    assert session.scalar(select(func.count()).select_from(AICallLog)) == 1

    confirmed = confirm_plan(session, plan.id)
    assert confirmed.status == "confirmed"
    watch = session.scalar(select(Watchlist).where(Watchlist.code == "600000"))
    assert watch is not None and watch.plan_id == plan.id
    assert session.scalar(
        select(Candidate).where(Candidate.code == "600000", Candidate.trade_date == TRADE_DATE)
    ).status == "confirmed"


def test_usage_summary(session, monkeypatch) -> None:
    monkeypatch.setattr(QuoteService, "get_quote", lambda self, code: fake_quote(code))
    client = DeepSeekClient(api_key="test", transport=plan_transport, sleep=lambda _: None)
    generate_plans(session, limit=1, client=client, trade_date=TRADE_DATE)

    summary = usage_summary(session)
    assert summary["calls"] == 1
    assert summary["input_tokens"] == 1200
    assert summary["output_tokens"] == 450
    assert summary["cost"] > 0


def test_review_without_ai_and_with_ai(session, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.review.QuoteService.get_index_quotes", lambda self, codes: {}
    )
    monkeypatch.setattr("app.services.review.QuoteService.get_quotes", lambda self, codes: {})
    monkeypatch.setattr(
        "app.services.review.get_sector_flow",
        lambda day: {"available": False, "performance_top": [], "performance_bottom": []},
    )
    monkeypatch.setattr(
        "app.services.review.get_lhb", lambda day: {"available": False, "total": 0}
    )

    report = generate_review(session, trade_date=TRADE_DATE, use_ai=False)
    assert report.market_summary
    assert report.ai_narrative is None
    assert session.scalar(select(func.count()).select_from(ReviewReport)) == 1

    review_transport = lambda _u, _h, _p: (
        200,
        json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "market_narrative": "指数震荡，等待方向。",
                                    "watch_narrative": "自选表现平稳。",
                                    "holding_narrative": "持仓浮盈。",
                                    "sector_narrative": "半导体资金流入。",
                                    "lhb_narrative": "龙虎榜净买入集中在银行。",
                                    "operation_narrative": "今日无操作。",
                                    "tomorrow_focus": ["关注成交量"],
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 800, "completion_tokens": 300},
            }
        ),
    )
    client = DeepSeekClient(api_key="test", transport=review_transport, sleep=lambda _: None)
    report = generate_review(
        session, trade_date=TRADE_DATE, use_ai=True, client=client, force=True
    )
    assert "指数震荡，等待方向。" in report.ai_narrative
    assert "半导体资金流入。" in report.ai_narrative
    assert report.raw["usage"]["input_tokens"] == 800
