"""Web routes and view models.

Pages read portfolio data from the database through ``app.services.portfolio``;
live prices are overlaid from Tencent/Sina. Demo data is now only a seed source
(``app.db.seed``) and the source of the AI/analysis prose.
"""

from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from urllib.parse import quote as url_quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.base import check_database, get_db
from app.db.models import AlertEvent, AlertRule, DailyBar, ReviewReport, Stock
from app.demo_data import INDICES, MARKET_REGIME, get_stock_detail, make_kline
from app.providers.base import normalize_code
from app.services import alerts as alert_service
from app.services import monitor as monitor_service
from app.services.ai import usage_summary
from app.services.calendar import session_status, sync_calendar
from app.services.discovery import latest_candidates, latest_job, run_discovery
from app.services.notifier import condition_text
from app.services.plans import confirm_plan, generate_plans, plan_items, reject_plan
from app.services.portfolio import (
    PortfolioError,
    add_holding,
    add_watch,
    holding_items,
    remove_holding,
    remove_watch,
    update_holding,
    watch_items,
)
from app.services.quotes import QuoteService
from app.services.review import generate_review, recent_reports

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ------------------------------------------------------------------ filters
def _money(value) -> str:
    return "—" if value is None else f"{value:,.2f}"


def _pct(value) -> str:
    return "—" if value is None else f"{value:+.2f}%"


def _num(value) -> str:
    return "—" if value is None else f"{value:.2f}"


templates.env.filters["money"] = _money
templates.env.filters["price"] = _money
templates.env.filters["pct"] = _pct
templates.env.filters["num2"] = _num

quote_service = QuoteService()

router = APIRouter()


def render(request: Request, name: str, context: dict):
    return templates.TemplateResponse(request, name, context)


def _redirect(path: str, *, error: str | None = None, ok: str | None = None) -> RedirectResponse:
    params = []
    if error:
        params.append(f"error={url_quote(error)}")
    if ok:
        params.append(f"ok={url_quote(ok)}")
    url = f"{path}?{'&'.join(params)}" if params else path
    return RedirectResponse(url, status_code=303)


# ------------------------------------------------------------------ helpers
def _trend_class(value: float | None) -> str:
    if value is None or value == 0:
        return "flat"
    return "up" if value > 0 else "down"


def _rating_class(rating: str) -> str:
    return {"看多": "buy", "中性": "neutral", "观察": "watch"}.get(rating, "neutral")


ALERT_LEVELS = {"red": "warning", "green": "success", "blue": "info", "grey": "info"}
ALERT_STATUS = {
    "sent": "已发送",
    "failed": "发送失败",
    "pending": "待发送",
    "suppressed": "已抑制",
}


def _alert_items(db: Session, limit: int = 20) -> list[dict]:
    items: list[dict] = []
    for event in alert_service.recent_events(db, limit=limit):
        snapshot = event.condition_snapshot or {}
        meta = alert_service.rule_meta(snapshot.get("rule_type", ""))
        items.append(
            {
                "id": event.id,
                "time": event.trigger_ts.strftime("%H:%M:%S"),
                "date": event.trigger_ts.strftime("%Y-%m-%d"),
                "name": snapshot.get("name") or event.code,
                "code": event.code,
                "price": event.trigger_price,
                "rule": meta["label"],
                "message": condition_text(event),
                "status": ALERT_STATUS.get(event.status, event.status),
                "level": ALERT_LEVELS.get(meta["color"], "info"),
                "dedup_key": event.dedup_key,
            }
        )
    return items


def _alert_counts(items: list[dict]) -> dict[str, int]:
    return {
        "success": sum(1 for item in items if item["level"] == "success"),
        "warning": sum(1 for item in items if item["level"] == "warning"),
        "info": sum(1 for item in items if item["level"] == "info"),
    }


def _plan_dict(plan) -> dict:
    return {
        "id": plan.id,
        "plan_no": plan.plan_no,
        "code": plan.code,
        "name": plan.name,
        "status": plan.status,
        "source": plan.source,
        "trade_date": plan.trade_date.isoformat() if plan.trade_date else None,
        "rating": plan.rating,
        "buy_zone_low": plan.buy_zone_low,
        "buy_zone_high": plan.buy_zone_high,
        "stop_loss": plan.stop_loss,
        "take_profit_1": plan.take_profit_1,
        "take_profit_2": plan.take_profit_2,
        "position_pct": plan.position_pct,
        "confidence": plan.confidence,
        "risk_score": plan.risk_score,
        "holding_horizon": plan.holding_horizon,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
    }


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
        source = "fallback"
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


