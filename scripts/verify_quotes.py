"""Verify Tencent / Sina real-time quote endpoints.

Run:
    .venv\\python.exe scripts\\verify_quotes.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.providers.sina import SinaProvider  # noqa: E402
from app.providers.tencent import TencentProvider  # noqa: E402
from app.services.quotes import QuoteService  # noqa: E402

CODES = ["600000", "000001", "600036", "601318", "000858", "002594", "600519"]
INDICES = ["sh000001", "sz399001", "sz399006", "sh000300"]


def print_quotes(provider_name: str, quotes: dict, elapsed_ms: float) -> None:
    by_code = {quote.code: quote for quote in quotes.values()}
    print(f"\n[{provider_name}] 获取 {len(by_code)}/{len(CODES)} 条，耗时 {elapsed_ms:.0f} ms")
    print(f"  {'代码':<8}{'名称':<10}{'现价':>9}{'涨跌幅':>9}{'成交量(手)':>12}  时间")
    for code in CODES:
        quote = by_code.get(code)
        if quote is None:
            print(f"  {code:<8}{'--':<10}{'MISS':>9}")
            continue
        volume_lots = quote.volume / 100
        print(
            f"  {quote.code:<8}{quote.name:<10}{quote.price:>9.2f}"
            f"{quote.pct_chg:>8.2f}%{volume_lots:>12,.0f}  "
            f"{quote.ts.strftime('%H:%M:%S') if quote.ts else '--:--:--'}"
        )


def main() -> int:
    print("=" * 78)
    print("TradeAI 实时行情验证（腾讯 / 新浪）")
    print("=" * 78)
    failed = False

    for provider in (TencentProvider(), SinaProvider()):
        started = time.perf_counter()
        try:
            quotes = provider.get_quotes(CODES)
        except Exception as exc:  # noqa: BLE001
            print(f"\n[{provider.name}] 请求失败：{exc}")
            failed = True
            continue
        elapsed = (time.perf_counter() - started) * 1000
        print_quotes(provider.name, quotes, elapsed)
        if len({quote.code for quote in quotes.values()}) < len(CODES):
            failed = True

    print("\n" + "-" * 78)
    print("多源降级 + 缓存（QuoteService）")
    service = QuoteService()
    started = time.perf_counter()
    quotes = service.get_quotes(CODES)
    first_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    quotes_cached = service.get_quotes(CODES)
    cached_ms = (time.perf_counter() - started) * 1000
    sources = sorted({quote.source for quote in quotes.values()})
    print(f"  首次 {len(quotes)}/{len(CODES)} 条，耗时 {first_ms:.0f} ms，来源 {sources}")
    print(f"  缓存 {len(quotes_cached)}/{len(CODES)} 条，耗时 {cached_ms:.0f} ms")

    print("\n" + "-" * 78)
    print("指数行情（保留 sh/sz 前缀）")
    index_quotes = service.get_index_quotes(INDICES)
    for code in INDICES:
        quote = index_quotes.get(code)
        if quote is None:
            print(f"  {code}  MISS")
            failed = True
        else:
            print(
                f"  {code}  {quote.name:<8} {quote.price:>10.2f}  "
                f"{quote.pct_chg:>+7.2f}%  {quote.source}"
            )

    print("\n" + "=" * 78)
    if failed:
        print("结果：存在失败项，请检查网络或接口是否变更。")
        return 1
    print("结果：腾讯 / 新浪实时行情均可用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
