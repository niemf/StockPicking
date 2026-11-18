"""筛选漏斗的展示整理：原因归并、截断与抽屉结构。"""

from __future__ import annotations

from app.web.funnel import aggregate, build, reason_label


def test_reason_label_strips_numeric_tails() -> None:
    assert reason_label("涨幅不足 -10.0%") == "涨幅不足"
    assert reason_label("涨幅不足 0.3%") == "涨幅不足"
    assert reason_label("距 60 日高点 88%") == "距 60 日高点"
    assert reason_label("量比不足 1.3") == "量比不足"
    assert reason_label("止损过宽 10.7%") == "止损过宽"
    assert reason_label("盈亏比不足 1.1") == "盈亏比不足"


def test_reason_label_handles_parentheses_and_ranges() -> None:
    assert reason_label("次新（上市 27 个交易日）") == "次新"
    assert reason_label("距 20 日新高 90%–95%") == "距 20 日新高"


def test_reason_label_keeps_plain_text() -> None:
    assert reason_label("MACD 走弱") == "MACD 走弱"
    assert reason_label("MA20 未拐头") == "MA20 未拐头"
    assert reason_label("日线不足") == "日线不足"


def test_aggregate_merges_and_truncates() -> None:
    result = aggregate(
        {
            "涨幅不足 1.0%": 3,
            "涨幅不足 2.0%": 2,
            "距 60 日高点 88%": 5,
            "量比不足 1.1": 4,
            "不是首次上穿 MA5": 6,
        },
        limit=2,
    )

    assert [item["label"] for item in result["reasons"]] == ["不是首次上穿 MA5", "涨幅不足"]
    assert result["reasons"][0]["count"] == 6
    assert result["reasons"][1]["count"] == 5
    assert result["others"] == 2
    assert result["others_count"] == 5 + 4


def test_aggregate_handles_empty() -> None:
    assert aggregate(None) == {"reasons": [], "others": 0, "others_count": 0}


def test_build_summarises_a_job() -> None:
    stats = {
        "strategy": "indicator",
        "total_input": 3195,
        "elapsed_ms": 2000.4,
        "filters": [{"stage": 1, "key": "universe_basic", "label": "基础池"}],
        "stages": [
            {
                "stage": 1,
                "name": "基础池",
                "input": 3195,
                "passed": 3047,
                "elapsed_ms": 8.2,
                "rejections": {"ST": 142, "退市风险": 1},
            },
            {
                "stage": 2,
                "name": "价格与流动性",
                "input": 3047,
                "passed": 1214,
                "elapsed_ms": 11.9,
                "rejections": {"成交额不足": 1331},
            },
        ],
    }

    built = build(stats)

    assert built is not None
    assert built["strategy"] == "indicator"
    assert built["total"] == 3195
    assert built["final"] == 1214
    assert built["elapsed_ms"] == 2000
    assert built["stages"][0]["reasons"][0] == {"label": "ST", "count": 142}
    assert built["filters"][0]["label"] == "基础池"


def test_build_returns_none_without_stages() -> None:
    assert build(None) is None
    assert build({}) is None
    assert build({"stages": []}) is None