def _build_watchlist(rows: list[dict], quotes: dict) -> list[dict]:
    items: list[dict] = []
    for row in rows:
        snap = _snapshot(quotes.get(row["code"]), row["fallback_price"], row["fallback_pct"])
        price = snap["price"]
        buy_low = row["buy_low"]
        buy_high = row["buy_high"]
        stop_loss = row["stop_loss"]
        take_profit = row["take_profit"]

        has_plan = bool(row["has_plan"] and buy_low and buy_high)
        if not has_plan:
            zone_status = "未设置计划"
            zone_class = "out"
            distance_text = "等待生成交易计划"
            risk_reward = None
        else:
            if price > buy_high:
                zone_status = "高于建仓区"
                distance_text = f"距建仓区 +{(price - buy_high) / buy_high * 100:.2f}%"
            elif price < buy_low:
                zone_status = "低于建仓区"
                distance_text = f"低于建仓区 {(buy_low - price) / buy_low * 100:.2f}%"
            else:
                zone_status = "进入建仓区"
                distance_text = "已在建仓区内"
            zone_class = "in" if zone_status == "进入建仓区" else "out"
            risk_reward = (
                (take_profit - price) / (price - stop_loss)
                if take_profit and stop_loss and price > stop_loss
                else None
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
                "zone_class": zone_class,
                "distance_text": distance_text,
                "stop_distance": (
                    (price - stop_loss) / price * 100 if stop_loss and price else None
                ),
                "tp_distance": (
                    (take_profit - price) / price * 100 if take_profit and price else None
                ),
                "risk_reward": risk_reward,
                "has_plan": has_plan,
            }
        )
    return items


def _build_holdings(rows: list[dict], quotes: dict) -> list[dict]:
    items: list[dict] = []
    for row in rows:
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
        stop_distance = (price - stop_loss) / price * 100 if stop_loss and price else None
        tp1_distance = (tp1 - price) / price * 100 if tp1 and price else None
        progress = 0.0
        if tp1 and cost and tp1 > cost:
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


def _real_chart_payload(db: Session, code: str, limit: int = 120) -> dict | None:
    """K-line payload from cached daily bars; ``None`` when history is missing."""
    rows = list(
        db.scalars(
            select(DailyBar)
            .where(DailyBar.code == code)
            .order_by(DailyBar.trade_date.desc())
            .limit(limit)
        )
    )
    if len(rows) < 5:
        return None
    rows.reverse()
    closes = [float(row.close) for row in rows]

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
        "dates": [row.trade_date.isoformat() for row in rows],
        "kline": [
            [float(row.open), float(row.close), float(row.low), float(row.high)] for row in rows
        ],
        "volume": [float(row.volume) for row in rows],
        "ma5": moving_average(5),
        "ma20": moving_average(20),
        "base_price": closes[-1],
    }


# ------------------------------------------------------------------ pages
@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    watch_rows = watch_items(db)
    holding_rows = holding_items(db)
    codes = {row["code"] for row in watch_rows} | {row["code"] for row in holding_rows}
    quotes = quote_service.get_quotes(codes) if codes else {}
    watchlist = _build_watchlist(watch_rows, quotes)
    holdings = _build_holdings(holding_rows, quotes)
    totals = _totals(holdings)
    indices = _build_indices()

    allocation = [
        {"name": item["name"], "value": round(item["market_value"], 2)} for item in holdings
    ]
    pnl_bars = [{"name": item["name"], "value": round(item["pnl"], 2)} for item in holdings]
    ai_count = sum(1 for item in watchlist if item["source"] == "AI 候选")
    alert_items = _alert_items(db, limit=10)
    alert_counts = _alert_counts(alert_items)
    today_start = datetime.combine(date.today(), time.min)
    alert_total = (
        db.scalar(
            select(func.count())
            .select_from(AlertEvent)
            .where(AlertEvent.trigger_ts >= today_start)
        )
        or 0
    )

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
            "alerts": alert_items,
            "alert_counts": alert_counts,
            "alert_total": alert_total,
            "allocation": allocation,
            "pnl_bars": pnl_bars,
        },
    )


