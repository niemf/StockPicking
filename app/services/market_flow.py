"""Sector capital flow and Dragon-Tiger list with daily cache and fallbacks.

Eastmoney's push2 endpoints are rate-limited aggressively, so every call is
cached per trading day. When the flow endpoint is unavailable we fall back to
Sina sector performance and clearly mark that capital-flow data is missing.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from loguru import logger

from app.providers.akshare_provider import AkShareProvider

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"
SECTOR_TTL = 1800
LHB_TTL = 3600


def _pick(df: pd.DataFrame, *candidates: str, contains: tuple[str, ...] = ()) -> str | None:
    for name in candidates:
        if name in df.columns:
            return name
    for column in df.columns:
        text = str(column)
        if all(part in text for part in contains):
            return column
    return None


def _cache_path(kind: str, day: date) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{kind}_{day.isoformat()}.json"


def _read_cache(kind: str, day: date, ttl: int) -> dict | None:
    path = _cache_path(kind, day)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        age = time.time() - float(payload.get("fetched_ts", 0))
        if age <= ttl:
            return payload
        payload["stale"] = True
        return payload
    except Exception:  # noqa: BLE001 - a corrupt cache must not break the review
        return None


def _write_cache(kind: str, day: date, payload: dict) -> dict:
    payload["fetched_ts"] = time.time()
    payload["fetched_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        _cache_path(kind, day).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache write failed: {}", exc)
    return payload


def _parse_sector_flow(df: pd.DataFrame) -> list[dict]:
    name_col = _pick(df, "名称", contains=("板块", "名称"))
    pct_col = _pick(df, contains=("涨跌幅",))
    flow_col = _pick(df, contains=("主力净流入", "净额"))
    ratio_col = _pick(df, contains=("主力净流入", "净占比"))
    leader_col = _pick(df, contains=("主力净流入最大股",))
    if name_col is None or flow_col is None:
        raise RuntimeError(f"unexpected sector flow columns: {list(df.columns)}")

    rows: list[dict] = []
    for _, row in df.iterrows():
        try:
            rows.append(
                {
                    "name": str(row[name_col]).strip(),
                    "pct_chg": float(row[pct_col]) if pct_col else None,
                    "net_inflow": float(row[flow_col]),
                    "net_ratio": float(row[ratio_col]) if ratio_col else None,
                    "leader": str(row[leader_col]).strip() if leader_col else None,
                }
            )
        except (TypeError, ValueError):
            continue
    return rows


def _sina_performance(provider: AkShareProvider) -> list[dict]:
    try:
        df = provider.get_sector_spot()
    except Exception as exc:  # noqa: BLE001
        logger.warning("sina sector fallback failed: {}", exc)
        return []
    name_col = _pick(df, "板块", contains=("板块",))
    pct_col = _pick(df, contains=("涨跌幅",))
    amount_col = _pick(df, contains=("总成交额",))
    if name_col is None or pct_col is None:
        return []
    rows = []
    for _, row in df.iterrows():
        try:
            rows.append(
                {
                    "name": str(row[name_col]).strip(),
                    "pct_chg": float(row[pct_col]),
                    "amount": float(row[amount_col]) if amount_col else None,
                }
            )
        except (TypeError, ValueError):
            continue
    return rows


def get_sector_flow(
    day: date | None = None,
    *,
    provider: AkShareProvider | None = None,
    refresh: bool = False,
) -> dict:
    day = day or date.today()
    if not refresh:
        cached = _read_cache("sector_flow", day, SECTOR_TTL)
        if cached is not None:
            return cached

    provider = provider or AkShareProvider()
    try:
        df = provider.get_sector_fund_flow()
        rows = _parse_sector_flow(df)
        if not rows:
            raise RuntimeError("empty sector flow")
        ordered = sorted(rows, key=lambda item: item["net_inflow"], reverse=True)
        payload = {
            "date": day.isoformat(),
            "available": True,
            "inflow": ordered[:6],
            "outflow": list(reversed(ordered[-6:])),
            "source": "eastmoney",
        }
        return _write_cache("sector_flow", day, payload)
    except Exception as exc:  # noqa: BLE001 - fall back, never break the report
        logger.warning("sector fund flow unavailable: {}", exc)
        cached = _read_cache("sector_flow", day, 7 * 24 * 3600)
        if cached is not None and cached.get("available"):
            cached["stale"] = True
            return cached

        performance = _sina_performance(provider)
        ordered = sorted(performance, key=lambda item: item["pct_chg"], reverse=True)
        payload = {
            "date": day.isoformat(),
            "available": False,
            "error": str(exc)[:300],
            "inflow": [],
            "outflow": [],
            "performance_top": ordered[:6],
            "performance_bottom": list(reversed(ordered[-6:])),
            "source": "sina",
        }
        return _write_cache("sector_flow", day, payload)


def _parse_lhb(df: pd.DataFrame) -> list[dict]:
    code_col = _pick(df, "代码", "股票代码")
    name_col = _pick(df, "名称", "股票名称")
    close_col = _pick(df, "收盘价")
    pct_col = _pick(df, contains=("涨跌幅",))
    net_col = _pick(df, contains=("龙虎榜净买额",))
    buy_col = _pick(df, contains=("龙虎榜买入额",))
    sell_col = _pick(df, contains=("龙虎榜卖出额",))
    reason_col = _pick(df, "上榜原因")
    note_col = _pick(df, "解读")
    turnover_col = _pick(df, "换手率")
    if code_col is None or name_col is None or net_col is None:
        raise RuntimeError(f"unexpected LHB columns: {list(df.columns)}")

    rows = []
    for _, row in df.iterrows():
        try:
            rows.append(
                {
                    "code": str(row[code_col]).zfill(6),
                    "name": str(row[name_col]).strip(),
                    "close": float(row[close_col]) if close_col else None,
                    "pct_chg": float(row[pct_col]) if pct_col else None,
                    "net_buy": float(row[net_col]),
                    "buy": float(row[buy_col]) if buy_col else None,
                    "sell": float(row[sell_col]) if sell_col else None,
                    "reason": str(row[reason_col]).strip() if reason_col else None,
                    "interpretation": str(row[note_col]).strip() if note_col else None,
                    "turnover_rate": float(row[turnover_col]) if turnover_col else None,
                }
            )
        except (TypeError, ValueError):
            continue
    return rows


def get_lhb(
    day: date | None = None,
    *,
    provider: AkShareProvider | None = None,
    refresh: bool = False,
    limit: int = 8,
) -> dict:
    day = day or date.today()
    if not refresh:
        cached = _read_cache("lhb", day, LHB_TTL)
        if cached is not None:
            return cached

    provider = provider or AkShareProvider()
    try:
        df = provider.get_lhb_detail(day, day)
        rows = _parse_lhb(df)
        ordered = sorted(rows, key=lambda item: item["net_buy"], reverse=True)
        payload = {
            "date": day.isoformat(),
            "available": bool(rows),
            "net_buy": ordered[:limit],
            "net_sell": list(reversed(ordered[-limit:])),
            "total": len(rows),
            "source": "eastmoney",
        }
        return _write_cache("lhb", day, payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("LHB unavailable: {}", exc)
        cached = _read_cache("lhb", day, 7 * 24 * 3600)
        if cached is not None:
            cached["stale"] = True
            return cached
        return {"date": day.isoformat(), "available": False, "error": str(exc)[:300]}


__all__ = ["get_lhb", "get_sector_flow"]
