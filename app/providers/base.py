"""Shared quote models and code helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(slots=True)
class Quote:
    """A normalized real-time quote."""

    code: str
    name: str
    price: float
    pre_close: float
    open: float
    high: float
    low: float
    volume: float
    amount: float
    pct_chg: float
    change: float
    turnover_rate: float = 0.0
    limit_up: float = 0.0
    limit_down: float = 0.0
    ts: datetime | None = None
    source: str = ""

    @property
    def is_up(self) -> bool:
        return self.pct_chg > 0

    @property
    def is_down(self) -> bool:
        return self.pct_chg < 0


class QuoteProvider(Protocol):
    name: str

    def get_quotes(self, codes: list[str]) -> dict[str, Quote]:
        """Return quotes keyed by 6-digit stock code."""
        ...


def normalize_code(code: str) -> str:
    """Normalize ``sh600000`` / ``600000.SH`` / ``600000`` to ``600000``."""
    value = str(code).strip().lower()
    if "." in value:
        value = value.split(".", 1)[0]
    for prefix in ("sh", "sz", "bj"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
            break
    if not value.isdigit():
        raise ValueError(f"invalid stock code: {code!r}")
    return value.zfill(6)


def exchange_for_code(code: str) -> str:
    """Return ``sh`` / ``sz`` / ``bj`` for a 6-digit A-share code."""
    value = normalize_code(code)
    if value.startswith(("600", "601", "603", "605", "688", "689")):
        return "sh"
    if value.startswith(("000", "001", "002", "003", "300", "301")):
        return "sz"
    if value.startswith(("4", "8", "920")):
        return "bj"
    raise ValueError(f"cannot determine exchange for code: {code!r}")


def to_prefixed_code(code: str) -> str:
    """Return ``sh600000`` / ``sz000001``.

    An existing exchange prefix is preserved, which is required for indices
    (e.g. ``sh000001`` is the Shanghai Composite, not Shenzhen stock 000001).
    """
    raw = str(code).strip().lower()
    if raw.startswith(("sh", "sz", "bj")):
        digits = raw[2:]
        if digits.isdigit() and len(digits) == 6:
            return raw
    value = normalize_code(raw)
    return f"{exchange_for_code(value)}{value}"


def is_main_board(code: str) -> bool:
    """A-share main board only.

    Included: SH 600/601/603/605, SZ 000/001/002/003.
    Excluded: STAR (688), ChiNext (300/301), BSE (4/8/920).
    """
    value = normalize_code(code)
    return value.startswith(("600", "601", "603", "605", "000", "001", "002", "003"))


def board_label(code: str) -> str:
    """Return a human-readable board name for a 6-digit A-share code."""
    value = normalize_code(code)
    if value.startswith(("600", "601", "603", "605")):
        return "沪市主板"
    if value.startswith(("000", "001", "002", "003")):
        return "深市主板"
    if value.startswith(("688", "689")):
        return "科创板"
    if value.startswith(("300", "301")):
        return "创业板"
    if value.startswith(("4", "8", "920")):
        return "北交所"
    return "其他"


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        text = str(value).strip()
        if not text or text in {"-", "--", "None", "nan"}:
            return default
        return float(text)
    except (TypeError, ValueError):
        return default