@router.get("/watchlist")
def watchlist(request: Request, db: Session = Depends(get_db)):
    rows = watch_items(db)
    quotes = quote_service.get_quotes([row["code"] for row in rows]) if rows else {}
    items = _build_watchlist(rows, quotes)
    in_zone = sum(1 for item in items if item["zone_status"] == "进入建仓区")
    ai_count = sum(1 for item in items if item["source"] == "AI 候选")
    return render(
        request,
        "watchlist.html",
        {
            "page": "watchlist",
            "page_title": "自选股",
            "items": items,
            "count": len(items),
            "in_zone": in_zone,
            "ai_count": ai_count,
            "error": request.query_params.get("error"),
            "notice": request.query_params.get("ok"),
            "regime": MARKET_REGIME,
        },
    )


@router.post("/watchlist/add")
def watchlist_add(
    code: str = Form(...),
    note: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    try:
        row = add_watch(db, code, source="manual", note=note or None)
    except PortfolioError as exc:
        return _redirect("/watchlist", error=str(exc))
    return _redirect("/watchlist", ok=f"已添加 {row.name}（{row.code}）")


@router.post("/watchlist/{watch_id}/delete")
def watchlist_delete(watch_id: int, db: Session = Depends(get_db)):
    try:
        remove_watch(db, watch_id)
    except PortfolioError as exc:
        return _redirect("/watchlist", error=str(exc))
    return _redirect("/watchlist", ok="已移除自选")


@router.get("/holdings")
def holdings(request: Request, db: Session = Depends(get_db)):
    rows = holding_items(db)
    quotes = quote_service.get_quotes([row["code"] for row in rows]) if rows else {}
    items = _build_holdings(rows, quotes)
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
    max_industry = max_item["industry"] if max_item else None
    industry_value = (
        sum(item["market_value"] for item in items if item["industry"] == max_industry)
        if max_industry
        else 0
    )
    concentration = {
        "max_name": max_item["name"] if max_item else "—",
        "max_pct": (
            (max_item["market_value"] / totals["market_value"] * 100)
            if max_item and totals["market_value"]
            else 0
        ),
        "industry_name": max_industry or "—",
        "industry_pct": (
            (industry_value / totals["market_value"] * 100) if totals["market_value"] else 0
        ),
        "closest_stop": min(
            (item for item in items if item["stop_distance"] is not None),
            key=lambda item: item["stop_distance"],
            default=None,
        ),
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
            "error": request.query_params.get("error"),
            "notice": request.query_params.get("ok"),
            "today": date.today().isoformat(),
            "regime": MARKET_REGIME,
        },
    )


