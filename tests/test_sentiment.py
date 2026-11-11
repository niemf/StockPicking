"""Market breadth aggregation and cache behaviour (no network)."""

from __future__ import annotations

import pytest

from app.providers.base import Quote
from app.services import sentiment


def make_quote(code: str, price: float, pre_close: float, amount: float = 1e8) -> Quote:
    pct = (price / pre_close - 1) * 100 if pre_close else 0.0
    return Quote(
        code=code,
        name=code,
        price=price,
        pre_close=pre_close,
        open=pre_close,
        high=max(price, pre_close),
        low=min(price, pre_close),
        volume=1e6,
        amount=amount,
        pct_chg=round(pct, 2),
        change=round(price - pre_close, 2),
    )


def test_compute_breadth_and_limits() -> None:
    quotes = {
        "600000": make_quote("600000", 11.0, 10.0),   # +10% limit up
        "000001": make_quote("000001", 10.2, 10.0),   # +2%
        "600001": make_quote("600001", 9.9, 10.0),    # -1%
        "000002": make_quote("000002", 10.0, 10.0),   # flat
    }
    result = sentiment.compute(quotes)

    assert result["available"] is True
    assert result["total"] == 4
    assert (result["up"], result["down"], result["flat"]) == (2, 1, 1)
    assert result["limit_up"] == 1
    assert result["limit_down"] == 0
    assert result["strong"] == 1
    assert result["breadth"] == pytest.approx(50.0)
    assert result["label"] == "分化"
    assert result["amount_yi"] == pytest.approx(4.0)


def test_compute_prefers_provider_limit_prices() -> None:
    quote = make_quote("600000", 9.55, 10.0)
    quote.limit_down = 9.55
    result = sentiment.compute({"600000": quote})
    assert result["limit_down"] == 1
    assert result["label"] == "普跌"


def test_compute_ignores_bad_quotes() -> None:
    bad = make_quote("600000", 0.0, 0.0)
    result = sentiment.compute({"600000": bad})
    assert result["available"] is False
    assert result["reason"] == "no_quotes"


def test_market_sentiment_cache_and_refresh(monkeypatch) -> None:
    sentiment.reset_cache()
    calls: list[list[str]] = []

    class FakeService:
        def get_quotes(self, codes):
            calls.append(list(codes))
            return {"600000": make_quote("600000", 10.5, 10.0, amount=2e8)}

    monkeypatch.setattr(sentiment, "_market_codes", lambda session: ["600000"])

    first = sentiment.market_sentiment(None, quote_service=FakeService())
    second = sentiment.market_sentiment(None, quote_service=FakeService())
    assert first["available"] and second["total"] == 1
    assert len(calls) == 1

    sentiment.market_sentiment(None, refresh=True, quote_service=FakeService())
    assert len(calls) == 2
    sentiment.reset_cache()
