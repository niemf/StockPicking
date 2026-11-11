"""Sector flow and Dragon-Tiger parsing/cache tests (fake provider, no network)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from app.services import market_flow

DAY = date(2026, 11, 10)


class FakeProvider:
    def __init__(self, *, fail_sector: bool = False, fail_stock: bool = False) -> None:
        self.fail_sector = fail_sector
        self.fail_stock = fail_stock

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

    def get_stock_fund_flow(self, code, days=60):
        if self.fail_stock:
            raise RuntimeError("blocked")
        return pd.DataFrame(
            [
                {"日期": "2026-11-06", "收盘价": 42.88, "涨跌幅": 0.8, "主力净流入-净额": -2.0e8, "主力净流入-净占比": -5.06, "超大单净流入-净额": -4.7e8, "大单净流入-净额": 2.7e8, "中单净流入-净额": 2.2e8, "小单净流入-净额": -0.3e8},
                {"日期": "2026-11-09", "收盘价": 43.01, "涨跌幅": 0.3, "主力净流入-净额": -1.5e8, "主力净流入-净占比": -5.17, "超大单净流入-净额": -6.1e8, "大单净流入-净额": 4.6e8, "中单净流入-净额": 1.1e8, "小单净流入-净额": 0.4e8},
                {"日期": "2026-11-10", "收盘价": 43.01, "涨跌幅": 0.0, "主力净流入-净额": 1.1e8, "主力净流入-净占比": 6.31, "超大单净流入-净额": 0.6e8, "大单净流入-净额": 0.5e8, "中单净流入-净额": -0.9e8, "小单净流入-净额": -0.2e8},
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
    assert [row["name"] for row in result["rows"]] == ["半导体", "白酒", "银行"]
    assert result["rows"][0]["rank"] == 1
    assert result["rows"][0]["net_inflow_yi"] == pytest.approx(52.0)
    assert (result["up"], result["down"], result["count"]) == (2, 1, 3)


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


def test_stock_flow_parses_latest_and_sum() -> None:
    result = market_flow.get_stock_fund_flow("600036", DAY, provider=FakeProvider(), refresh=True)

    assert result["available"] is True
    assert result["date"] == "2026-11-10"
    assert result["latest"]["main_yi"] == pytest.approx(1.1)
    assert result["latest"]["main_ratio"] == pytest.approx(6.31)
    assert result["latest"]["super_yi"] == pytest.approx(0.6)
    # (-2.0 - 1.5 + 1.1) 亿
    assert result["main_5d_yi"] == pytest.approx(-2.4)
    assert len(result["history"]) == 3


def test_stock_flow_uses_cache_and_stale_fallback(monkeypatch) -> None:
    first = market_flow.get_stock_fund_flow("600036", DAY, provider=FakeProvider(), refresh=True)
    assert first["available"] is True

    class Broken(FakeProvider):
        def get_stock_fund_flow(self, code, days=60):
            raise AssertionError("cache should prevent this call")

    cached = market_flow.get_stock_fund_flow("600036", DAY, provider=Broken())
    assert cached["latest"]["main_yi"] == pytest.approx(1.1)

    # After the TTL the stale cache is still better than an empty panel.
    monkeypatch.setattr(
        market_flow, "_sina_stock_flow", lambda code: (_ for _ in ()).throw(RuntimeError("down"))
    )
    missing = market_flow.get_stock_fund_flow(
        "600036", DAY, provider=FakeProvider(fail_stock=True), refresh=True
    )
    assert missing["available"] is True
    assert missing["stale"] is True

    empty = market_flow.get_stock_fund_flow(
        "601988", DAY, provider=FakeProvider(fail_stock=True), refresh=True
    )
    assert empty["available"] is False
    assert empty["error"]


def test_stock_flow_falls_back_to_sina(monkeypatch) -> None:
    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def fake_get(url, **kwargs):
        if "ssi_ssfx" in url:
            return FakeResponse(
                {
                    "r0_in": "249354478.1500",
                    "r0_out": "385317755.8800",
                    "r1_in": "154518220.7200",
                    "r1_out": "165707451.3100",
                    "r2_in": "48905591.0000",
                    "r2_out": "51080295.0000",
                    "r3_in": "4960482.7800",
                    "r3_out": "4757219.0000",
                    "trade": "42.9800",
                    "changeratio": "-0.000697512",
                }
            )
        return FakeResponse(
            [
                {"opendate": "2026-11-10", "trade": "43.0100", "changeratio": "0", "netamount": "-8782329.4600"},
                {"opendate": "2026-11-09", "trade": "43.0100", "changeratio": "0.00303172", "netamount": "158890280.9900"},
            ]
        )

    monkeypatch.setattr(market_flow.httpx, "get", fake_get)
    result = market_flow.get_stock_fund_flow(
        "600036", DAY, provider=FakeProvider(fail_stock=True), refresh=True
    )

    assert result["available"] is True
    assert result["source"] == "sina"
    assert result["metric_label"] == "全单净流入"
    # main = (r0_in - r0_out) + (r1_in - r1_out), in 亿元
    assert result["latest"]["main_yi"] == pytest.approx(-1.47)
    assert result["latest"]["super_yi"] == pytest.approx(-1.36)
    assert result["latest"]["big_yi"] == pytest.approx(-0.11)
    assert result["latest"]["pct_chg"] == pytest.approx(-0.07)
    assert [item["date"] for item in result["history"]] == ["2026-11-09", "2026-11-10"]
    assert result["main_5d_yi"] == pytest.approx(1.5)
