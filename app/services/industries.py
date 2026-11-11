"""Industry classification for the whole market (Baostock, one bulk call).

AkShare's stock list leaves half of the A-share industry column empty, which
shows up as ``—`` on the cards. Baostock exposes a single ``query_stock_industry``
call for every listed stock (证监会行业分类); we normalize the long official
names into short board labels and store them on ``stock.industry``.

The call takes ~90 seconds, so it runs in the post-close job at most once a
week, never in a request.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Stock

CACHE_FILE = Path(__file__).resolve().parents[2] / "data" / "cache" / "industry_sync.json"
SYNC_DAYS = 7

# CSRC industry code -> the short label retail apps use.
SHORT_NAMES: dict[str, str] = {
    "A01": "农业",
    "A02": "林业",
    "A03": "畜牧",
    "A04": "渔业",
    "A05": "农林服务",
    "B06": "煤炭",
    "B07": "石油开采",
    "B08": "黑色金属矿",
    "B09": "有色金属矿",
    "B10": "非金属矿",
    "B11": "开采服务",
    "C13": "农副食品",
    "C14": "食品",
    "C15": "饮料",
    "C17": "纺织",
    "C18": "服装",
    "C19": "皮革",
    "C20": "木材",
    "C21": "家具",
    "C22": "造纸",
    "C23": "印刷",
    "C24": "文教用品",
    "C25": "石油加工",
    "C26": "化工",
    "C27": "医药",
    "C28": "化纤",
    "C29": "橡胶塑料",
    "C30": "建材",
    "C31": "钢铁",
    "C32": "有色金属",
    "C33": "金属制品",
    "C34": "通用设备",
    "C35": "专用设备",
    "C36": "汽车",
    "C37": "交运设备",
    "C38": "电气设备",
    "C39": "电子",
    "C40": "仪器仪表",
    "C41": "其他制造",
    "C42": "环保",
    "D44": "电力",
    "D45": "燃气",
    "D46": "水务",
    "E47": "房屋建设",
    "E48": "基础建设",
    "E50": "建筑装饰",
    "F51": "批发",
    "F52": "零售",
    "G53": "铁路公路",
    "G54": "道路运输",
    "G55": "航运港口",
    "G56": "航空机场",
    "G58": "仓储物流",
    "G59": "邮政",
    "I63": "通信服务",
    "I64": "互联网",
    "I65": "软件服务",
    "J66": "银行",
    "J67": "证券",
    "J68": "保险",
    "J69": "多元金融",
    "K70": "房地产",
    "L71": "租赁",
    "L72": "商务服务",
    "M73": "科研",
    "M74": "专业技术",
    "N77": "环保",
    "N78": "公用事业",
    "O79": "居民服务",
    "O80": "维修",
    "P82": "教育",
    "Q83": "医疗",
    "R85": "传媒",
    "R86": "广播电视",
    "R87": "文化",
    "R88": "体育",
    "R89": "娱乐",
    "S90": "综合",
}

_CODE_RE = re.compile(r"^([A-Z]\d{2})")

Fetcher = Callable[[], list[dict[str, Any]]]


def short_industry(raw: str | None) -> str | None:
    """``C32有色金属冶炼和压延加工业`` -> ``有色金属``."""
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    match = _CODE_RE.match(text)
    if match:
        code = match.group(1)
        if code in SHORT_NAMES:
            return SHORT_NAMES[code]
        remainder = text[len(code) :].strip()
        return remainder[:10] or code
    return text[:10]


def last_sync() -> date | None:
    try:
        payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return date.fromisoformat(str(payload.get("date"))[:10])
    except Exception:  # noqa: BLE001 - no cache yet
        return None


def needs_sync(days: int = SYNC_DAYS, today: date | None = None) -> bool:
    synced = last_sync()
    if synced is None:
        return True
    today = today or date.today()
    return (today - synced) >= timedelta(days=days)


def _fetch_rows() -> list[dict[str, Any]]:
    import baostock as bs

    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"baostock login failed: {login.error_msg}")
    try:
        result = bs.query_stock_industry()
        rows: list[dict[str, Any]] = []
        while result.error_code == "0" and result.next():
            _update, code, name, industry, classification = result.get_row_data()
            rows.append(
                {
                    "code": code,
                    "name": name,
                    "industry": industry,
                    "classification": classification,
                }
            )
        return rows
    finally:
        bs.logout()


def sync_industries(
    session: Session,
    *,
    fetcher: Fetcher | None = None,
    force: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    """Fetch once and write short labels onto ``stock.industry``."""
    today = today or date.today()
    if not force and not needs_sync(today=today):
        return {"skipped": True, "updated": 0, "date": (last_sync() or today).isoformat()}

    rows = (fetcher or _fetch_rows)()
    if not rows:
        raise RuntimeError("empty industry table")

    mapping: dict[str, str] = {}
    for row in rows:
        code = str(row.get("code") or "")
        if "." in code:
            code = code.split(".", 1)[1]
        label = short_industry(row.get("industry"))
        if code and label:
            mapping[code] = label

    if not mapping:
        raise RuntimeError("no usable industry rows")

    updated = 0
    stocks = session.scalars(select(Stock))
    for stock in stocks:
        label = mapping.get(stock.code)
        if label and stock.industry != label:
            stock.industry = label
            updated += 1
    session.commit()

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(
        json.dumps(
            {"date": today.isoformat(), "count": len(mapping), "updated": updated},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    logger.info("industry sync: {} labels, {} stocks updated", len(mapping), updated)
    return {"skipped": False, "updated": updated, "count": len(mapping), "date": today.isoformat()}


__all__ = ["needs_sync", "short_industry", "sync_industries"]
