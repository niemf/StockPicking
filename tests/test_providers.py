"""Unit tests for code helpers and quote parsers (no network required)."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.providers.base import (
    Quote,
    exchange_for_code,
    is_main_board,
    normalize_code,
    to_prefixed_code,
)
from app.providers.sina import parse_sina_quotes
from app.providers.tencent import parse_tencent_quotes
from app.services.quotes import QuoteService

TENCENT_SAMPLE = (
    'v_sh600000="1~浦发银行~600000~10.16~10.10~10.06~292151~157099~135013~10.15~601~'
    "10.14~700~10.13~823~10.12~508~10.11~659~10.16~1~10.17~1051~10.18~4009~"
    "10.19~8395~10.20~14298~~20261110140930~0.06~0.59~10.19~10.00~"
    "10.16/292151/295623939~292151~29562~0.09~6.54~~10.19~10.00~1.88~3383.87~"
    '3383.87~0.45~11.11~9.00~";'
)

SINA_SAMPLE = (
    'var hq_str_sh600000="浦发银行,10.060,10.100,10.160,10.190,10.000,10.150,10.160,'
    "29216400,295637147.000,62900,10.150,70000,10.140,82300,10.130,50800,10.120,"
    "65900,10.110,3700,10.160,103600,10.170,400900,10.180,839540,10.190,"
    '1429800,10.200,2026-11-10,14:09:33,00,";'
)


def test_normalize_code() -> None:
    assert normalize_code("600000") == "600000"
    assert normalize_code("sh600000") == "600000"
    assert normalize_code("600000.SH") == "600000"
    assert normalize_code("1") == "000001"


def test_exchange_for_code() -> None:
    assert exchange_for_code("600000") == "sh"
    assert exchange_for_code("000001") == "sz"
    assert exchange_for_code("002594") == "sz"
    assert exchange_for_code("920001") == "bj"


def test_to_prefixed_code_keeps_exchange() -> None:
    assert to_prefixed_code("600000") == "sh600000"
    assert to_prefixed_code("000001") == "sz000001"
    # Indices must keep their explicit exchange prefix.
    assert to_prefixed_code("sh000001") == "sh000001"
    assert to_prefixed_code("sz399001") == "sz399001"


def test_main_board() -> None:
    for code in ("600000", "601318", "603855", "605001", "000001", "001390", "002594", "003001"):
        assert is_main_board(code), code
    for code in ("688981", "300059", "301001", "920001", "430001"):
        assert not is_main_board(code), code


def test_parse_tencent_quotes() -> None:
    quotes = parse_tencent_quotes(TENCENT_SAMPLE)
    assert "sh600000" in quotes
    quote = quotes["sh600000"]
    assert quote.code == "600000"
    assert quote.name == "浦发银行"
    assert quote.price == pytest.approx(10.16)
    assert quote.pre_close == pytest.approx(10.10)
    assert quote.pct_chg == pytest.approx(0.59)
    assert quote.volume == pytest.approx(292151 * 100)
    assert quote.source == "tencent"
    assert quote.ts is not None


def test_parse_sina_quotes() -> None:
    quotes = parse_sina_quotes(SINA_SAMPLE)
    assert "sh600000" in quotes
    quote = quotes["sh600000"]
    assert quote.code == "600000"
    assert quote.name == "浦发银行"
    assert quote.price == pytest.approx(10.16)
    assert quote.pre_close == pytest.approx(10.10)
    assert quote.pct_chg == pytest.approx(0.594, rel=1e-3)
    assert quote.volume == pytest.approx(29216400)
    assert quote.source == "sina"
    assert quote.ts is not None


# ------------------------------------------------------------------ batching
def _fake_quote(code: str) -> Quote:
    return Quote(
        code=code,
        name=code,
        price=10.0,
        pre_close=10.0,
        open=10.0,
        high=10.0,
        low=10.0,
        volume=1.0,
        amount=10.0,
        pct_chg=0.0,
        change=0.0,
        ts=datetime(2026, 11, 10, 10, 0),
        source="fake",
    )


class RecordingProvider:
    name = "recording"

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def get_quotes(self, codes: list[str]) -> dict[str, Quote]:
        self.batches.append(list(codes))
        return {code: _fake_quote(code) for code in codes}


def test_quote_service_splits_large_requests() -> None:
    provider = RecordingProvider()
    service = QuoteService(providers=[provider], cache_seconds=0, batch_size=2, workers=2)

    quotes = service.get_quotes(["600000", "600001", "600002", "600003", "600004"])

    assert len(quotes) == 5
    assert len(provider.batches) == 3
    assert all(len(batch) <= 2 for batch in provider.batches)
    assert sorted(code for batch in provider.batches for code in batch) == [
        "600000",
        "600001",
        "600002",
        "600003",
        "600004",
    ]


def test_quote_service_falls_back_when_primary_fails() -> None:
    class BrokenProvider:
        name = "broken"

        def get_quotes(self, codes):
            raise RuntimeError("primary down")

    fallback = RecordingProvider()
    service = QuoteService(providers=[BrokenProvider(), fallback], cache_seconds=0, batch_size=2)

    quotes = service.get_quotes(["600000", "600001", "600002"])
    assert set(quotes) == {"600000", "600001", "600002"}
    assert len(fallback.batches) == 2


def test_quote_service_cache_avoids_second_request() -> None:
    provider = RecordingProvider()
    service = QuoteService(providers=[provider], cache_seconds=60, batch_size=10)

    service.get_quotes(["600000"])
    service.get_quotes(["600000"])

    assert len(provider.batches) == 1
