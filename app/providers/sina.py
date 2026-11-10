"""Sina real-time quote provider.

Endpoint: https://hq.sinajs.cn/list=sh600000,sz000001
Requires a Referer header. Response encoding: GBK.
"""

from __future__ import annotations

import re
from datetime import datetime

import httpx

from app.providers.base import Quote, normalize_code, safe_float, to_prefixed_code

_LINE_RE = re.compile(r'var hq_str_([a-z]{2}\d{6})="([^"]*)";')


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

    def get_quotes(self, codes: list[str]) -> dict[str, Quote]:
        if not codes:
            return {}
        prefixed = ",".join(to_prefixed_code(code) for code in codes)
        url = f"https://hq.sinajs.cn/list={prefixed}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Referer": "https://finance.sina.com.cn/",
        }
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
        return parse_sina_quotes(response.content.decode("gbk", errors="replace"))
