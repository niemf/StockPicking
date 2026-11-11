"""Alert rules and the persisted trigger state machine.

Rules are generated from real positions and confirmed plans, then evaluated on
every polling tick. The state lives in ``alert_rule.params["state"]`` so a
restart does not lose the confirm counter, cooldown or rearm status.

State machine (spec 7.10.4)::

    IDLE --(condition true N times)--> TRIGGERED --> COOLDOWN
      ^                                                |
      +----------(condition recovered + cooldown)-------+
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import AlertEvent, AlertRule, Holding, TradePlan, Watchlist
from app.providers.base import Quote

# --------------------------------------------------------------------- metadata
RULE_META: dict[str, dict[str, Any]] = {
    "STOP_LOSS": {"label": "止损", "priority": 10, "color": "red"},
    "TRAILING_STOP": {"label": "移动止盈", "priority": 15, "color": "red"},
    "TAKE_PROFIT_1": {"label": "止盈一", "priority": 20, "color": "green"},
    "TAKE_PROFIT_2": {"label": "止盈二", "priority": 20, "color": "green"},
    "PRICE_IN_BUY_ZONE": {"label": "进入建仓区", "priority": 50, "color": "blue"},
    "PRICE_CHANGE": {"label": "异动", "priority": 80, "color": "blue"},
    "COMPOSITE": {"label": "自定义条件", "priority": 60, "color": "blue"},
}

# Metrics available to user-defined composite rules. Values are read straight
# from the real-time quote so evaluation stays cheap and AI-free.
RULE_METRICS: dict[str, dict[str, Any]] = {
    "price": {"label": "价格", "unit": "元"},
    "pct_chg": {"label": "涨跌幅", "unit": "%"},
    "amount_yi": {"label": "成交额", "unit": "亿"},
    "turnover_rate": {"label": "换手率", "unit": "%"},
}

RULE_OPS: dict[str, str] = {">=": "≥", "<=": "≤", ">": ">", "<": "<"}

RULE_CHANNELS: dict[str, str] = {"feishu": "飞书", "email": "邮件", "both": "飞书 + 邮件"}

METRIC_ALIASES = {"amount": "amount_yi", "turnover": "turnover_rate"}

STOP_LIKE = {"STOP_LOSS", "TRAILING_STOP"}
TARGET_LIKE = {"TAKE_PROFIT_1", "TAKE_PROFIT_2"}

MIN_PRICE = 0.01
MAX_ABS_PCT_CHG = 30.0


def rule_meta(rule_type: str) -> dict[str, Any]:
    return RULE_META.get(rule_type, {"label": rule_type, "priority": 100, "color": "grey"})


# --------------------------------------------------------------------- rule spec
def _spec(
    rule_type: str,
    params: dict[str, Any],
    *,
    confirm_samples: int,
    cooldown_sec: int,
    rearm_pct: float,
) -> dict[str, Any]:
    meta = rule_meta(rule_type)
    return {
        "rule_type": rule_type,
        "params": params,
        "priority": meta["priority"],
        "confirm_samples": max(1, confirm_samples),
        "cooldown_sec": max(0, cooldown_sec),
        "rearm_threshold": rearm_pct / 100.0,
        "auto": True,
    }


def _holding_specs(holding: Holding, plan: TradePlan | None) -> list[dict[str, Any]]:
    stop = holding.current_stop or (plan.stop_loss if plan else None)
    specs: list[dict[str, Any]] = []

    if stop:
        specs.append(
            _spec(
                "STOP_LOSS",
                {"stop": float(stop)},
                confirm_samples=settings.alert_stop_confirm_samples,
                cooldown_sec=settings.alert_cooldown_seconds,
                rearm_pct=settings.alert_rearm_pct,
            )
        )
        # A trailing stop that sits below the hard stop adds nothing but noise:
        # only create it once the position has actually run up.
        highest = holding.trailing_high or holding.cost_price
        trailing_level = float(highest) * (1 - settings.alert_trailing_drawdown_pct / 100)
        if trailing_level > float(stop) * 1.001:
            specs.append(
                _spec(
                    "TRAILING_STOP",
                    {
                        "stop": float(stop),
                        "drawdown_pct": float(settings.alert_trailing_drawdown_pct),
                    },
                    confirm_samples=settings.alert_confirm_samples,
                    cooldown_sec=settings.alert_cooldown_seconds,
                    rearm_pct=settings.alert_rearm_pct,
                )
            )

    if plan is not None:
        if plan.take_profit_1:
            specs.append(
                _spec(
                    "TAKE_PROFIT_1",
                    {"take_profit": float(plan.take_profit_1)},
                    confirm_samples=settings.alert_confirm_samples,
                    cooldown_sec=settings.alert_cooldown_seconds,
                    rearm_pct=settings.alert_rearm_pct,
                )
            )
        if plan.take_profit_2:
            specs.append(
                _spec(
                    "TAKE_PROFIT_2",
                    {"take_profit": float(plan.take_profit_2)},
                    confirm_samples=settings.alert_confirm_samples,
                    cooldown_sec=settings.alert_cooldown_seconds,
                    rearm_pct=settings.alert_rearm_pct,
                )
            )
    return specs


def _watch_specs(watch: Watchlist, plan: TradePlan | None) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    if plan is not None and plan.buy_zone_low and plan.buy_zone_high:
        specs.append(
            _spec(
                "PRICE_IN_BUY_ZONE",
                {"buy_low": float(plan.buy_zone_low), "buy_high": float(plan.buy_zone_high)},
                confirm_samples=settings.alert_confirm_samples,
                cooldown_sec=settings.alert_cooldown_seconds,
                rearm_pct=settings.alert_rearm_pct,
            )
        )
    specs.append(
        _spec(
            "PRICE_CHANGE",
            {"threshold_pct": float(settings.alert_price_change_pct)},
            confirm_samples=settings.alert_confirm_samples,
            cooldown_sec=settings.alert_cooldown_seconds,
            rearm_pct=settings.alert_rearm_pct,
        )
    )
    return specs


# --------------------------------------------------------------------- sync rules
def ensure_rules(session: Session) -> dict[str, int]:
    """Create/refresh rules for open holdings and active watches.

    Thresholds are refreshed when a stop or take-profit changes, but the
    ``state`` block is preserved so confirm counters and cooldown survive edits.
    Orphaned rules (closed holding / removed watch) are deleted.
    """
    holdings = list(session.scalars(select(Holding).where(Holding.status == "open")))
    watches = list(session.scalars(select(Watchlist).where(Watchlist.status == "active")))

    plan_ids = {row.plan_id for row in holdings + watches if row.plan_id}
    plans = {
        plan.id: plan
        for plan in session.scalars(select(TradePlan).where(TradePlan.id.in_(plan_ids)))
    } if plan_ids else {}

    desired: dict[tuple[str, int, str], dict[str, Any]] = {}
    for holding in holdings:
        for spec in _holding_specs(holding, plans.get(holding.plan_id)):
            desired[("holding", holding.id, spec["rule_type"])] = spec
    for watch in watches:
        for spec in _watch_specs(watch, plans.get(watch.plan_id)):
            desired[("watch", watch.id, spec["rule_type"])] = spec

    existing = {
        (rule.target_type, rule.target_id, rule.rule_type): rule
        for rule in session.scalars(select(AlertRule))
    }

    created = updated = 0
    for key, spec in desired.items():
        rule = existing.get(key)
        if rule is None:
            session.add(
                AlertRule(
                    target_type=key[0],
                    target_id=key[1],
                    rule_type=key[2],
                    params={**spec["params"], "auto": True},
                    enabled=True,
                    priority=spec["priority"],
                    confirm_samples=spec["confirm_samples"],
                    cooldown_sec=spec["cooldown_sec"],
                    rearm_threshold=spec["rearm_threshold"],
                )
            )
            created += 1
            continue

        state = (rule.params or {}).get("state")
        merged = {**spec["params"], "auto": True}
        if state is not None:
            merged["state"] = state
        if merged != (rule.params or {}):
            rule.params = merged
            updated += 1
        rule.priority = spec["priority"]
        rule.confirm_samples = spec["confirm_samples"]
        rule.cooldown_sec = spec["cooldown_sec"]
        rule.rearm_threshold = spec["rearm_threshold"]

    orphans = [
        rule
        for key, rule in existing.items()
        if key not in desired and bool((rule.params or {}).get("auto"))
    ]
    for rule in orphans:
        session.delete(rule)

    session.commit()
    summary = {"created": created, "updated": updated, "deleted": len(orphans), "total": len(desired)}
    logger.debug("alert rules synced: {}", summary)
    return summary


# --------------------------------------------------------------------- evaluation
def _state(rule: AlertRule) -> dict[str, Any]:
    return {
        "consecutive": 0,
        "armed": True,
        "last_trigger": None,
        **(rule.params or {}).get("state", {}),
    }


def _save_state(rule: AlertRule, state: dict[str, Any]) -> None:
    base = {key: value for key, value in (rule.params or {}).items() if key != "state"}
    base["state"] = state
    rule.params = base


def _trailing_level(rule: AlertRule, holding: Holding) -> float | None:
    params = rule.params or {}
    stop = float(params.get("stop") or holding.current_stop or 0)
    highest = holding.trailing_high or holding.cost_price
    drawdown = float(params.get("drawdown_pct") or settings.alert_trailing_drawdown_pct) / 100
    trailing = float(highest) * (1 - drawdown)
    level = max(stop, trailing)
    return level if level > 0 else None


def _metric_value(metric: str, quote: Quote) -> float | None:
    metric = METRIC_ALIASES.get(metric, metric)
    if metric == "price":
        return quote.price
    if metric == "pct_chg":
        return quote.pct_chg
    if metric == "amount_yi":
        return quote.amount / 1e8
    if metric == "turnover_rate":
        return quote.turnover_rate
    return None


def _compare(left: float, op: str, right: float) -> bool:
    if op == ">=":
        return left >= right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == "<":
        return left < right
    return False


def eval_composite(params: dict[str, Any], quote: Quote) -> tuple[bool, list[dict[str, Any]]]:
    """Evaluate the user-defined condition list; returns (triggered, detail)."""
    conditions = params.get("conditions") or []
    detail: list[dict[str, Any]] = []
    for item in conditions:
        try:
            metric = str(item.get("metric") or "")
            op = str(item.get("op") or ">=")
            value = float(item.get("value"))
        except (TypeError, ValueError):
            continue
        actual = _metric_value(metric, quote)
        ok = actual is not None and _compare(actual, op, value)
        detail.append(
            {
                "metric": metric,
                "metric_label": RULE_METRICS.get(metric, {}).get("label", metric),
                "op": op,
                "value": value,
                "actual": round(actual, 3) if actual is not None else None,
                "ok": ok,
            }
        )
    if not detail:
        return False, detail
    if str(params.get("logic") or "AND").upper() == "OR":
        return any(item["ok"] for item in detail), detail
    return all(item["ok"] for item in detail), detail


def _condition(rule: AlertRule, quote: Quote, holding: Holding | None) -> tuple[bool, float | None]:
    """Return ``(is_triggered, threshold)``; threshold is stored for the card."""
    params = rule.params or {}
    price = quote.price

    if rule.rule_type == "COMPOSITE":
        triggered, _ = eval_composite(params, quote)
        return triggered, None

    if rule.rule_type == "STOP_LOSS":
        stop = float(params.get("stop") or 0)
        return price <= stop, stop or None

    if rule.rule_type == "TRAILING_STOP":
        level = _trailing_level(rule, holding) if holding else None
        if level is None:
            return False, None
        return price <= level, level

    if rule.rule_type in TARGET_LIKE:
        target = float(params.get("take_profit") or 0)
        return price >= target, target or None

    if rule.rule_type == "PRICE_IN_BUY_ZONE":
        low = float(params.get("buy_low") or 0)
        high = float(params.get("buy_high") or 0)
        return (low <= price <= high), high or None

    if rule.rule_type == "PRICE_CHANGE":
        threshold = float(params.get("threshold_pct") or 0)
        return abs(quote.pct_chg) >= threshold, threshold

    return False, None


def _recovered(rule: AlertRule, quote: Quote) -> bool:
    """Whether price has moved far enough back for the rule to re-arm."""
    buffer = max(0.0, float(rule.rearm_threshold or 0))
    price = quote.price
    params = rule.params or {}

    if rule.rule_type in STOP_LIKE:
        level = float(params.get("stop") or 0)
        if rule.rule_type == "TRAILING_STOP":
            # level changes with the trailing high; the stop is a safe proxy.
            level = float(params.get("stop") or 0)
        return level > 0 and price >= level * (1 + buffer)

    if rule.rule_type in TARGET_LIKE:
        target = float(params.get("take_profit") or 0)
        return target > 0 and price <= target * (1 - buffer)

    if rule.rule_type == "PRICE_IN_BUY_ZONE":
        high = float(params.get("buy_high") or 0)
        low = float(params.get("buy_low") or 0)
        return price > high * (1 + buffer) or price < low * (1 - buffer)

    if rule.rule_type == "PRICE_CHANGE":
        threshold = float(params.get("threshold_pct") or 0)
        return abs(quote.pct_chg) < max(0.0, threshold - buffer * 100)

    if rule.rule_type == "COMPOSITE":
        # Composite rules re-arm as soon as the condition set is false.
        return True

    return False


def _next_dedup_key(session: Session, code: str, rule_type: str, trade_date: date) -> str:
    prefix = f"{code}:{rule_type}:{trade_date.isoformat()}"
    used = session.scalar(
        select(AlertEvent.dedup_key)
        .where(AlertEvent.dedup_key.like(f"{prefix}:%"))
        .order_by(AlertEvent.dedup_key.desc())
        .limit(1)
    )
    sequence = 1
    if used:
        try:
            sequence = int(used.rsplit(":", 1)[1]) + 1
        except (IndexError, ValueError):
            sequence = 1
    return f"{prefix}:{sequence:03d}"


def _snapshot(
    rule: AlertRule,
    quote: Quote,
    threshold: float | None,
    holding: Holding | None,
    plan: TradePlan | None,
    watch: Watchlist | None,
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "rule_type": rule.rule_type,
        "rule_label": (rule.params or {}).get("label") or rule_meta(rule.rule_type)["label"],
        "name": quote.name,
        "price": quote.price,
        "pct_chg": quote.pct_chg,
        "pre_close": quote.pre_close,
        "source": quote.source,
        "threshold": threshold,
        "channel": (rule.params or {}).get("channel") or "feishu",
        "params": {k: v for k, v in (rule.params or {}).items() if k != "state"},
    }
    if rule.rule_type == "COMPOSITE":
        _, detail = eval_composite(rule.params or {}, quote)
        snapshot["conditions"] = detail
        snapshot["logic"] = (rule.params or {}).get("logic") or "AND"
    if holding is not None:
        pnl_pct = ((quote.price / holding.cost_price) - 1) * 100 if holding.cost_price else 0.0
        snapshot["holding"] = {
            "id": holding.id,
            "cost_price": holding.cost_price,
            "shares": holding.shares,
            "available_shares": holding.available_shares,
            "pnl_pct": round(pnl_pct, 2),
            "current_stop": holding.current_stop,
        }
    if plan is not None:
        snapshot["plan"] = {
            "plan_no": plan.plan_no,
            "trade_date": plan.trade_date.isoformat() if plan.trade_date else None,
            "stop_loss": plan.stop_loss,
            "take_profit_1": plan.take_profit_1,
            "take_profit_2": plan.take_profit_2,
        }
    if watch is not None:
        snapshot["watch"] = {"id": watch.id, "note": watch.note}
    return snapshot


def evaluate(
    session: Session,
    quotes: dict[str, Quote],
    *,
    moment: datetime | None = None,
) -> list[AlertEvent]:
    """Evaluate every enabled rule once and return newly triggered events."""
    moment = moment or datetime.now()
    trade_date = moment.date()

    rules = list(
        session.scalars(
            select(AlertRule)
            .where(AlertRule.enabled.is_(True))
            .order_by(AlertRule.priority, AlertRule.id)
        )
    )
    if not rules:
        return []

    holdings = {row.id: row for row in session.scalars(select(Holding))}
    watches = {row.id: row for row in session.scalars(select(Watchlist))}
    plan_ids = {
        target.plan_id
        for target in list(holdings.values()) + list(watches.values())
        if target.plan_id
    }
    plans = {
        plan.id: plan
        for plan in session.scalars(select(TradePlan).where(TradePlan.id.in_(plan_ids)))
    } if plan_ids else {}

    events: list[AlertEvent] = []
    for rule in rules:
        target = holdings.get(rule.target_id) if rule.target_type == "holding" else watches.get(rule.target_id)
        if target is None:
            continue

        quote = quotes.get(target.code)
        state = _state(rule)

        if quote is None:
            state["consecutive"] = 0
            _save_state(rule, state)
            continue

        # Data guard: a zero price or an impossible change is a bad sample.
        if quote.price < MIN_PRICE or quote.pre_close <= 0 or abs(quote.pct_chg) > MAX_ABS_PCT_CHG:
            logger.warning(
                "discard abnormal quote {} price={} pct={}", quote.code, quote.price, quote.pct_chg
            )
            state["consecutive"] = 0
            _save_state(rule, state)
            continue

        triggered, threshold = _condition(rule, quote, target if rule.target_type == "holding" else None)

        if rule.rule_type == "COMPOSITE":
            params = rule.params or {}
            expires_on = params.get("expires_on")
            if expires_on:
                try:
                    if date.fromisoformat(str(expires_on)) < trade_date:
                        continue
                except ValueError:
                    pass

        if not triggered:
            state["consecutive"] = 0
            if not state["armed"] and _recovered(rule, quote):
                state["armed"] = True
            _save_state(rule, state)
            continue

        state["consecutive"] = int(state.get("consecutive", 0)) + 1
        if not state["armed"] or state["consecutive"] < rule.confirm_samples:
            _save_state(rule, state)
            continue

        last_trigger = state.get("last_trigger")
        if last_trigger:
            try:
                elapsed = (moment - datetime.fromisoformat(last_trigger)).total_seconds()
            except ValueError:
                elapsed = float("inf")
            if elapsed < rule.cooldown_sec:
                _save_state(rule, state)
                continue

        daily_cap = int((rule.params or {}).get("max_per_day") or 0)
        if daily_cap > 0:
            prefix = f"{target.code}:{rule.rule_type}:{trade_date.isoformat()}:%"
            used = session.scalar(
                select(func.count())
                .select_from(AlertEvent)
                .where(AlertEvent.dedup_key.like(prefix))
            )
            if int(used or 0) >= daily_cap:
                state["armed"] = False
                _save_state(rule, state)
                continue

        plan = plans.get(target.plan_id) if target.plan_id else None
        holding = target if rule.target_type == "holding" else None
        watch = target if rule.target_type == "watch" else None

        event = AlertEvent(
            rule_id=rule.id,
            code=target.code,
            trigger_ts=moment,
            trigger_price=quote.price,
            condition_snapshot=_snapshot(rule, quote, threshold, holding, plan, watch),
            status="pending",
            dedup_key=_next_dedup_key(session, target.code, rule.rule_type, trade_date),
        )
        session.add(event)
        state.update(
            {
                "consecutive": 0,
                "armed": False,
                "last_trigger": moment.isoformat(timespec="seconds"),
            }
        )
        _save_state(rule, state)
        events.append(event)

    session.flush()
    session.commit()
    return events


# --------------------------------------------------------------------- queries
def recent_events(session: Session, limit: int = 50) -> list[AlertEvent]:
    return list(
        session.scalars(select(AlertEvent).order_by(AlertEvent.trigger_ts.desc()).limit(limit))
    )


def pending_events(session: Session, limit: int = 20) -> list[AlertEvent]:
    return list(
        session.scalars(
            select(AlertEvent)
            .where(AlertEvent.status.in_(["pending", "failed"]))
            .order_by(AlertEvent.trigger_ts)
            .limit(limit)
        )
    )


def delete_events_before(session: Session, cutoff: datetime) -> int:
    """Retention helper for the 20:00 maintenance job."""
    result = session.execute(delete(AlertEvent).where(AlertEvent.trigger_ts < cutoff))
    session.commit()
    return int(result.rowcount or 0)


def seconds_until_next_poll(moment: datetime | None = None) -> int:
    """Small helper for the loop: sleep exactly until the next minute boundary."""
    moment = moment or datetime.now()
    target = (moment + timedelta(seconds=settings.poll_interval_seconds)).replace(microsecond=0)
    return max(1, int((target - moment).total_seconds()))


# ------------------------------------------------------------------ custom rules
def normalize_conditions(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate and normalize user input for a composite rule."""
    cleaned: list[dict[str, Any]] = []
    for item in raw[:4]:
        metric = METRIC_ALIASES.get(str(item.get("metric") or ""), str(item.get("metric") or ""))
        op = str(item.get("op") or "")
        raw_value = item.get("value")
        if not metric and raw_value in (None, ""):
            continue
        if metric not in RULE_METRICS:
            raise ValueError(f"不支持的指标：{metric or '空'}")
        if op not in RULE_OPS:
            raise ValueError("比较方式只能是 >= / <= / > / <")
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("阈值必须是数字") from exc
        if metric in {"price", "amount_yi"} and value <= 0:
            raise ValueError("价格/成交额阈值必须大于 0")
        if metric == "pct_chg" and abs(value) > 30:
            raise ValueError("涨跌幅阈值需在 -30% ~ 30% 之间")
        if metric == "turnover_rate" and not 0 <= value <= 100:
            raise ValueError("换手率阈值需在 0 ~ 100 之间")
        cleaned.append({"metric": metric, "op": op, "value": value})

    if not cleaned:
        raise ValueError("至少填写一个条件")
    return cleaned