@router.post("/holdings/add")
def holdings_add(
    code: str = Form(...),
    cost_price: float = Form(...),
    shares: int = Form(...),
    stop_loss: float = Form(...),
    take_profit_1: str | None = Form(default=None),
    take_profit_2: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    try:
        row = add_holding(
            db,
            code=code,
            cost_price=cost_price,
            shares=shares,
            stop_loss=stop_loss,
            take_profit_1=float(take_profit_1) if take_profit_1 else None,
            take_profit_2=float(take_profit_2) if take_profit_2 else None,
        )
    except (PortfolioError, ValueError) as exc:
        return _redirect("/holdings", error=str(exc))
    return _redirect("/holdings", ok=f"已记录持仓 {row.name}（{row.code}）")


@router.post("/holdings/{holding_id}/stop")
def holdings_update_stop(
    holding_id: int,
    current_stop: float = Form(...),
    db: Session = Depends(get_db),
):
    try:
        update_holding(db, holding_id, current_stop=current_stop)
    except PortfolioError as exc:
        return _redirect("/holdings", error=str(exc))
    return _redirect("/holdings", ok="止损价已更新")


@router.post("/holdings/{holding_id}/delete")
def holdings_delete(holding_id: int, db: Session = Depends(get_db)):
    try:
        remove_holding(db, holding_id)
    except PortfolioError as exc:
        return _redirect("/holdings", error=str(exc))
    return _redirect("/holdings", ok="持仓已删除")


@router.get("/stock/{code}")
def stock_detail(request: Request, code: str, db: Session = Depends(get_db)):
    try:
        code = normalize_code(code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="无效的股票代码") from exc

    detail = get_stock_detail(code)
    watch_row = next((row for row in watch_items(db) if row["code"] == code), None)
    holding_row = next((row for row in holding_items(db) if row["code"] == code), None)
    stock = db.get(Stock, code)
    if detail is None and watch_row is None and holding_row is None and stock is None:
        raise HTTPException(status_code=404, detail="暂无该股票数据")

    name = (
        (watch_row or holding_row or {}).get("name")
        or (stock.name if stock else None)
        or code
    )
    base_price = (
        (watch_row or holding_row or {}).get("fallback_price")
        or (watch_row or {}).get("buy_high")
        or 10.0
    )
    quote = quote_service.get_quote(code)
    snap = _snapshot(quote, base_price, 0.0)

    plan = None
    if watch_row is not None and watch_row["has_plan"]:
        plan = {
            **watch_row,
            "rating_class": _rating_class(watch_row["rating"]),
            "stop_distance": (
                (snap["price"] - watch_row["stop_loss"]) / snap["price"] * 100
                if watch_row["stop_loss"]
                else None
            ),
            "tp_distance": (
                (watch_row["take_profit"] - snap["price"]) / snap["price"] * 100
                if watch_row["take_profit"]
                else None
            ),
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

    chart = _real_chart_payload(db, code)
    chart_real = chart is not None
    if chart is None:
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
            "industry": stock.industry if stock else None,
            "chart": chart,
            "chart_real": chart_real,
            "regime": MARKET_REGIME,
        },
    )


@router.get("/candidates")
def candidates(request: Request, db: Session = Depends(get_db)):
    trade_date, rows = latest_candidates(db, limit=100)
    job = latest_job(db)
    stats = (job.stats if job and job.stats else {}) or {}

    quotes = (
        quote_service.get_quotes([row.code for row in rows]) if rows else {}
    )

    items: list[dict] = []
    for row in rows:
        snapshot = row.indicator_snapshot or {}
        quote = quotes.get(row.code)
        price = quote.price if quote else snapshot.get("price_at_discovery")
        pct_chg = quote.pct_chg if quote else snapshot.get("pct_chg_at_discovery")
        breakdown = snapshot.get("score_breakdown") or {}
        items.append(
            {
                "rank": row.rank,
                "code": row.code,
                "name": row.name,
                "industry": snapshot.get("industry") or "—",
                "score": row.score,
                "price": price,
                "pct_chg": pct_chg or 0.0,
                "trend": _trend_class(pct_chg),
                "breakdown": breakdown,
                "snapshot": snapshot,
                "rule_hits": (row.rule_hits or [])[:6],
            }
        )

    return render(
        request,
        "candidates.html",
        {
            "page": "candidates",
            "page_title": "候选池",
            "trade_date": trade_date,
            "items": items,
            "count": len(items),
            "stats": stats,
            "stages": stats.get("stages") or [],
            "job": job,
            "error": request.query_params.get("error"),
            "notice": request.query_params.get("ok"),
            "has_key": bool(settings.deepseek_api_key),
            "regime": MARKET_REGIME,
        },
    )


@router.post("/candidates/run")
def candidates_run(db: Session = Depends(get_db)):
    try:
        output = run_discovery(db)
    except Exception as exc:  # noqa: BLE001 - surface a readable message in the UI
        return _redirect("/candidates", error=f"发现失败：{exc}")
    summary = output["summary"]
    return _redirect(
        "/candidates",
        ok=(
            f"发现完成：{summary['codes']} 只股票 → {len(output['candidates'])} 只候选，"
            f"用时 {summary['total_sec']} 秒"
        ),
    )


