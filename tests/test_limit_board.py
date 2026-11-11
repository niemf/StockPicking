"""Limit-up ladder and Dragon-Tiger seat parsing/caching (fake provider, no network)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from app.services import limit_board

DAY = date(2026, 11, 11)


def pool_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"代码": "002310", "名称": "东方新能", "涨跌幅": 10.19, "最新价": 2.81, "成交额": 1.68e8, "换手率": 1.36, "封板资金": 3.23e8, "首次封板时间": "092500", "最后封板时间": "092500", "炸板次数": 0, "涨停统计": "3/3", "连板数": 3, "所属行业": "电力"},
            {"代码": "603366", "名称": "日出东方", "涨跌幅": 10.01, "最新价": 10.88, "成交额": 6.31e7, "换手率": 0.71, "封板资金": 7.8e8, "首次封板时间": "092501", "最后封板时间": "092501", "炸板次数": 0, "涨停统计": "3/3", "连板数": 3, "所属行业": "厨卫电器"},
            {"代码": "002623", "名称": "亚玛顿", "涨跌幅": 10.01, "最新价": 21.21, "成交额": 2.82e7, "换手率": 0.69, "封板资金": 2.36e8, "首次封板时间": "092500", "最后封板时间": "092500", "炸板次数": 1, "涨停统计": "6/3", "连板数": 2, "所属行业": "光伏设备"},
            {"代码": "600000", "名称": "浦发银行", "涨跌幅": 10.0, "最新价": 10.2, "成交额": 9.9e8, "换手率": 3.1, "封板资金": 1.5e8, "首次封板时间": "133000", "最后封板时间": "143000", "炸板次数": 2, "涨停统计": "1/1", "连板数": 1, "所属行业": "银行"},
        ]
    )


def seat_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"营业部名称": "沪股通专用", "上榜日": "2026-11-10", "买入个股数": 10, "卖出个股数": 10, "买入总金额": 5.3e9, "卖出总金额": 3.7e9, "总买卖净额": 1.6e9, "买入股票": "金健米业 生益科技"},
            {"营业部名称": "中信上海分公司", "上榜日": "2026-11-10", "买入个股数": 7, "卖出个股数": 6, "买入总金额": 8.7e8, "卖出总金额": 2.5e8, "总买卖净额": -6.2e8, "买入股票": "金健米业 大东方"},
            {"营业部名称": "旧数据营业部", "上榜日": "2026-11-09", "买入个股数": 1, "卖出个股数": 1, "买入总金额": 1e8, "卖出总金额": 1e8, "总买卖净额": 0, "买入股票": "某某"},
        ]
    )


class FakeProvider:
    def __init__(self, *, empty=False) -> None:
        self.empty = empty

    def get_limit_pool(self, day):
        return pd.DataFrame() if self.empty else pool_frame()

    def get_lhb_seats(self, start, end):
        return pd.DataFrame() if self.empty else seat_frame()


@pytest.fixture(autouse=True)
def temp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(limit_board, "CACHE_DIR", tmp_path)


def test_parse_pool_and_ladder() -> None:
    result = limit_board.get_limit_pool(DAY, provider=FakeProvider(), refresh=True)

    assert result["available"] is True
    assert result["total"] == 4
    assert result["max_streak"] == 3
    assert result["first_boards"] == 1
    assert [group["streak"] for group in result["ladder"]] == [3, 2, 1]
    assert [item["name"] for item in result["ladder"][0]["stocks"]] == ["日出东方", "东方新能"]
    assert result["stocks"][0]["first_seal"] == "09:25:01"
    assert result["stocks"][0]["seal_yi"] == pytest.approx(7.8)
    assert result["stocks"][-1]["breaks"] == 2


def test_limit_pool_uses_cache_and_degrades() -> None:
    first = limit_board.get_limit_pool(DAY, provider=FakeProvider(), refresh=True)
    assert first["available"] is True

    class Broken(FakeProvider):
        def get_limit_pool(self, day):
            raise AssertionError("cache should prevent this call")

    cached = limit_board.get_limit_pool(DAY, provider=Broken())
    assert cached["total"] == 4

    empty = limit_board.get_limit_pool(date(2026, 11, 12), provider=FakeProvider(empty=True), refresh=True)
    assert empty["available"] is False
    assert empty["error"]


def test_parse_seats_uses_latest_day_and_sorts() -> None:
    latest, rows = limit_board.parse_seats(seat_frame(), DAY)

    assert latest == date(2026, 11, 10)
    assert [row["seat"] for row in rows] == ["沪股通专用", "中信上海分公司"]
    assert rows[0]["net_yi"] == pytest.approx(16.0)
    assert rows[0]["stocks"] == ["金健米业", "生益科技"]
    assert rows[1]["net_yi"] == pytest.approx(-6.2)


def test_get_lhb_seats_payload_and_cache() -> None:
    result = limit_board.get_lhb_seats(DAY, provider=FakeProvider(), refresh=True, limit=1)

    assert result["available"] is True
    assert result["date"] == "2026-11-10"
    assert result["count"] == 2
    assert [row["seat"] for row in result["buy"]] == ["沪股通专用"]
    assert [row["seat"] for row in result["sell"]] == ["中信上海分公司"]

    class Broken(FakeProvider):
        def get_lhb_seats(self, start, end):
            raise AssertionError("cache should prevent this call")

    assert limit_board.get_lhb_seats(DAY, provider=Broken(), limit=1)["count"] == 2


def test_ladder_handles_six_plus_boards() -> None:
    frame = pool_frame()
    extra = frame.iloc[[0]].copy()
    extra["代码"] = "600001"
    extra["名称"] = "六板样本"
    extra["连板数"] = 6
    extra["封板资金"] = 1.0e9
    rows = limit_board.parse_pool(pd.concat([frame, extra], ignore_index=True))

    ladder = limit_board._ladder(rows)
    labels = [group["label"] for group in ladder]
    assert labels[0] == "6 板"
    assert labels[-1] == "首板"
    assert ladder[0]["count"] == 1


def test_expired_cache_is_refetched() -> None:
    import json

    limit_board.get_limit_pool(DAY, provider=FakeProvider(), refresh=True)
    path = limit_board._cache_path("limit_pool", DAY)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["fetched_ts"] = 0
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    class OneRow(FakeProvider):
        def get_limit_pool(self, day):
            return pool_frame().head(1)

    refreshed = limit_board.get_limit_pool(DAY, provider=OneRow())
    assert refreshed["total"] == 1
    assert not refreshed.get("stale")


def test_nan_values_do_not_break_json() -> None:
    import json

    frame = pool_frame()
    frame.loc[0, "封板资金"] = float("nan")
    frame.loc[0, "涨跌幅"] = float("nan")
    rows = limit_board.parse_pool(frame)
    json.dumps(rows, allow_nan=False)  # must not raise
    target = next(item for item in rows if item["code"] == "002310")
    assert target["seal_yi"] is None
    assert target["pct_chg"] == 0.0

    seats = seat_frame()
    seats.loc[0, "总买卖净额"] = float("nan")
    _, seat_rows = limit_board.parse_seats(seats, DAY)
    json.dumps(seat_rows, allow_nan=False)
    assert any(row["net_yi"] is None for row in seat_rows)
