"""筛选漏斗的展示层整理：把成百上千条淘汰原因压成几行。

原始 ``rejections`` 是 ``{"涨幅不足 -10.0%": 27, "涨幅不足 -1.4%": 23, ...}``，
同一个条件会因为数值不同裂成上百个 key，页面上就是一大坨。这里按「去掉尾部的
具体数值」归并，只保留条件名，例如：

    涨幅不足 -10.0% / 涨幅不足 0.3%  ->  涨幅不足
    距 60 日高点 88%                ->  距 60 日高点
    次新（上市 27 个交易日）          ->  次新
"""

from __future__ import annotations

import re

MAX_REASONS = 5

_RANGE = re.compile(r"^(.*?)\s+\d+(?:\.\d+)?%?\s*[–—-]\s*\d+(?:\.\d+)?%?$")
_PARENS = re.compile(r"^(.*?)（.*?）$")
# 只去掉「空格 + 数值」的尾巴，避免把 MA5 / RSI12 这类标识符也削掉
_TRAILING_NUMBER = re.compile(r"^(.*?)\s+[-+]?\d+(?:\.\d+)?%?$")


def reason_label(reason: str) -> str:
    """把带具体数值的淘汰原因归并成条件名。"""
    text = (reason or "").strip()
    if not text:
        return "其他"
    for pattern in (_RANGE, _PARENS, _TRAILING_NUMBER):
        match = pattern.match(text)
        if match:
            label = match.group(1).strip(" ：:，,")
            if label:
                return label
    return text


def aggregate(rejections: dict[str, int] | None, limit: int = MAX_REASONS) -> dict:
    """归并 + 排序 + 截断，返回 ``{items, others, others_count}``。"""
    merged: dict[str, int] = {}
    for reason, count in (rejections or {}).items():
        label = reason_label(str(reason))
        merged[label] = merged.get(label, 0) + int(count)

    ordered = sorted(merged.items(), key=lambda item: -item[1])
    reasons = [{"label": label, "count": count} for label, count in ordered[:limit]]
    rest = ordered[limit:]
    return {
        # 不要用 "items"：Jinja 里 dict.items 会遮住同名 key
        "reasons": reasons,
        "others": len(rest),
        "others_count": sum(count for _, count in rest),
    }


def build(stats: dict | None) -> dict | None:
    """把一次发现的统计整理成抽屉用的结构。"""
    if not stats:
        return None
    stages = stats.get("stages") or []
    if not stages:
        return None

    return {
        "strategy": stats.get("strategy") or "indicator",
        "total": stats.get("total_input"),
        "final": len(stages) and stages[-1].get("passed"),
        "elapsed_ms": round(float(stats.get("elapsed_ms") or 0), 0),
        "stages": [
            {
                "stage": stage.get("stage"),
                "name": stage.get("name"),
                "input": stage.get("input"),
                "passed": stage.get("passed"),
                "elapsed_ms": round(float(stage.get("elapsed_ms") or 0), 0),
                **aggregate(stage.get("rejections")),
            }
            for stage in stages
        ],
        "filters": stats.get("filters") or [],
    }


__all__ = ["aggregate", "build", "reason_label"]
