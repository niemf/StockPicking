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

import httpx
import pandas as pd
from loguru import logger

from app.providers.akshare_provider import AkShareProvider
from app.providers.base import exchange_for_code, normalize_code

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"
SECTOR_TTL = 1800
LHB_TTL = 3600
STOCK_FLOW_TTL = 3 * 3600


def _yi(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(float(value) / 1e8, digits)


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
    else:
        # A failed refresh is cached for a while too: hammering Eastmoney only
        # extends the IP-level rate limit.
        cached = _read_cache("sector_flow", day, 7 * 24 * 3600)
        if (
            cached is not None
            and not cached.get("available")
            and time.time() - float(cached.get("fetched_ts", 0)) < 600
        ):
            cached["refresh_skipped"] = True
            return cached

    provider = provider or AkShareProvider()
    try:
        df = provider.get_sector_fund_flow()
        rows = _parse_sector_flow(df)
        if not rows:
            raise RuntimeError("empty sector flow")
        ordered = sorted(rows, key=lambda item: item["net_inflow"], reverse=True)
        for rank, item in enumerate(ordered, 1):
            item["rank"] = rank
            item["net_inflow_yi"] = _yi(item["net_inflow"])
        up = sum(1 for item in ordered if (item.get("pct_chg") or 0) > 0)
        payload = {
            "date": day.isoformat(),
            "available": True,
            "rows": ordered,
            "count": len(ordered),
            "up": up,
            "down": len(ordered) - up,
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


SINA_DAY_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "MoneyFlow.ssi_ssfx_flzjtj?daima={prefixed}"
)
SINA_HIST_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "MoneyFlow.ssl_qsfx_zjlrqs?page=1&num=10&sort=opendate&asc=0&daima={prefixed}"
)
SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}


def _sina_stock_flow(code: str) -> dict:
    """Sina money-flow fallback: today's breakdown + 10-day net history."""
    prefixed = f"{exchange_for_code(code)}{code}"
    day_response = httpx.get(
        SINA_DAY_URL.format(prefixed=prefixed), timeout=8.0, headers=SINA_HEADERS
    )
    hist_response = httpx.get(
        SINA_HIST_URL.format(prefixed=prefixed), timeout=8.0, headers=SINA_HEADERS
    )
    day = day_response.json()
    history_raw = hist_response.json()
    if not isinstance(day, dict) or not day or not isinstance(history_raw, list) or not history_raw:
        raise RuntimeError("unexpected sina money-flow payload")

    def net(key_in: str, key_out: str) -> float:
        return float(day.get(key_in) or 0) - float(day.get(key_out) or 0)

    super_net = net("r0_in", "r0_out")
    big_net = net("r1_in", "r1_out")
    mid_net = net("r2_in", "r2_out")
    small_net = net("r3_in", "r3_out")
    main = super_net + big_net

    history: list[dict] = []
    for row in history_raw:
        try:
            amount = float(row.get("netamount") or 0)
            history.append(
                {
                    "date": str(row.get("opendate"))[:10],
                    "close": float(row.get("trade") or 0) or None,
                    "pct_chg": round(float(row.get("changeratio") or 0) * 100, 2),
                    "main": amount,
                    "main_yi": _yi(amount),
                }
            )
        except (TypeError, ValueError):
            continue
    history.sort(key=lambda item: item["date"])
    if not history:
        raise RuntimeError("empty sina history")

    five = history[-5:]
    latest = {
        "date": date.today().isoformat(),
        "close": float(day.get("trade") or 0) or None,
        "pct_chg": round(float(day.get("changeratio") or 0) * 100, 2),
        "main": main,
        "main_yi": _yi(main),
        "main_ratio": None,
        "super_yi": _yi(super_net),
        "big_yi": _yi(big_net),
        "mid_yi": _yi(mid_net),
        "small_yi": _yi(small_net),
    }
    return {
        "code": code,
        "date": latest["date"],
        "available": True,
        "latest": latest,
        "history": history[-10:],
        "main_5d_yi": round(sum(item["main_yi"] or 0 for item in five), 2),
        "days": len(five),
        "source": "sina",
        "source_label": "新浪（东财限流降级）",
        "metric_label": "全单净流入",
        "note": "主力=超大单+大单；历史曲线为全单净额",
    }


def _parse_stock_flow(df: pd.DataFrame) -> list[dict]:
    if df is None or df.empty:
        return []
    required = "主力净流入-净额"
    if required not in df.columns:
        raise RuntimeError(f"unexpected stock flow columns: {list(df.columns)}")

    def number(row: pd.Series, column: str) -> float | None:
        if column not in df.columns:
            return None
        try:
            return float(row[column])
        except (TypeError, ValueError):
            return None

    rows: list[dict] = []
    for _, row in df.iterrows():
        try:
            day = str(row.get("日期"))[:10]
            main = number(row, "主力净流入-净额")
            if not day or main is None:
                continue
            rows.append(
                {
                    "date": day,
                    "close": number(row, "收盘价"),
                    "pct_chg": number(row, "涨跌幅"),
                    "main": main,
                    "main_yi": _yi(main),
                    "main_ratio": number(row, "主力净流入-净占比"),
                    "super_yi": _yi(number(row, "超大单净流入-净额")),
                    "big_yi": _yi(number(row, "大单净流入-净额")),
                    "mid_yi": _yi(number(row, "中单净流入-净额")),
                    "small_yi": _yi(number(row, "小单净流入-净额")),
                }
            )
        except (TypeError, ValueError):
            continue
    return rows


def get_stock_fund_flow(
    code: str,
    day: date | None = None,
    *,
    provider: AkShareProvider | None = None,
    refresh: bool = False,
) -> dict:
    """Daily main-capital flow for one stock, cached per trading day."""
    try:
        code = normalize_code(code)
    except ValueError:
        return {"code": code, "available": False, "error": "invalid code"}

    day = day or date.today()
    kind = f"stock_flow_{code}"
    if not refresh:
        cached = _read_cache(kind, day, STOCK_FLOW_TTL)
        if cached is not None:
            return cached
    else:
        cached = _read_cache(kind, day, 7 * 24 * 3600)
        if (
            cached is not None
            and cached.get("source") == "sina"
            and time.time() - float(cached.get("fetched_ts", 0)) < 600
        ):
            cached["refresh_skipped"] = True
            return cached

    provider = provider or AkShareProvider()
    try:
        rows = _parse_stock_flow(provider.get_stock_fund_flow(code))
        if not rows:
            raise RuntimeError("empty stock flow")
        latest = rows[-1]
        five = rows[-5:]
        payload = {
            "code": code,
            "date": latest["date"],
            "available": True,
            "latest": latest,
            "history": rows[-10:],
            "main_5d_yi": round(sum(item["main_yi"] or 0 for item in five), 2),
            "days": len(five),
            "source": "eastmoney",
            "source_label": "东方财富",
            "metric_label": "主力净流入",
        }
        return _write_cache(kind, day, payload)
    except Exception as exc:  # noqa: BLE001 - the panel must degrade, not fail
        logger.warning("stock flow unavailable for {}: {}", code, exc)
        try:
            payload = _sina_stock_flow(code)
            return _write_cache(kind, day, payload)
        except Exception as sina_exc:  # noqa: BLE001
            logger.warning("sina stock flow also failed for {}: {}", code, sina_exc)

        cached = _read_cache(kind, day, 7 * 24 * 3600)
        if cached is not None and cached.get("available"):
            cached["stale"] = True
            return cached
        return {"code": code, "available": False, "error": str(exc)[:300]}


__all__ = ["get_lhb", "get_sector_flow", "get_stock_fund_flow"]
