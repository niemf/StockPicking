"""Technical feature builders.

Each builder receives an OHLCV frame (oldest first) and returns named series.
Registering a new indicator is one decorated function; every factor can then
reference its column by name. This keeps the math independent from the screen
configuration and from the HTTP/DB layers.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

FeatureBuilder = Callable[[pd.DataFrame], dict[str, pd.Series]]

BUILDERS: dict[str, FeatureBuilder] = {}
BUILDER_PROVIDES: dict[str, tuple[str, ...]] = {}
FEATURE_OWNERS: dict[str, str] = {}
BUILDER_DEPS: dict[str, tuple[str, ...]] = {
    "ma_slope": ("moving_averages",),
    "candle_shape": ("returns",),
}


def feature_builder(name: str, *, provides: tuple[str, ...] = (), deps: tuple[str, ...] = ()):
    def decorator(func: FeatureBuilder) -> FeatureBuilder:
        BUILDERS[name] = func
        BUILDER_PROVIDES[name] = provides
        BUILDER_DEPS.setdefault(name, deps)
        for key in provides:
            FEATURE_OWNERS[key] = name
        return func

    return decorator


def compute_features(bars: pd.DataFrame) -> pd.DataFrame:
    """Build every registered feature once and return them as a DataFrame."""
    if bars is None or bars.empty:
        return pd.DataFrame()
    frame = bars.copy()
    for builder in BUILDERS.values():
        for key, series in builder(frame).items():
            frame[key] = series
    return frame


# ------------------------------------------------------------------ trend
@feature_builder("ma20", provides=("ma20",))
def _ma20(df: pd.DataFrame) -> dict[str, pd.Series]:
    return {"ma20": df["close"].rolling(20).mean()}


@feature_builder("ma_other", provides=("ma5", "ma10", "ma30", "ma60"))
def _ma_other(df: pd.DataFrame) -> dict[str, pd.Series]:
    close = df["close"]
    return {
        "ma5": close.rolling(5).mean(),
        "ma10": close.rolling(10).mean(),
        "ma30": close.rolling(30).mean(),
        "ma60": close.rolling(60).mean(),
    }


@feature_builder("ma_slope", provides=("ma20_slope3",), deps=("ma20",))
def _ma_slope(df: pd.DataFrame) -> dict[str, pd.Series]:
    ma20 = df["ma20"]
    prev = ma20.shift(3)
    return {"ma20_slope3": (ma20 - prev) / prev * 100}


@feature_builder("boll", provides=("boll_mid", "boll_upper", "boll_lower", "boll_width"))
def _boll(df: pd.DataFrame) -> dict[str, pd.Series]:
    close = df["close"]
    mid = close.rolling(20).mean()
    std = close.rolling(20).std(ddof=0)
    return {
        "boll_mid": mid,
        "boll_upper": mid + 2 * std,
        "boll_lower": mid - 2 * std,
        "boll_width": (4 * std) / mid * 100,
    }


@feature_builder("breakout_levels", provides=("high20", "high20_prev"))
def _breakout_levels(df: pd.DataFrame) -> dict[str, pd.Series]:
    high = df["high"]
    rolled = high.rolling(20).max()
    return {"high20": rolled, "high20_prev": rolled.shift(1)}


@feature_builder(
    "overhead",
    provides=("high60_prev", "low20", "range20_pct", "resistance_dist"),
)
def _overhead(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Nearest overhead level and the 20-day range (measured-move target).

    ``resistance_dist`` is the distance to the closest level above the current
    close: the 20-day high first, then the 60-day high. It is deliberately
    conservative — a stock sitting right under its prior high has little room,
    which is exactly what the risk gate should reject.
    """
    close = df["close"]
    high20 = df["high"].rolling(20).max()
    low20 = df["low"].rolling(20).min()
    high20_prev = high20.shift(1)
    high60_prev = df["high"].rolling(60).max().shift(1)
    resistance = high20_prev.where(high20_prev > close, high60_prev)
    resistance = resistance.where(resistance > close)
    return {
        "high60_prev": high60_prev,
        "low20": low20,
        "range20_pct": (high20 - low20) / close * 100,
        "resistance_dist": (resistance / close - 1) * 100,
    }


