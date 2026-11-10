"""Sector flow and Dragon-Tiger parsing/cache tests (fake provider, no network)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from app.services import market_flow

DAY = date(2026, 11, 10)


class FakeProvider:
    def __init__(self, *, fail_sector: bool = False) -> None:
        self.fail_sector = fail_sector

    def get_sector_fund_flow(self):
        if self.fail_sector:
            raise RuntimeError("rate limited")
        return pd.DataFrame(
            [
                {"名称": "半导体", "今日涨跌幅": 3.2, "今日主力净流入-净额": 5.2e9, "今日主力净流入-净占比": 8.1, "主力净流入最大股": "中芯国际"},
                {"名称": "银行", "今日涨跌幅": -1.1, "今日主力净流入-净额": -3.4e9, "今日主力净流入-净占比": -6.0, "主力净流入最大股": "招商银行"},
                {"名称": "白酒", "今日涨跌幅": 1.5, "今日主力净流入-净额": 1.1e9, "今日主力净流入-净占比": 2.0, "主力净流入最大股": "贵州茅台"},
            ]
        )

    def get_sector_spot(self):
        return pd.DataFrame(
            [
                {"板块": "半导体", "涨跌幅": 3.2, "总成交额": 1e10},
                {"板块": "银行", "涨跌幅": -1.1, "总成交额": 8e9},
            ]
        )

    def get_lhb_detail(self, start, end):
        return pd.DataFrame(
            [
                {"代码": "600000", "名称": "浦发银行", "收盘价": 10.2, "涨跌幅": 5.1, "龙虎榜净买额": 2.0e8, "龙虎榜买入额": 3.0e8, "龙虎榜卖出额": 1.0e8, "上榜原因": "涨幅偏离", "换手率": 4.2},
                {"代码": "000001", "名称": "平安银行", "收盘价": 11.8, "涨跌幅": -3.0, "龙虎榜净买额": -1.5e8, "龙虎榜买入额": 1.0e8, "龙虎榜卖出额": 2.5e8, "上榜原因": "跌幅偏离", "换手率": 3.1},
            ]
        )


@pytest.fixture(autouse=True)
def temp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(market_flow, "CACHE_DIR", tmp_path)


def test_sector_flow_parses_and_sorts() -> None:
    result = market_flow.get_sector_flow(DAY, provider=FakeProvider(), refresh=True)
    assert result["available"] is True
    assert result["inflow"][0]["name"] == "半导体"
    assert result["inflow"][0]["net_inflow"] == pytest.approx(5.2e9)
    assert result["outflow"][0]["name"] == "银行"


def test_sector_flow_uses_cache() -> None:
    provider = FakeProvider()
    first = market_flow.get_sector_flow(DAY, provider=provider, refresh=True)
    assert first["available"] is True

    class Broken(FakeProvider):
        def get_sector_fund_flow(self):
            raise AssertionError("cache should prevent this call")

    second = market_flow.get_sector_flow(DAY, provider=Broken())
    assert second["inflow"][0]["name"] == "半导体"


def test_sector_flow_falls_back_to_performance() -> None:
    result = market_flow.get_sector_flow(
        DAY, provider=FakeProvider(fail_sector=True), refresh=True
    )
    assert result["available"] is False
    assert result["performance_top"][0]["name"] == "半导体"
    assert result["error"]


def test_lhb_parses_and_sorts() -> None:
    result = market_flow.get_lhb(DAY, provider=FakeProvider(), refresh=True)
    assert result["available"] is True
    assert result["net_buy"][0]["code"] == "600000"
    assert result["net_buy"][0]["net_buy"] == pytest.approx(2.0e8)
    assert result["total"] == 2
