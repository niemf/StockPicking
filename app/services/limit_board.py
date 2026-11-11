"""Limit-up ladder (涨停梯队) and Dragon-Tiger seats (营业部).

Both are daily artefacts with their own caches:

* ``stock_zt_pool_em`` returns one row per limit-up stock with ``连板数``,
  ``封板资金``, ``炸板次数`` and ``所属行业`` — enough to build a ladder without
  recomputing streaks from daily bars.
* ``stock_lhb_hyyyb_em`` returns active seats (营业部) over a date range; we keep
  the latest listed day and rank by net buy/sell.

The endpoints live on Eastmoney's push2ex/datacenter hosts, which are separate
from the heavily limited push2 quote host. First call takes ~10 s, so results
are cached per day and served from disk afterwards.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

from app.providers.akshare_provider import AkShareProvider

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"
POOL_TTL_INTRADAY = 600
POOL_TTL_CLOSED = 6 * 3600
SEAT_TTL = 6 * 3600

BUCKETS: tuple[tuple[int, str], ...] = (
    (5, "5 板及以上"),
    (4, "4 板"),
    (3, "3 板"),
    (2, "2 板"),
    (1, "首板"),
)


def _yi(value: Any, digits: int = 2) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN from pandas
        return None
    return round(number / 1e8, digits)


def _cache_path(kind: str, day: date) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{kind}_{day.isoformat()}.json"


def _read_cache(kind: str, day: date, ttl: int) -> dict | None:
    path = _cache_path(kind, day)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - float(payload.get("fetched_ts", 0)) <= ttl:
            return payload
        payload["stale"] = True
        return payload
    except Exception:  # noqa: BLE001
        return None


def _write_cache(kind: str, day: date, payload: dict) -> dict:
    payload["fetched_ts"] = time.time()
    payload["fetched_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        _cache_path(kind, day).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("ladder cache write failed: {}", exc)
    return payload


def _clock(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) == 6 and text.isdigit():
        return f"{text[:2]}:{text[2:4]}:{text[4:]}"
    return text or "—"


def _num(value: Any, digits: int = 2) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN from pandas
        return None
    return round(number, digits)


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def parse_pool(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Normalize the limit-up pool; unknown columns are skipped, never fatal."""
    if df is None or df.empty:
        return []
    if "代码" not in df.columns or "名称" not in df.columns:
        raise RuntimeError(f"unexpected limit pool columns: {list(df.columns)}")

    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        try:
            code = str(row["代码"]).zfill(6)
            name = str(row["名称"]).strip()
            if not name:
                continue
            streak = _int(row.get("连板数")) or 1
            rows.append(
                {
                    "code": code,
                    "name": name,
                    "pct_chg": _num(row.get("涨跌幅")) or 0.0,
                    "price": _num(row.get("最新价")) or 0.0,
                    "amount_yi": _yi(row.get("成交额")),
                    "turnover": _num(row.get("换手率")) or 0.0,
                    "seal_yi": _yi(row.get("封板资金")),
                    "first_seal": _clock(row.get("首次封板时间")),
                    "last_seal": _clock(row.get("最后封板时间")),
                    "breaks": _int(row.get("炸板次数")),
                    "stat": str(row.get("涨停统计") or "").strip(),
                    "streak": max(1, streak),
                    "industry": str(row.get("所属行业") or "").strip() or "—",
                }
            )
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda item: (-item["streak"], -(item["seal_yi"] or 0)))
    return rows


def _ladder(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for streak, label in BUCKETS:
        members = [item for item in rows if item["streak"] == streak] if streak < 5 else [
            item for item in rows if item["streak"] >= 5
        ]
        if members:
            groups.append({"streak": streak, "label": label, "count": len(members), "stocks": members})
    return groups


def get_limit_pool(
    day: date | None = None,
    *,
    provider: AkShareProvider | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Return the limit-up pool and its ladder for one trading day."""
    day = day or date.today()
    ttl = POOL_TTL_INTRADAY if day == date.today() else POOL_TTL_CLOSED
    if not refresh:
        cached = _read_cache("limit_pool", day, ttl)
        if cached is not None:
            return cached

    provider = provider or AkShareProvider()
    try:
        rows = parse_pool(provider.get_limit_pool(day))
        if not rows:
            raise RuntimeError("empty limit pool")
        payload = {
            "date": day.isoformat(),
            "available": True,
            "total": len(rows),
            "max_streak": max(item["streak"] for item in rows),
            "first_boards": sum(1 for item in rows if item["streak"] == 1),
            "ladder": _ladder(rows),
            "stocks": rows,
            "source": "eastmoney",
        }
        return _write_cache("limit_pool", day, payload)
    except Exception as exc:  # noqa: BLE001 - page must degrade, not fail
        logger.warning("limit pool unavailable for {}: {}", day, exc)
        cached = _read_cache("limit_pool", day, 7 * 24 * 3600)
        if cached is not None and cached.get("available"):
            cached["stale"] = True
            return cached
        return {"date": day.isoformat(), "available": False, "error": str(exc)[:200], "stocks": []}


def parse_seats(df: pd.DataFrame, day: date) -> tuple[date | None, list[dict[str, Any]]]:
    if df is None or df.empty:
        return None, []
    if "营业部名称" not in df.columns or "上榜日" not in df.columns:
        raise RuntimeError(f"unexpected seat columns: {list(df.columns)}")

    frame = df.copy()
    frame["_day"] = frame["上榜日"].astype(str).str[:10]
    latest = max(frame["_day"]) if len(frame) else None
    if latest is None:
        return None, []
    frame = frame[frame["_day"] == latest]

    rows: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        try:
            net = float(row.get("总买卖净额") or 0)
            stocks = str(row.get("买入股票") or "").split()
            rows.append(
                {
                    "seat": str(row["营业部名称"]).strip(),
                    "date": latest,
                    "buy_count": _int(row.get("买入个股数")),
                    "sell_count": _int(row.get("卖出个股数")),
                    "buy_yi": _yi(row.get("买入总金额")),
                    "sell_yi": _yi(row.get("卖出总金额")),
                    "net_yi": _yi(net),
                    "stocks": stocks[:8],
                }
            )
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda item: item["net_yi"] or 0, reverse=True)
    return date.fromisoformat(latest), rows


def get_lhb_seats(
    day: date | None = None,
    *,
    provider: AkShareProvider | None = None,
    refresh: bool = False,
    limit: int = 10,
) -> dict[str, Any]:
    """Top buying/selling seats on the latest Dragon-Tiger day up to ``day``."""
    day = day or date.today()
    if not refresh:
        cached = _read_cache("lhb_seats", day, SEAT_TTL)
        if cached is not None:
            return cached

    provider = provider or AkShareProvider()
    try:
        frame = provider.get_lhb_seats(day - timedelta(days=7), day)
        latest, rows = parse_seats(frame, day)
        if latest is None or not rows:
            raise RuntimeError("empty seat table")
        payload = {
            "date": latest.isoformat(),
            "available": True,
            "count": len(rows),
            "buy": rows[:limit],
            "sell": list(reversed(rows[-limit:])),
            "source": "eastmoney",
        }
        return _write_cache("lhb_seats", day, payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("LHB seats unavailable for {}: {}", day, exc)
        cached = _read_cache("lhb_seats", day, 7 * 24 * 3600)
        if cached is not None and cached.get("available"):
            cached["stale"] = True
            return cached
        return {"date": day.isoformat(), "available": False, "error": str(exc)[:200], "buy": [], "sell": []}


__all__ = ["get_lhb_seats", "get_limit_pool", "parse_pool", "parse_seats"]
