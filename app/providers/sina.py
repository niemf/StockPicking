"""Sina real-time quote provider.

Endpoint: https://hq.sinajs.cn/list=sh600000,sz000001
Requires a Referer header. Response encoding: GBK.

The daily-kline endpoint is a last-resort bar source: it is fast and rarely
rate limited, but it returns **raw (unadjusted)** prices, so it must only be
used for recent sessions where no ex-dividend event can have occurred.
"""

from __future__ import annotations

import re
from datetime import date, datetime

import httpx
import pandas as pd

from app.providers.base import Quote, normalize_code, safe_float, to_prefixed_code

_LINE_RE = re.compile(r'var hq_str_([a-z]{2}\d{6})="([^"]*)";')
_KLINE_URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"


def parse_sina_quotes(text: str) -> dict[str, Quote]:
    """Parse the raw Sina response body into normalized quotes."""
    result: dict[str, Quote] = {}
    for prefixed, payload in _LINE_RE.findall(text):
        if not payload.strip():
            continue
        fields = payload.split(",")
        if len(fields) < 32:
            continue
        try:
            # The payload itself does not contain the code; the variable name
            # (e.g. sh600000) is the reliable source.
            code = normalize_code(prefixed)
        except ValueError:
            continue

        pre_close = safe_float(fields[2])
        price = safe_float(fields[3])
        if price <= 0:
            continue
        change = price - pre_close
        pct_chg = (change / pre_close * 100) if pre_close else 0.0

        ts = None
        try:
            ts = datetime.strptime(f"{fields[30]} {fields[31]}", "%Y-%m-%d %H:%M:%S")
        except (ValueError, IndexError):
            pass

        result[prefixed] = Quote(
            code=code,
            name=fields[0].strip(),
            price=price,
            pre_close=pre_close,
            open=safe_float(fields[1]),
            high=safe_float(fields[4]),
            low=safe_float(fields[5]),
            volume=safe_float(fields[8]),
            amount=safe_float(fields[9]),
            pct_chg=pct_chg,
            change=change,
            turnover_rate=0.0,
            ts=ts,
            source="sina",
        )
    return result


class SinaProvider:
    name = "sina"

    def __init__(self, timeout: float = 8.0) -> None:
        self.timeout = timeout
        # One client per provider: bar fetches reuse the TLS connection, which
        # is the difference between ~3 and ~30 requests per second.
        self._client = httpx.Client(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Referer": "https://finance.sina.com.cn/",
        }

    def get_quotes(self, codes: list[str]) -> dict[str, Quote]:
        if not codes:
            return {}
        prefixed = ",".join(to_prefixed_code(code) for code in codes)
        url = f"https://hq.sinajs.cn/list={prefixed}"
        response = self._client.get(url, headers=self._headers())
        response.raise_for_status()
        return parse_sina_quotes(response.content.decode("gbk", errors="replace"))

    def get_daily_bars(
        self,
        code: str,
        start: date | None = None,
        end: date | None = None,
        adjust: str = "",
    ) -> pd.DataFrame:
        """Recent daily bars (raw prices) from Sina's kline API."""
        value = normalize_code(code)
        end = end or date.today()
        start = start or (end - pd.Timedelta(days=30)).date()
        length = min(200, max(12, (end - start).days))
        params = {
            "symbol": to_prefixed_code(value),
            "scale": "240",
            "ma": "no",
            "datalen": str(length),
        }
        response = self._client.get(_KLINE_URL, params=params, headers=self._headers())
        response.raise_for_status()
        payload = response.json()

        if not isinstance(payload, list) or not payload:
            return pd.DataFrame()

        records: list[dict] = []
        previous: float | None = None
        for row in payload:
            trade_date = pd.to_datetime(row.get("day"), errors="coerce")
            if pd.isna(trade_date):
                continue
            close = safe_float(row.get("close"))
            if close <= 0:
                continue
            records.append(
                {
                    "trade_date": trade_date.date(),
                    "open": safe_float(row.get("open")),
                    "high": safe_float(row.get("high")),
                    "low": safe_float(row.get("low")),
                    "close": close,
                    "pre_close": previous,
                    "volume": safe_float(row.get("volume")),  # Sina reports shares
                    "amount": 0.0,
                    "pct_chg": ((close / previous - 1) * 100) if previous else None,
                }
            )
            previous = close
        frame = pd.DataFrame(records)
        return frame[(frame["trade_date"] >= start) & (frame["trade_date"] <= end)].reset_index(drop=True)