# ------------------------------------------------------------------ momentum
@feature_builder("macd", provides=("macd_dif", "macd_dea", "macd_hist"))
def _macd(df: pd.DataFrame) -> dict[str, pd.Series]:
    close = df["close"]
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    return {"macd_dif": dif, "macd_dea": dea, "macd_hist": (dif - dea) * 2}


@feature_builder("rsi", provides=("rsi12",))
def _rsi(df: pd.DataFrame) -> dict[str, pd.Series]:
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    period = 12
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return {"rsi12": (100 - 100 / (1 + rs)).fillna(100.0)}


@feature_builder("returns", provides=("pct_chg", "ret20"))
def _returns(df: pd.DataFrame) -> dict[str, pd.Series]:
    close = df["close"]
    return {
        "pct_chg": close.pct_change() * 100,
        "ret20": close.pct_change(20) * 100,
    }


@feature_builder("short_momentum", provides=("ret5", "up_streak"))
def _short_momentum(df: pd.DataFrame) -> dict[str, pd.Series]:
    close = df["close"]
    up = close > close.shift(1)
    groups = (~up).cumsum()
    streak = up.astype(int).groupby(groups).cumsum()
    return {
        "ret5": close.pct_change(5) * 100,
        "up_streak": streak,
    }


@feature_builder("drawdown", provides=("drawdown20",))
def _drawdown(df: pd.DataFrame) -> dict[str, pd.Series]:
    close = df["close"]
    peak = close.rolling(20).max()
    # Current drawdown from the 20-session high: cheap and intuitive for a
    # "is this name still near its high" risk gate.
    return {"drawdown20": (close / peak - 1) * 100}


# ------------------------------------------------------------------ volatility
@feature_builder("atr", provides=("atr14", "atr_pct"))
def _atr(df: pd.DataFrame) -> dict[str, pd.Series]:
    prev_close = df["close"].shift(1)
    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = true_range.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    return {"atr14": atr, "atr_pct": atr / df["close"] * 100}


# ------------------------------------------------------------------ volume
@feature_builder(
    "volume",
    provides=("volume_ratio",),
)
def _volume(df: pd.DataFrame) -> dict[str, pd.Series]:
    volume = df["volume"]
    vol_ma5 = volume.rolling(5).mean()
    return {
        # Today vs the previous five sessions, so today's spike does not dilute it.
        "volume_ratio": volume / vol_ma5.shift(1),
    }


# ------------------------------------------------------------------ shape
@feature_builder(
    "candle_shape",
    provides=(
        "close_position",
        "gap_pct",
        "limit_up_streak",
        "upper_shadow_pct",
        "lower_shadow_pct",
    ),
    deps=("returns",),
)
def _candle_shape(df: pd.DataFrame) -> dict[str, pd.Series]:
    price_range = (df["high"] - df["low"]).replace(0, np.nan)
    prev_close = df["close"].shift(1)
    body_top = df[["open", "close"]].max(axis=1)
    body_bottom = df[["open", "close"]].min(axis=1)
    limit_up = df["pct_chg"] >= 9.7
    streak = limit_up.astype(int)
    # Run length of consecutive limit-ups ending at each row.
    groups = (~limit_up).cumsum()
    streak = streak.groupby(groups).cumsum()
    return {
        "close_position": (df["close"] - df["low"]) / price_range,
        "gap_pct": (df["open"] - prev_close) / prev_close * 100,
        "limit_up_streak": streak,
        "upper_shadow_pct": (df["high"] - body_top) / df["close"] * 100,
        "lower_shadow_pct": (body_bottom - df["low"]) / df["close"] * 100,
    }


__all__ = ["BUILDERS", "compute_features", "feature_builder"]
