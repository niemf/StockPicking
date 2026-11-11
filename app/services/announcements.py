"""Recent stock announcements from cninfo (巨潮资讯), cached per day.

The AkShare helper for announcements is slow (≈60 s for one stock) because it
scrapes several pages. cninfo's own JSON endpoint answers in under a second, so
the panel uses it directly and keeps the AkShare path as a CLI fallback.

Only metadata is fetched: title, date, type and the PDF link. No parsing of the
announcement body, no sentiment model — the goal is "did something happen",
not an automated reading of the filing.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import httpx
from loguru import logger

from app.providers.base import exchange_for_code, normalize_code

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"
NOTICE_TTL = 6 * 3600
PDF_BASE = "http://static.cninfo.com.cn/"

QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://www.cninfo.com.cn/",
    "X-Requested-With": "XMLHttpRequest",
}

Transport = Callable[..., httpx.Response]

CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("业绩", ("季度报告", "年度报告", "半年度报告", "业绩", "季报", "年报", "中报")),
    ("分红", ("分红", "权益分派", "利润分配", "派息")),
    ("股东", ("回购", "增持", "减持", "股东", "股权激励", "质押")),
    ("风险", ("诉讼", "仲裁", "处罚", "问询", "风险提示", "退市", "非标")),
    ("经营", ("中标", "合同", "投资", "收购", "出售", "设立", "签署")),
)

_TAG_RE = re.compile(r"<[^>]+>")


def _cache_path(code: str, day: date) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"notices_{code}_{day.isoformat()}.json"


def _read_cache(code: str, day: date, ttl: int) -> dict | None:
    """Fresh payload only; the failure path reads a long TTL and marks stale."""
    path = _cache_path(code, day)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - float(payload.get("fetched_ts", 0)) <= ttl:
            return payload
        return None
    except Exception:  # noqa: BLE001 - corrupt cache must not break the page
        return None


def _write_cache(code: str, day: date, payload: dict) -> dict:
    payload["fetched_ts"] = time.time()
    payload["fetched_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        _cache_path(code, day).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("notice cache write failed: {}", exc)
    return payload


def classify(title: str) -> str:
    for label, keywords in CATEGORY_RULES:
        if any(keyword in title for keyword in keywords):
            return label
    if title.startswith("[H股公告]") or "H股" in title:
        return "H股"
    return "公告"


def _org_id(code: str) -> str:
    prefix = "gssh0" if exchange_for_code(code) == "sh" else "gssz0"
    return f"{prefix}{code}"


def _parse(payload: dict, code: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in payload.get("announcements") or []:
        title = _TAG_RE.sub("", str(row.get("announcementTitle") or "")).strip()
        if not title:
            continue
        stamp = row.get("announcementTime")
        if isinstance(stamp, (int, float)):
            day = datetime.fromtimestamp(stamp / 1000).date().isoformat()
        else:
            day = str(row.get("announcementTime") or "")[:10]
        adjunct = str(row.get("adjunctUrl") or "")
        items.append(
            {
                "code": code,
                "date": day,
                "title": title[:120],
                "type": classify(title),
                "url": f"{PDF_BASE}{adjunct}" if adjunct else None,
            }
        )
    items.sort(key=lambda item: item["date"], reverse=True)
    return items


def get_announcements(
    code: str,
    *,
    days: int = 45,
    limit: int = 15,
    refresh: bool = False,
    transport: Transport | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Return recent announcements, falling back to the last good cache."""
    try:
        code = normalize_code(code)
    except ValueError:
        return {"code": code, "available": False, "entries": [], "error": "invalid code"}

    today = today or date.today()
    if not refresh:
        cached = _read_cache(code, today, NOTICE_TTL)
        if cached is not None:
            return cached

    start = (today - timedelta(days=days)).isoformat()
    data = {
        "pageNum": 1,
        "pageSize": max(1, min(limit * 3, 60)),
        "column": "szse",
        "tabName": "fulltext",
        "plate": "",
        "stock": f"{code},{_org_id(code)}",
        "searchkey": "",
        "secid": "",
        "category": "",
        "trade": "",
        "seDate": f"{start}~{today.isoformat()}",
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }

    try:
        post = transport or httpx.post
        response = post(QUERY_URL, data=data, headers=HEADERS, timeout=10.0)
        if response.status_code != 200:
            raise RuntimeError(f"cninfo HTTP {response.status_code}")
        items = _parse(response.json(), code)[:limit]
        payload = {
            "code": code,
            "available": True,
            "count": len(items),
            "entries": items,
            "days": days,
            "source": "cninfo",
        }
        return _write_cache(code, today, payload)
    except Exception as exc:  # noqa: BLE001 - the panel degrades to demo/empty
        logger.warning("announcements unavailable for {}: {}", code, exc)
        cached = _read_cache(code, today, 7 * 24 * 3600)
        if cached is not None and cached.get("available"):
            cached["stale"] = True
            return cached
        return {"code": code, "available": False, "entries": [], "error": str(exc)[:200]}


__all__ = ["classify", "get_announcements"]
