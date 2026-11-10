"""Tencent real-time quote provider.

Endpoint: https://qt.gtimg.cn/q=sh600000,sz000001
Response encoding: GBK.
"""

from __future__ import annotations

import re
from datetime import datetime

import httpx

from app.providers.base import Quote, normalize_code, safe_float, to_prefixed_code

_LINE_RE = re.compile(r'v_([a-z]{2}\d{6})="([^"]*)"')


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

    def get_quotes(self, codes: list[str]) -> dict[str, Quote]:
        if not codes:
            return {}
        prefixed = ",".join(to_prefixed_code(code) for code in codes)
        url = f"https://qt.gtimg.cn/q={prefixed}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Referer": "https://gu.qq.com/",
        }
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
        return parse_tencent_quotes(response.content.decode("gbk", errors="replace"))
