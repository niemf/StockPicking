"""Web routes and view models for the UI skeleton."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.templating import Jinja2Templates

from app.demo_data import (
    ALERTS,
    HOLDINGS,
    INDICES,
    MARKET_REGIME,
    WATCHLIST,
    get_stock_detail,
    make_kline,
)
from app.providers.base import normalize_code
from app.services.quotes import QuoteService

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ------------------------------------------------------------------ filters
templates.env.filters["money"] = lambda value: f"{value:,.2f}"
templates.env.filters["price"] = lambda value: f"{value:,.2f}"
templates.env.filters["pct"] = lambda value: f"{value:+.2f}%"
templates.env.filters["compact"] = lambda value: f"{value / 10000:,.1f} 万"

quote_service = QuoteService()

router = APIRouter()


def render(request: Request, name: str, context: dict):
    return templates.TemplateResponse(request, name, context)


# ------------------------------------------------------------------ helpers
def _trend_class(value: float) -> str:
    if value > 0:
        return "up"
    if value < 0:
        return "down"
    return "flat"


def _rating_class(rating: str) -> str:
    return {"看多": "buy", "中性": "neutral", "观察": "watch"}.get(rating, "neutral")


def _snapshot(quote, fallback_price: float, fallback_pct: float) -> dict:
    if quote is not None:
        price = quote.price
        pct_chg = quote.pct_chg
        pre_close = quote.pre_close
        name = quote.name
        ts = quote.ts
        source = quote.source
    else:
        price = fallback_price
        pct_chg = fallback_pct
        pre_close = price / (1 + fallback_pct / 100) if fallback_pct != -100 else price
        name = None
        ts = None
        source = "demo"
    return {
        "price": price,
        "pct_chg": pct_chg,
        "pre_close": pre_close,
        "name": name,
        "ts": ts,
        "time_text": ts.strftime("%H:%M:%S") if ts else "--:--:--",
        "source": source,
        "trend": _trend_class(pct_chg),
    }


def _build_watchlist(quotes: dict) -> list[dict]:
    items: list[dict] = []
    for row in WATCHLIST:
        snap = _snapshot(quotes.get(row["code"]), row["fallback_price"], row["fallback_pct"])
        price = snap["price"]
        buy_low = row["buy_low"]
        buy_high = row["buy_high"]
        stop_loss = row["stop_loss"]
        take_profit = row["take_profit"]

        if price > buy_high:
            zone_status = "高于建仓区"
            distance = (price - buy_high) / buy_high * 100
            distance_text = f"距建仓区 +{distance:.2f}%"
        elif price < buy_low:
            zone_status = "低于建仓区"
            distance = (buy_low - price) / buy_low * 100
            distance_text = f"低于建仓区 {distance:.2f}%"
        else:
            zone_status = "进入建仓区"
            distance_text = "已在建仓区内"

        risk_reward = (
            (take_profit - price) / (price - stop_loss)
            if price > stop_loss
            else 0.0
        )

        items.append(
            {
                **row,
                **snap,
                # ``snap`` carries the *quote* source; the plan source must win.
                "name": row["name"],
                "source": row["source"],
                "data_source": snap["source"],
                "rating_class": _rating_class(row["rating"]),
                "zone_status": zone_status,
                "zone_class": "in" if zone_status == "进入建仓区" else "out",
                "distance_text": distance_text,
                "stop_distance": (price - stop_loss) / price * 100 if price else 0,
                "tp_distance": (take_profit - price) / price * 100 if price else 0,
                "risk_reward": risk_reward,
            }
        )
    return items


def _build_holdings(quotes: dict) -> list[dict]:
    items: list[dict] = []
    for row in HOLDINGS:
        snap = _snapshot(quotes.get(row["code"]), row["fallback_price"], row["fallback_pct"])
        price = snap["price"]
        cost = row["cost"]
        shares = row["shares"]
        market_value = price * shares
        cost_value = cost * shares
        pnl = market_value - cost_value
        pnl_pct = (price / cost - 1) * 100 if cost else 0.0
        today_pnl = (price - snap["pre_close"]) * shares

        stop_loss = row["stop_loss"]
        tp1 = row["take_profit_1"]
        tp2 = row["take_profit_2"]
        stop_distance = (price - stop_loss) / price * 100 if price else 0.0
        tp1_distance = (tp1 - price) / price * 100 if price else 0.0
        progress = 0.0
        if tp1 > cost:
            progress = (price - cost) / (tp1 - cost) * 100
        progress = max(0.0, min(100.0, progress))

        items.append(
            {
                **row,
                **snap,
                "name": row["name"],
                "market_value": market_value,
                "cost_value": cost_value,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "today_pnl": today_pnl,
                "pnl_trend": _trend_class(pnl),
                "today_trend": _trend_class(today_pnl),
                "stop_distance": stop_distance,
                "tp1_distance": tp1_distance,
                "progress": progress,
            }
        )
    return items


def _totals(holdings: list[dict]) -> dict:
    market_value = sum(item["market_value"] for item in holdings)
    cost_value = sum(item["cost_value"] for item in holdings)
    pnl = market_value - cost_value
    today_pnl = sum(item["today_pnl"] for item in holdings)
    pnl_pct = (pnl / cost_value * 100) if cost_value else 0.0
    return {
        "market_value": market_value,
        "cost_value": cost_value,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "today_pnl": today_pnl,
        "count": len(holdings),
        "trend": _trend_class(pnl),
        "today_trend": _trend_class(today_pnl),
    }


def _build_indices() -> list[dict]:
    quotes = quote_service.get_index_quotes([row["code"] for row in INDICES])
    items: list[dict] = []
    for row in INDICES:
        snap = _snapshot(quotes.get(row["code"]), row["fallback_price"], row["fallback_pct"])
        items.append({**row, **snap, "name": row["name"]})
    return items


def _chart_payload(code: str, base_price: float) -> dict:
    kline = make_kline(base_price, seed=int(code))
    closes = [row["close"] for row in kline]

    def moving_average(window: int) -> list[float | None]:
        values: list[float | None] = []
        for index in range(len(closes)):
            if index + 1 < window:
                values.append(None)
            else:
                chunk = closes[index + 1 - window : index + 1]
                values.append(round(sum(chunk) / window, 2))
        return values

    return {
        "dates": [row["date"] for row in kline],
        "kline": [[row["open"], row["close"], row["low"], row["high"]] for row in kline],
        "volume": [row["volume"] for row in kline],
        "ma5": moving_average(5),
        "ma20": moving_average(20),
        "base_price": base_price,
    }


# ------------------------------------------------------------------ pages
@router.get("/")
def dashboard(request: Request):
    codes = {row["code"] for row in WATCHLIST} | {row["code"] for row in HOLDINGS}
    quotes = quote_service.get_quotes(codes)
    watchlist = _build_watchlist(quotes)
    holdings = _build_holdings(quotes)
    totals = _totals(holdings)
    indices = _build_indices()

    allocation = [
        {"name": item["name"], "value": round(item["market_value"], 2)}
        for item in holdings
    ]
    pnl_bars = [
        {"name": item["name"], "value": round(item["pnl"], 2)}
        for item in holdings
    ]
    ai_count = sum(1 for item in watchlist if item["source"] == "AI 候选")
    alert_counts = {
        "success": sum(1 for alert in ALERTS if alert["level"] == "success"),
        "warning": sum(1 for alert in ALERTS if alert["level"] == "warning"),
        "info": sum(1 for alert in ALERTS if alert["level"] == "info"),
    }

    return render(
        request,
        "dashboard.html",
        {
            "page": "dashboard",
            "page_title": "仪表盘",
            "regime": MARKET_REGIME,
            "indices": indices,
            "watchlist": watchlist[:4],
            "watchlist_total": len(watchlist),
            "ai_count": ai_count,
            "rule_count": len(watchlist) - ai_count,
            "holdings": holdings,
            "totals": totals,
            "alerts": ALERTS,
            "alert_counts": alert_counts,
            "allocation": allocation,
            "pnl_bars": pnl_bars,
        },
    )


@router.get("/watchlist")
def watchlist(request: Request):
    quotes = quote_service.get_quotes([row["code"] for row in WATCHLIST])
    items = _build_watchlist(quotes)
    return render(
        request,
        "watchlist.html",
        {
            "page": "watchlist",
            "page_title": "自选股",
            "items": items,
            "count": len(items),
            "regime": MARKET_REGIME,
        },
    )


@router.get("/holdings")
def holdings(request: Request):
    quotes = quote_service.get_quotes([row["code"] for row in HOLDINGS])
    items = _build_holdings(quotes)
    totals = _totals(items)
    chart_items = [
        {
            "name": item["name"],
            "market_value": round(item["market_value"], 2),
            "pnl": round(item["pnl"], 2),
        }
        for item in items
    ]

    max_item = max(items, key=lambda item: item["market_value"]) if items else None
    liquor_value = sum(item["market_value"] for item in items if item["industry"] == "白酒")
    pingan = next((item for item in items if item["code"] == "601318"), None)
    concentration = {
        "max_name": max_item["name"] if max_item else "—",
        "max_pct": (max_item["market_value"] / totals["market_value"] * 100) if max_item and totals["market_value"] else 0,
        "liquor_pct": (liquor_value / totals["market_value"] * 100) if totals["market_value"] else 0,
        "pingan_stop_distance": pingan["stop_distance"] if pingan else 0,
    }

    return render(
        request,
        "holdings.html",
        {
            "page": "holdings",
            "page_title": "持仓",
            "items": items,
            "chart_items": chart_items,
            "totals": totals,
            "concentration": concentration,
            "regime": MARKET_REGIME,
        },
    )


@router.get("/stock/{code}")
def stock_detail(request: Request, code: str):
    try:
        code = normalize_code(code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="无效的股票代码") from exc

    detail = get_stock_detail(code)
    watch_row = next((row for row in WATCHLIST if row["code"] == code), None)
    holding_row = next((row for row in HOLDINGS if row["code"] == code), None)
    if detail is None and watch_row is None and holding_row is None:
        raise HTTPException(status_code=404, detail="暂未收录该股票的演示数据")

    name = (watch_row or holding_row or {}).get("name", code)
    base_price = (watch_row or holding_row or {}).get("fallback_price", 10.0)
    quote = quote_service.get_quote(code)
    snap = _snapshot(quote, base_price, (watch_row or holding_row or {}).get("fallback_pct", 0.0))

    plan = None
    if watch_row is not None:
        plan = {
            **watch_row,
            "rating_class": _rating_class(watch_row["rating"]),
            "stop_distance": (snap["price"] - watch_row["stop_loss"]) / snap["price"] * 100,
            "tp_distance": (watch_row["take_profit"] - snap["price"]) / snap["price"] * 100,
        }

    position = None
    if holding_row is not None:
        market_value = snap["price"] * holding_row["shares"]
        cost_value = holding_row["cost"] * holding_row["shares"]
        position = {
            **holding_row,
            "market_value": market_value,
            "pnl": market_value - cost_value,
            "pnl_pct": (snap["price"] / holding_row["cost"] - 1) * 100,
            "pnl_trend": _trend_class(market_value - cost_value),
        }

    chart = _chart_payload(code, snap["price"])

    return render(
        request,
        "stock_detail.html",
        {
            "page": "watchlist",
            "page_title": f"{name} {code}",
            "code": code,
            "name": name,
            "snap": snap,
            "detail": detail,
            "plan": plan,
            "position": position,
            "chart": chart,
            "regime": MARKET_REGIME,
        },
    )


@router.get("/candidates")
def candidates(request: Request):
    return render(
        request,
        "placeholder.html",
        {
            "page": "candidates",
            "page_title": "候选池",
            "icon": "🔍",
            "title": "候选池建设中",
            "description": "全主板规则初筛、技术面评分和 AI 分析将在下一阶段接入。当前可先查看自选股页面中的演示数据。",
        },
    )


@router.get("/reports")
def reports(request: Request):
    return render(
        request,
        "placeholder.html",
        {
            "page": "reports",
            "page_title": "复盘",
            "icon": "📊",
            "title": "复盘报告建设中",
            "description": "每日 18:00 的大盘、板块、自选、持仓和候选池复盘将在 AI 接入后生成。",
        },
    )


# ------------------------------------------------------------------ api
@router.get("/api/health")
def health():
    return {"status": "ok", "app": "TradeAI", "stage": "framework"}


@router.get("/api/quotes")
def api_quotes(codes: str):
    requested = [code.strip() for code in codes.split(",") if code.strip()]
    quotes = quote_service.get_quotes(requested)
    return {
        "requested": requested,
        "count": len(quotes),
        "quotes": {
            code: {
                "code": quote.code,
                "name": quote.name,
                "price": quote.price,
                "pct_chg": quote.pct_chg,
                "pre_close": quote.pre_close,
                "source": quote.source,
                "ts": quote.ts.isoformat() if quote.ts else None,
            }
            for code, quote in quotes.items()
        },
    }
