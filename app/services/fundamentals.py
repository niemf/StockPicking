"""AI 基本面/题材闸门：只对**自选股**运行，做排除法。

设计取舍：

* 只在加入自选（或手动点击）时调用，每天每只股票最多一次 → 成本可控；
* 只回答「有没有硬伤 / 是不是当前可炒的题材」，**不预测涨跌、不参与打分**；
* 输出结构化 JSON，便于页面展示与后续统计；
* 无法回测（没有历史基本面），所以定位是"避雷"，不是 alpha 来源。
"""

from __future__ import annotations

import json
from datetime import date

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import DailyBar, FundamentalReview, Stock
from app.services.ai import AICallLog, AIError, DeepSeekClient
from app.services.announcements import get_announcements

PROMPT_VERSION = "fundamental-v1"

SYSTEM_PROMPT = """你是 A 股基本面风控审查员，只做「排除法」，不预测涨跌。

判断依据只有三类：
1. 近期公告里是否有硬伤：减持、解禁、预亏/业绩大幅下滑、问询函、立案调查、商誉减值、股权质押风险、退市风险；
2. 公司是否属于当前市场可炒的题材（政策、订单、产品、重组等），题材标签要具体；
3. 估值与位置是否明显异常（结合给定涨幅与成交额，不臆测财务数据）。

输出严格 JSON：
{"verdict":"pass|veto|watch","score":0-100,"tags":["题材标签"],"reasons":["支持理由"],"risks":["风险点"]}

规则：
- 有硬伤（减持/解禁/预亏/问询/立案/商誉/退市）→ verdict=veto，score<=30；
- 没有硬伤但也没题材 → watch；
- 无明显风险且有具体题材 → pass；
- 不确定就给 watch，绝不编造公告里没有的信息。"""


def build_payload(session: Session, code: str) -> dict:
    stock = session.get(Stock, code)
    if stock is None:
        raise AIError(f"股票 {code} 不存在")

    rows = list(
        session.scalars(
            select(DailyBar)
            .where(DailyBar.code == code)
            .order_by(DailyBar.trade_date.desc())
            .limit(20)
        )
    )
    rows.reverse()
    last = rows[-1] if rows else None
    ret20 = None
    if len(rows) >= 2 and rows[0].close:
        ret20 = round((rows[-1].close / rows[0].close - 1) * 100, 2)

    notices = []
    try:
        for item in get_announcements(code).get("entries", [])[:12]:
            notices.append(
                {
                    "date": item.get("date"),
                    "title": item.get("title"),
                    "type": item.get("type"),
                }
            )
    except Exception as exc:  # noqa: BLE001 - 公告拿不到也要能分析
        logger.warning("announcements unavailable for {}: {}", code, exc)

    return {
        "stock": {
            "code": code,
            "name": stock.name,
            "industry": stock.industry,
            "is_st": bool(stock.is_st),
        },
        "price": {
            "last": round(last.close, 2) if last else None,
            "ret20_pct": ret20,
            "amount_yi": round((last.amount or 0) / 1e8, 2) if last else None,
            "last_date": last.trade_date.isoformat() if last else None,
        },
        "announcements": notices,
    }


def validate(data: dict) -> tuple[dict, list[str]]:
    notes: list[str] = []
    verdict = str(data.get("verdict") or "watch").strip().lower()
    if verdict not in {"pass", "veto", "watch"}:
        notes.append(f"verdict 非法：{verdict}")
        verdict = "watch"

    try:
        score = int(round(float(data.get("score") or 0)))
    except (TypeError, ValueError):
        score = 0
        notes.append("score 非法")
    score = max(0, min(100, score))

    def _list(key: str, limit: int) -> list[str]:
        value = data.get(key) or []
        if isinstance(value, str):
            value = [value]
        return [str(item).strip()[:60] for item in value if str(item).strip()][:limit]

    return (
        {
            "verdict": verdict,
            "score": score,
            "tags": _list("tags", 6),
            "reasons": _list("reasons", 4),
            "risks": _list("risks", 4),
        },
        notes,
    )


def analyze(
    session: Session,
    code: str,
    *,
    force: bool = False,
    client: DeepSeekClient | None = None,
    trade_date: date | None = None,
) -> tuple[FundamentalReview, bool]:
    """返回 ``(评审行, 是否命中缓存)``。"""
    trade_date = trade_date or date.today()
    if not force:
        cached = session.scalar(
            select(FundamentalReview)
            .where(FundamentalReview.code == code, FundamentalReview.trade_date == trade_date)
            .limit(1)
        )
        if cached is not None:
            return cached, True

    client = client or DeepSeekClient()
    payload = build_payload(session, code)
    data, log = client.call(
        session,
        purpose="fundamental",
        prompt_version=PROMPT_VERSION,
        system=SYSTEM_PROMPT,
        user=json.dumps(payload, ensure_ascii=False),
        code=code,
        max_tokens=700,
    )
    cleaned, notes = validate(data)

    row = session.scalar(
        select(FundamentalReview)
        .where(FundamentalReview.code == code, FundamentalReview.trade_date == trade_date)
        .limit(1)
    )
    if row is None:
        row = FundamentalReview(code=code, trade_date=trade_date)
        session.add(row)

    row.name = payload["stock"]["name"]
    row.verdict = cleaned["verdict"]
    row.score = cleaned["score"]
    row.tags = cleaned["tags"]
    row.reasons = cleaned["reasons"]
    row.risks = cleaned["risks"]
    row.model = log.model
    row.raw = {"output": data, "validation_notes": notes}
    session.commit()
    return row, False


def latest_map(session: Session, codes: list[str]) -> dict[str, FundamentalReview]:
    """每只股票最近一次评审。"""
    if not codes:
        return {}
    rows = session.scalars(
        select(FundamentalReview)
        .where(FundamentalReview.code.in_(codes))
        .order_by(FundamentalReview.code, FundamentalReview.trade_date.desc())
    )
    result: dict[str, FundamentalReview] = {}
    for row in rows:
        result.setdefault(row.code, row)
    return result


__all__ = ["PROMPT_VERSION", "analyze", "build_payload", "latest_map", "validate"]
