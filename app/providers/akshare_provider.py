"""AkShare-based provider for daily bars, financials, announcements and calendars.

Tencent/Sina real-time endpoints only expose snapshots. Historical daily bars,
financial reports and announcement metadata are fetched through AkShare.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd
from loguru import logger

from app.providers.base import board_label, is_main_board, normalize_code


@dataclass(slots=True)
class Stock:
    code: str
    name: str
    exchange: str
    board: str


_AK = None


def _ak():
    """Import akshare lazily: it is slow and should not block app startup."""
    global _AK
    if _AK is None:
        import akshare as ak

        _AK = ak
    return _AK


def _pick_column(df: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    for name in candidates:
        if name in df.columns:
            return name
    return None


def _as_date(value) -> date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.date()


def _board_label(code: str) -> str:
    return board_label(code)


class AkShareProvider:
    """Thin, defensive wrapper around AkShare endpoints."""

    name = "akshare"

    # ------------------------------------------------------------------ stock list
    def get_listed_stocks(self) -> list[dict]:
        """Return main-board stocks with listing date and industry.

        Two exchange lists are enough for the universe metadata; unlike the
        generic code-name endpoint they include ``上市日期``, which the sub-new
        filter needs.
        """
        ak = _ak()
        stocks: list[dict] = []

        try:
            df = ak.stock_info_sh_name_code(symbol="主板A股")
        except Exception as exc:  # noqa: BLE001 - keep the Shenzhen half usable
            logger.warning("Shanghai list failed: {}", exc)
            df = None
        if df is not None and not df.empty:
            code_col = _pick_column(df, ("证券代码", "代码", "code"))
            name_col = _pick_column(df, ("证券简称", "简称", "name"))
            date_col = _pick_column(df, ("上市日期", "上市时间", "list_date"))
            for _, row in df.iterrows():
                code = normalize_code(row[code_col])
                if not is_main_board(code):
                    continue
                stocks.append(
                    {
                        "code": code,
                        "name": str(row[name_col]).strip(),
                        "exchange": "sh",
                        "board": board_label(code),
                        "list_date": _as_date(row[date_col]) if date_col else None,
                        "industry": None,
                    }
                )

        try:
            df = ak.stock_info_sz_name_code(symbol="A股列表")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Shenzhen list failed: {}", exc)
            df = None
        if df is not None and not df.empty:
            code_col = _pick_column(df, ("A股代码", "证券代码", "代码", "code"))
            name_col = _pick_column(df, ("A股简称", "证券简称", "简称", "name"))
            date_col = _pick_column(df, ("A股上市日期", "上市日期"))
            industry_col = _pick_column(df, ("所属行业", "行业"))
            for _, row in df.iterrows():
                code = normalize_code(row[code_col])
                if not is_main_board(code):
                    continue
                stocks.append(
                    {
                        "code": code,
                        "name": str(row[name_col]).strip(),
                        "exchange": "sz",
                        "board": board_label(code),
                        "list_date": _as_date(row[date_col]) if date_col else None,
                        "industry": str(row[industry_col]).strip() if industry_col else None,
                    }
                )

        # De-duplicate by code, preferring the row that carries a listing date.
        by_code: dict[str, dict] = {}
        for row in stocks:
            current = by_code.get(row["code"])
            if current is None or (current.get("list_date") is None and row.get("list_date")):
                by_code[row["code"]] = row
        return sorted(by_code.values(), key=lambda item: item["code"])

    def get_stock_list(self, main_board_only: bool = True) -> list[Stock]:
        ak = _ak()
        df = ak.stock_info_a_code_name()
        if df is None or df.empty:
            return []

        code_col = _pick_column(df, ("code", "代码"))
        name_col = _pick_column(df, ("name", "名称"))
        if code_col is None or name_col is None:
            raise RuntimeError(f"unexpected stock list columns: {list(df.columns)}")

        stocks: list[Stock] = []
        for _, row in df.iterrows():
            try:
                code = normalize_code(row[code_col])
            except ValueError:
                continue
            if main_board_only and not is_main_board(code):
                continue
            stocks.append(
                Stock(
                    code=code,
                    name=str(row[name_col]).strip(),
                    exchange="sh" if code.startswith(("600", "601", "603", "605")) else "sz",
                    board=_board_label(code),
                )
            )
        return stocks

    # ------------------------------------------------------------------ daily bars
    def get_daily_bars(
        self,
        code: str,
        start: date | None = None,
        end: date | None = None,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """Return daily OHLCV bars.

        ``adjust``: ``qfq`` (forward adjusted), ``hfq`` or ``""`` (raw).
        Volume is in lots (手), amount in CNY.
        """
        ak = _ak()
        code = normalize_code(code)
        end = end or date.today()
        start = start or (end - timedelta(days=240))

        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust=adjust,
        )
        if df is None or df.empty:
            return pd.DataFrame()

        rename = {
            "日期": "trade_date",
            "date": "trade_date",
            "股票代码": "code",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
            "成交额": "amount",
            "振幅": "amplitude",
            "涨跌幅": "pct_chg",
            "涨跌额": "change",
            "换手率": "turnover_rate",
        }
        df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
        if "trade_date" not in df.columns:
            raise RuntimeError(f"unexpected daily bar columns: {list(df.columns)}")
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        df["code"] = code
        # AkShare reports 成交量 in lots (手); the rest of the app (Quote.volume)
        # works in shares, so normalize once here.
        if "volume" in df.columns:
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0) * 100
        return df.sort_values("trade_date").reset_index(drop=True)

    # ------------------------------------------------------------------ financials
    def get_financial_indicators(self, code: str, start_year: str | None = None) -> pd.DataFrame:
        """Return key financial indicators (one row per report period)."""
        ak = _ak()
        code = normalize_code(code)
        start_year = start_year or str(date.today().year - 2)
        errors: list[str] = []

        try:
            df = ak.stock_financial_analysis_indicator(symbol=code, start_year=start_year)
            if df is not None and not df.empty:
                return df
        except Exception as exc:  # noqa: BLE001 - third-party API is unstable
            errors.append(f"stock_financial_analysis_indicator: {exc}")

        try:
            df = ak.stock_financial_abstract(symbol=code)
            if df is not None and not df.empty:
                return df
        except Exception as exc:  # noqa: BLE001
            errors.append(f"stock_financial_abstract: {exc}")

        raise RuntimeError("; ".join(errors) or "no financial data returned")

    # ------------------------------------------------------------------ announcements
    def get_announcements(self, code: str, days: int = 30) -> pd.DataFrame:
        """Return recent announcement metadata, filtered by stock code when possible."""
        ak = _ak()
        code = normalize_code(code)
        end = date.today()
        start = end - timedelta(days=days)

        try:
            df = ak.stock_zh_a_disclosure_report_cninfo(
                symbol=code,
                market="沪深京",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
            )
            if df is not None and not df.empty:
                return df
        except Exception:  # noqa: BLE001
            pass

        try:
            df = ak.stock_notice_report(symbol="全部", date=end.strftime("%Y%m%d"))
            if df is not None and not df.empty:
                code_col = _pick_column(df, ("代码", "股票代码", "code"))
                if code_col is not None:
                    df = df[df[code_col].astype(str).str.zfill(6) == code]
                return df.reset_index(drop=True)
        except Exception:  # noqa: BLE001
            pass

        return pd.DataFrame()

    # ------------------------------------------------------------------ market flow
    def get_sector_fund_flow(self, indicator: str = "今日") -> pd.DataFrame:
        """Industry-level capital flow ranking (Eastmoney)."""
        ak = _ak()
        return ak.stock_sector_fund_flow_rank(indicator=indicator, sector_type="行业资金流")

    def get_lhb_detail(self, start: date, end: date) -> pd.DataFrame:
        """Dragon-Tiger list detail (Eastmoney)."""
        ak = _ak()
        return ak.stock_lhb_detail_em(
            start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d")
        )

    def get_sector_spot(self) -> pd.DataFrame:
        """Sina industry-board performance, used when fund-flow data is down."""
        ak = _ak()
        return ak.stock_sector_spot(indicator="新浪行业")

    # ------------------------------------------------------------------ calendar
    def get_trade_calendar(self, start: date | None = None, end: date | None = None) -> list[date]:
        ak = _ak()
        df = ak.tool_trade_date_hist_sina()
        if df is None or df.empty:
            return []
        col = _pick_column(df, ("trade_date", "日期"))
        if col is None:
            col = df.columns[0]
        dates = pd.to_datetime(df[col]).dt.date.tolist()
        if start:
            dates = [d for d in dates if d >= start]
        if end:
            dates = [d for d in dates if d <= end]
        return dates

    def is_trade_day(self, day: date | None = None) -> bool:
        day = day or date.today()
        return day in set(self.get_trade_calendar(start=day, end=day))

    # ------------------------------------------------------------------ index
    def get_index_daily(
        self,
        symbol: str = "sh000001",
        start: date | None = None,
        end: date | None = None,
    ) -> pd.DataFrame:
        ak = _ak()
        df = ak.stock_zh_index_daily(symbol=symbol)
        if df is None or df.empty:
            return pd.DataFrame()

        # AkShare has returned ``date`` both as a column and as the index
        # depending on version, so normalize both shapes.
        df = df.reset_index()
        df = df.rename(columns={"date": "trade_date", "日期": "trade_date"})
        if "trade_date" not in df.columns:
            first_col = df.columns[0]
            df = df.rename(columns={first_col: "trade_date"})
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        if start:
            df = df[df["trade_date"] >= start]
        if end:
            df = df[df["trade_date"] <= end]
        return df.reset_index(drop=True)
