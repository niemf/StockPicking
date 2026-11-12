"""Tencent real-time quote provider.

Endpoint: https://qt.gtimg.cn/q=sh600000,sz000001
Response encoding: GBK.
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime

import httpx
import pandas as pd

from app.providers.base import Quote, normalize_code, safe_float, to_prefixed_code

_LINE_RE = re.compile(r'v_([a-z]{2}\d{6})="([^"]*)"')
_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def _parse_ts(value: str) -> datetime | None:
    value = value.strip()
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def parse_tencent_quotes(text: str) -> dict[str, Quote]:
    """Parse the raw Tencent response body into normalized quotes."""
    result: dict[str, Quote] = {}
    for prefixed, payload in _LINE_RE.findall(text):
        fields = payload.split("~")
        if len(fields) < 40:
            continue
        try:
            code = normalize_code(fields[2])
        except ValueError:
            continue

        quote = Quote(
            code=code,
            name=fields[1].strip(),
            price=safe_float(fields[3]),
            pre_close=safe_float(fields[4]),
            open=safe_float(fields[5]),
            high=safe_float(fields[33]),
            low=safe_float(fields[34]),
            volume=safe_float(fields[6]) * 100,  # 手 -> 股
            amount=safe_float(fields[37]) * 10_000,  # 万元 -> 元
            pct_chg=safe_float(fields[32]),
            change=safe_float(fields[31]),
            turnover_rate=safe_float(fields[38]),
            limit_up=safe_float(fields[47]) if len(fields) > 47 else 0.0,
            limit_down=safe_float(fields[48]) if len(fields) > 48 else 0.0,
            ts=_parse_ts(fields[30]),
            source="tencent",
        )
        if quote.price <= 0:
            # Suspended / invalid codes often return 0. Keep the record but
            # callers can decide to skip it.
            continue
        result[prefixed] = quote
    return result


class TencentProvider:
    name = "tencent"

    def __init__(self, timeout: float = 8.0) -> None:
        self.timeout = timeout
        # A shared client keeps the connection alive across per-stock bar
        # requests; httpx.Client is thread-safe.
        self._client = httpx.Client(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Referer": "https://gu.qq.com/",
        }

    def get_quotes(self, codes: list[str]) -> dict[str, Quote]:
        if not codes:
            return {}
        prefixed = ",".join(to_prefixed_code(code) for code in codes)
        url = f"https://qt.gtimg.cn/q={prefixed}"
        response = self._client.get(url, headers=self._headers())
        response.raise_for_status()
        return parse_tencent_quotes(response.content.decode("gbk", errors="replace"))

    def get_daily_bars(
        self,
        code: str,
        start: date | None = None,
        end: date | None = None,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """Daily bars from the Tencent kline endpoint.

        Used as a fallback when AkShare/Eastmoney is rate limited. Returns the
        same normalized shape as ``AkShareProvider.get_daily_bars``: volume in
        shares, no amount (Tencent omits it), forward-adjusted prices.
        """
        value = normalize_code(code)
        end = end or date.today()
        start = start or (end - pd.Timedelta(days=240)).date()
        prefix = to_prefixed_code(value)
        count = min(800, max(20, (end - start).days * 2))
        params = {
            "param": f"{prefix},day,{start:%Y-%m-%d},{end:%Y-%m-%d},{count},{adjust or ''}".rstrip(","),
        }
        headers = self._headers()
        payload: dict = {}
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self._client.get(_KLINE_URL, params=params, headers=headers)
                response.raise_for_status()
                payload = response.json()
                break
            except Exception as exc:  # noqa: BLE001 - the endpoint randomly returns 501
                last_error = exc
                time.sleep(0.25 * (attempt + 1))
        if not payload:
            raise RuntimeError(f"tencent kline failed for {value}: {last_error}")

        node = ((payload or {}).get("data") or {}).get(prefix) or {}
        key = {"qfq": "qfqday", "hfq": "hfqday"}.get(adjust, "day")
        rows = node.get(key) or node.get("day") or []
        if not rows:
            return pd.DataFrame()

        records: list[dict] = []
        previous: float | None = None
        for row in rows:
            if len(row) < 6:
                continue
            trade_date = pd.to_datetime(row[0], errors="coerce")
            if pd.isna(trade_date):
                continue
            open_, close, high, low, volume = (safe_float(item) for item in row[1:6])
            if close <= 0:
                continue
            records.append(
                {
                    "trade_date": trade_date.date(),
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "pre_close": previous,
                    "volume": volume * 100,  # 手 -> 股
                    "amount": 0.0,
                    "pct_chg": ((close / previous - 1) * 100) if previous else None,
                }
            )
            previous = close
        frame = pd.DataFrame(records)
        return frame[(frame["trade_date"] >= start) & (frame["trade_date"] <= end)].reset_index(drop=True)
