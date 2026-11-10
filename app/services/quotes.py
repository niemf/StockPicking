"""Real-time quote service with multi-provider fallback and a short TTL cache."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable

from loguru import logger

from app.config import settings
from app.providers.base import Quote, QuoteProvider, normalize_code
from app.providers.sina import SinaProvider
from app.providers.tencent import TencentProvider


class QuoteService:
    def __init__(
        self,
        providers: Iterable[QuoteProvider] | None = None,
        cache_seconds: int | None = None,
    ) -> None:
        self.providers = list(providers or [TencentProvider(settings.quote_timeout_seconds), SinaProvider(settings.quote_timeout_seconds)])
        self.cache_seconds = cache_seconds if cache_seconds is not None else settings.quote_cache_seconds
        self._cache: dict[str, tuple[float, Quote]] = {}
        self._index_cache: dict[str, tuple[float, Quote]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ stocks
    def get_quotes(self, codes: Iterable[str]) -> dict[str, Quote]:
        """Return quotes keyed by 6-digit code. Partial results are allowed."""
        normalized: list[str] = []
        for code in codes:
            try:
                value = normalize_code(code)
            except ValueError:
                logger.warning("skip invalid stock code: {}", code)
                continue
            if value not in normalized:
                normalized.append(value)

        result: dict[str, Quote] = {}
        missing: list[str] = []
        now = time.monotonic()
        with self._lock:
            for code in normalized:
                cached = self._cache.get(code)
                if cached and now - cached[0] < self.cache_seconds:
                    result[code] = cached[1]
                else:
                    missing.append(code)

        if missing:
            for provider in self.providers:
                try:
                    raw = provider.get_quotes(missing)
                except Exception as exc:  # noqa: BLE001 - provider failure must not break the page
                    logger.warning("{} quote request failed: {}", provider.name, exc)
                    continue

                by_code = {quote.code: quote for quote in raw.values()}
                got = {code: quote for code, quote in by_code.items() if code in missing}
                if got:
                    with self._lock:
                        for code, quote in got.items():
                            self._cache[code] = (now, quote)
                    result.update(got)
                    missing = [code for code in missing if code not in got]
                if not missing:
                    break
                logger.warning("{} missing quotes: {}", provider.name, missing)

        return result

    def get_quote(self, code: str) -> Quote | None:
        try:
            value = normalize_code(code)
        except ValueError:
            return None
        return self.get_quotes([value]).get(value)

    # ------------------------------------------------------------------ indices
    def get_index_quotes(self, prefixed_codes: Iterable[str]) -> dict[str, Quote]:
        """Return index quotes keyed by prefixed code, e.g. ``sh000001``.

        Indices must keep their exchange prefix because ``000001`` is also a
        Shenzhen stock code.
        """
        requested = [code.strip().lower() for code in prefixed_codes]
        result: dict[str, Quote] = {}
        missing: list[str] = []
        now = time.monotonic()
        with self._lock:
            for code in requested:
                cached = self._index_cache.get(code)
                if cached and now - cached[0] < self.cache_seconds:
                    result[code] = cached[1]
                else:
                    missing.append(code)

        if missing:
            for provider in self.providers:
                try:
                    raw = provider.get_quotes(missing)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("{} index request failed: {}", provider.name, exc)
                    continue
                got = {code: raw[code] for code in missing if code in raw}
                if got:
                    with self._lock:
                        for code, quote in got.items():
                            self._index_cache[code] = (now, quote)
                    result.update(got)
                    missing = [code for code in missing if code not in got]
                if not missing:
                    break

        return result