def rule_text(rule: AlertRule) -> str:
    params = rule.params or {}
    if rule.rule_type != "COMPOSITE":
        return rule_meta(rule.rule_type)["label"]
    joiner = " 或 " if str(params.get("logic") or "AND").upper() == "OR" else " 且 "
    parts = []
    for item in params.get("conditions") or []:
        meta = RULE_METRICS.get(item.get("metric"), {})
        op = RULE_OPS.get(item.get("op"), item.get("op"))
        parts.append(f"{meta.get('label', item.get('metric'))}{op}{item.get('value')}{meta.get('unit', '')}")
    return joiner.join(parts) or rule_meta(rule.rule_type)["label"]


def upsert_custom_rule(
    session: Session,
    code: str,
    conditions: list[dict[str, Any]],
    *,
    logic: str = "AND",
    channel: str = "feishu",
    max_per_day: int = 5,
    expires_on: date | None = None,
    label: str | None = None,
) -> AlertRule:
    """Create or replace the single composite rule attached to a watch item."""
    watch = session.scalar(
        select(Watchlist).where(Watchlist.code == code, Watchlist.status == "active")
    )
    if watch is None:
        raise ValueError("请先把该股票加入自选，再设置自定义预警")

    logic = "OR" if str(logic).upper() == "OR" else "AND"
    if channel not in RULE_CHANNELS:
        raise ValueError("通知渠道只能是飞书 / 邮件 / 两者")
    cleaned = normalize_conditions(conditions)

    rule = session.scalar(
        select(AlertRule).where(
            AlertRule.target_type == "watch",
            AlertRule.target_id == watch.id,
            AlertRule.rule_type == "COMPOSITE",
        )
    )
    state = (rule.params or {}).get("state") if rule is not None else None
    params: dict[str, Any] = {
        "logic": logic,
        "conditions": cleaned,
        "channel": channel,
        "max_per_day": max(1, min(int(max_per_day), 50)),
        "expires_on": expires_on.isoformat() if expires_on else None,
        "auto": False,
    }
    if label:
        params["label"] = label.strip()[:32]
    if state is not None:
        params["state"] = state

    if rule is None:
        rule = AlertRule(
            target_type="watch",
            target_id=watch.id,
            rule_type="COMPOSITE",
            params=params,
            enabled=True,
            priority=RULE_META["COMPOSITE"]["priority"],
            confirm_samples=1,
            cooldown_sec=settings.alert_cooldown_seconds,
            rearm_threshold=settings.alert_rearm_pct / 100.0,
        )
        session.add(rule)
    else:
        rule.params = params
        rule.enabled = True
        rule.confirm_samples = 1
        rule.cooldown_sec = settings.alert_cooldown_seconds
        rule.rearm_threshold = settings.alert_rearm_pct / 100.0

    session.commit()
    return rule


def list_custom_rules(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        select(AlertRule, Watchlist)
        .join(Watchlist, Watchlist.id == AlertRule.target_id)
        .where(
            AlertRule.target_type == "watch",
            AlertRule.rule_type == "COMPOSITE",
        )
        .order_by(Watchlist.code)
    ).all()
    items: list[dict[str, Any]] = []
    for rule, watch in rows:
        params = rule.params or {}
        items.append(
            {
                "id": rule.id,
                "code": watch.code,
                "name": watch.name,
                "text": rule_text(rule),
                "logic": params.get("logic") or "AND",
                "channel": RULE_CHANNELS.get(params.get("channel") or "feishu", "飞书"),
                "channel_key": params.get("channel") or "feishu",
                "max_per_day": params.get("max_per_day") or 0,
                "expires_on": params.get("expires_on"),
                "enabled": rule.enabled,
            }
        )
    return items


def delete_custom_rule(session: Session, rule_id: int) -> bool:
    rule = session.get(AlertRule, rule_id)
    if rule is None or rule.rule_type != "COMPOSITE":
        return False
    session.delete(rule)
    session.commit()
    return True