@router.get("/plans")
def plans_page(request: Request, db: Session = Depends(get_db)):
    status = request.query_params.get("status") or None
    items = plan_items(db, status=status, limit=100)
    usage = usage_summary(db)
    latest_date, candidates = latest_candidates(db, limit=50)
    return render(
        request,
        "plans.html",
        {
            "page": "plans",
            "page_title": "交易计划",
            "items": items,
            "count": len(items),
            "status_filter": status or "all",
            "usage": usage,
            "candidate_count": len(candidates),
            "candidate_date": latest_date,
            "has_key": bool(settings.deepseek_api_key),
            "error": request.query_params.get("error"),
            "notice": request.query_params.get("ok"),
            "regime": MARKET_REGIME,
        },
    )


@router.post("/plans/generate")
def plans_generate(
    limit: int = Form(default=5),
    force: bool = Form(default=False),
    db: Session = Depends(get_db),
):
    from app.services.ai import AIError

    try:
        result = generate_plans(db, limit=max(1, min(limit, 10)), force=force)
    except AIError as exc:
        return _redirect("/plans", error=str(exc))
    message = result["message"]
    if result["failed"]:
        message += f"；首个错误：{result['failed'][0]['error']}"
        return _redirect("/plans", error=message)
    return _redirect("/plans", ok=message)


@router.post("/plans/{plan_id}/confirm")
def plans_confirm(plan_id: int, db: Session = Depends(get_db)):
    from app.services.ai import AIError

    try:
        plan = confirm_plan(db, plan_id)
    except AIError as exc:
        return _redirect("/plans", error=str(exc))
    return _redirect("/plans", ok=f"已确认 {plan.name} 的计划，并加入自选")


@router.post("/plans/{plan_id}/reject")
def plans_reject(plan_id: int, db: Session = Depends(get_db)):
    from app.services.ai import AIError

    try:
        plan = reject_plan(db, plan_id)
    except AIError as exc:
        return _redirect("/plans", error=str(exc))
    return _redirect("/plans", ok=f"已拒绝 {plan.name} 的计划")


@router.get("/reports")
def reports(request: Request, db: Session = Depends(get_db)):
    reports_list = recent_reports(db, limit=60)
    return render(
        request,
        "reports.html",
        {
            "page": "reports",
            "page_title": "复盘",
            "reports": reports_list,
            "count": len(reports_list),
            "error": request.query_params.get("error"),
            "notice": request.query_params.get("ok"),
            "regime": MARKET_REGIME,
        },
    )


@router.post("/reports/generate")
def reports_generate(
    trade_date: str = Form(default=""),
    use_ai: bool = Form(default=True),
    force: bool = Form(default=False),
    db: Session = Depends(get_db),
):
    from app.services.ai import AIError

    target = date.fromisoformat(trade_date) if trade_date else date.today()
    try:
        generate_review(db, trade_date=target, use_ai=use_ai, force=force)
    except AIError as exc:
        return _redirect("/reports", error=str(exc))
    return _redirect("/reports", ok=f"{target} 复盘已生成")


@router.get("/reports/{trade_date}")
def report_detail(request: Request, trade_date: str, db: Session = Depends(get_db)):
    try:
        target = date.fromisoformat(trade_date)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="日期格式应为 YYYY-MM-DD") from exc
    report = db.scalar(select(ReviewReport).where(ReviewReport.trade_date == target))
    if report is None:
        raise HTTPException(status_code=404, detail="暂无该日复盘")
    raw = report.raw or {}
    return render(
        request,
        "report_detail.html",
        {
            "page": "reports",
            "page_title": f"复盘 {target}",
            "report": report,
            "data": raw.get("data") or {},
            "ai_error": raw.get("ai_error"),
            "usage": raw.get("usage"),
            "regime": MARKET_REGIME,
        },
    )


@router.get("/alerts")
def alerts_page(request: Request, db: Session = Depends(get_db)):
    items = _alert_items(db, limit=100)
    rule_count = db.scalar(select(func.count()).select_from(AlertRule)) or 0
    status = session_status(db)
    return render(
        request,
        "alerts.html",
        {
            "page": "alerts",
            "page_title": "预警",
            "items": items,
            "count": len(items),
            "rule_count": rule_count,
            "alert_counts": _alert_counts(items),
            "status": status,
            "monitor": monitor_service.monitor_status(),
            "regime": MARKET_REGIME,
        },
    )


