"""Per-stock analysis context.

A context owns the metadata, the quote snapshot and the daily bars. Features are
computed once on first access and then shared by every factor, which is what
keeps the funnel fast: later stages never recompute earlier math.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from app.analysis.features import BUILDER_DEPS, BUILDERS, FEATURE_OWNERS
from app.providers.base import Quote


@dataclass(slots=True)
class StockMeta:
    code: str
    name: str
    industry: str | None = None
    list_date: date | None = None
    is_st: bool = False
    is_delisting_risk: bool = False
    status: str = "normal"


@dataclass
class StockContext:
    meta: StockMeta
    quote: Quote | None = None
    bars: pd.DataFrame | None = None
    listed_days: int = 9999
    extras: dict = field(default_factory=dict)
    _market: pd.DataFrame | None = field(default=None, repr=False)
    _built: set[str] = field(default_factory=set, repr=False)

    @property
    def code(self) -> str:
        return self.meta.code

    @property
    def name(self) -> str:
        return self.meta.name

    @property
    def price(self) -> float:
        if self.quote is not None and self.quote.price > 0:
            return float(self.quote.price)
        if self.bars is not None and not self.bars.empty:
            return float(self.bars["close"].iloc[-1])
        return 0.0

    @property
    def pct_chg(self) -> float:
        if self.quote is not None:
            return float(self.quote.pct_chg)
        value = self.feature("pct_chg")
        return float(value) if value is not None else 0.0

    @property
    def amount(self) -> float:
        if self.quote is not None:
            return float(self.quote.amount)
        return float(self.bars["amount"].iloc[-1]) if self.bars is not None and not self.bars.empty else 0.0

    @property
    def turnover_rate(self) -> float:
        if self.quote is not None:
            return float(self.quote.turnover_rate)
        return 0.0

    def market_frame(self) -> pd.DataFrame:
        """Full feature frame. Use :meth:`feature` for single values."""
        frame = self._prepare()
        for builder in BUILDERS:
            self._ensure(builder)
        return frame

    def _prepare(self) -> pd.DataFrame:
        """Base OHLCV frame with today's snapshot merged in, built once."""
        if self._market is not None:
            return self._market

        frame = self.bars
        if frame is None or frame.empty:
            self._market = pd.DataFrame()
            return self._market

        if self.quote is not None and self.quote.price > 0:
            last_date = _last_date(frame)
            quote_date = self.quote.ts.date() if self.quote.ts else None
            if quote_date is None or last_date != quote_date:
                frame = _append_snapshot(frame, self.quote)
            else:
                # Same session: refresh the last row with the live price/volume.
                frame = frame.copy()
                frame.loc[frame.index[-1], "close"] = self.quote.price
                frame.loc[frame.index[-1], "high"] = max(
                    float(frame["high"].iloc[-1]), self.quote.high or self.quote.price
                )
                frame.loc[frame.index[-1], "low"] = min(
                    float(frame["low"].iloc[-1]), self.quote.low or self.quote.price
                )
                frame.loc[frame.index[-1], "volume"] = self.quote.volume
                frame.loc[frame.index[-1], "amount"] = self.quote.amount

        self._market = frame
        self._built.clear()
        return self._market

    def _ensure(self, builder: str) -> None:
        if builder in self._built or builder not in BUILDERS:
            return
        for dependency in BUILDER_DEPS.get(builder, ()):
            self._ensure(dependency)
        frame = self._prepare()
        for key, series in BUILDERS[builder](frame).items():
            frame[key] = series
        self._built.add(builder)

    def feature(self, key: str, offset: int = 0) -> float | None:
        frame = self._prepare()
        if key not in frame.columns:
            owner = FEATURE_OWNERS.get(key)
            if owner is not None:
                self._ensure(owner)
        if frame.empty or key not in frame.columns:
            return None
        series = frame[key]
        index = len(series) - 1 - offset
        if index < 0 or index >= len(series):
            return None
        value = series.iloc[index]
        if value is None or pd.isna(value):
            return None
        return float(value)

    def has(self, keys: tuple[str, ...]) -> bool:
        for key in keys:
            owner = FEATURE_OWNERS.get(key)
            if owner is not None:
                self._ensure(owner)
        frame = self._prepare()
        return all(key in frame.columns for key in keys)


def _last_date(frame: pd.DataFrame) -> date | None:
    if "trade_date" not in frame.columns or frame.empty:
        return None
    value = frame["trade_date"].iloc[-1]
    return value.date() if hasattr(value, "date") else value


def _append_snapshot(frame: pd.DataFrame, quote: Quote) -> pd.DataFrame:
    row = {
        "trade_date": quote.ts.date() if quote.ts else None,
        "open": quote.open or quote.price,
        "high": quote.high or quote.price,
        "low": quote.low or quote.price,
        "close": quote.price,
        "pre_close": quote.pre_close,
        "volume": quote.volume,
        "amount": quote.amount,
        "turnover_rate": quote.turnover_rate,
    }
    return pd.concat([frame, pd.DataFrame([row])], ignore_index=True)