# ------------------------------------------------------------------ api
@router.get("/api/health")
def health(db: Session = Depends(get_db)):
    database = check_database()
    calendar = session_status(db)
    monitor = monitor_service.monitor_status()
    healthy = database["ok"] and (monitor["ok"] is not False)
    return {
        "status": "ok" if healthy else "degraded",
        "app": settings.app_name,
        "stage": "monitor",
        "database": {
            "ok": database["ok"],
            "dialect": database["dialect"],
            "path": database["path"],
        },
        "calendar": calendar,
        "monitor": monitor,
    }


@router.get("/api/calendar")
def api_calendar(db: Session = Depends(get_db)):
    return session_status(db)


@router.post("/api/calendar/sync")
def api_calendar_sync(db: Session = Depends(get_db)):
    from app.providers.akshare_provider import AkShareProvider

    inserted = sync_calendar(db, provider=AkShareProvider())
    return {"inserted": inserted, **session_status(db)}


@router.get("/api/alerts")
def api_alerts(limit: int = 50, db: Session = Depends(get_db)):
    items = _alert_items(db, limit=max(1, min(limit, 500)))
    return {"count": len(items), "items": items}


@router.post("/api/monitor/tick")
def api_monitor_tick(
    force: bool = True,
    dry_run: bool = False,
    db: Session = Depends(get_db),
):
    """Manual tick for debugging. ``force`` ignores the trading-session window."""
    return monitor_service.run_once(db, force=force, dry_run=dry_run)


@router.post("/api/monitor/retry")
def api_monitor_retry(db: Session = Depends(get_db)):
    from app.services.notifier import Notifier

    return Notifier().notify_pending(db)


@router.get("/api/ai/usage")
def api_ai_usage(db: Session = Depends(get_db)):
    return usage_summary(db)


@router.post("/api/ai/plans/generate")
def api_ai_plans_generate(
    limit: int = 5,
    force: bool = False,
    db: Session = Depends(get_db),
):
    from app.services.ai import AIError

    try:
        result = generate_plans(db, limit=max(1, min(limit, 20)), force=force)
    except AIError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return {
        "message": result["message"],
        "created": result["created"],
        "cached": result["cached"],
        "failed": result["failed"],
        "plans": [_plan_dict(plan) for plan in result["plans"]],
    }


@router.post("/api/ai/review")
def api_ai_review(
    trade_date: str = "",
    use_ai: bool = True,
    force: bool = False,
    db: Session = Depends(get_db),
):
    from app.services.ai import AIError

    target = date.fromisoformat(trade_date) if trade_date else date.today()
    try:
        report = generate_review(db, trade_date=target, use_ai=use_ai, force=force)
    except AIError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return {
        "trade_date": report.trade_date.isoformat(),
        "market_summary": report.market_summary,
        "watch_summary": report.watch_summary,
        "holding_summary": report.holding_summary,
        "candidates_summary": report.candidates_summary,
        "ai_narrative": report.ai_narrative,
    }


@router.get("/api/plans")
def api_plans(status: str | None = None, limit: int = 100, db: Session = Depends(get_db)):
    items = plan_items(db, status=status, limit=max(1, min(limit, 200)))
    return {"count": len(items), "items": items}


@router.post("/api/plans/{plan_id}/confirm")
def api_plan_confirm(plan_id: int, db: Session = Depends(get_db)):
    from app.services.ai import AIError

    try:
        plan = confirm_plan(db, plan_id)
    except AIError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _plan_dict(plan)


@router.get("/api/discovery/latest")
def api_discovery_latest(limit: int = 50, db: Session = Depends(get_db)):
    trade_date, rows = latest_candidates(db, limit=max(1, min(limit, 200)))
    job = latest_job(db)
    return {
        "trade_date": trade_date.isoformat() if trade_date else None,
        "stats": (job.stats if job and job.stats else {}) or {},
        "count": len(rows),
        "items": [
            {
                "rank": row.rank,
                "code": row.code,
                "name": row.name,
                "score": row.score,
                "rule_hits": row.rule_hits,
                "snapshot": row.indicator_snapshot,
            }
            for row in rows
        ],
    }


@router.post("/api/discovery/run")
def api_discovery_run(
    limit: int | None = None,
    persist: bool = True,
    db: Session = Depends(get_db),
):
    return run_discovery(db, persist=persist, limit=limit)


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
