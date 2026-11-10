"""Notification delivery: Feishu card first, SMTP email as fallback.

Every attempt is written to ``notification_log`` (request payload, response,
status) so failures can be inspected and retried. Sending never changes the
alert trigger state; a failed send simply leaves the event ``failed``.
"""

from __future__ import annotations

import json
import smtplib
import time
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Callable

import httpx
from loguru import logger
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import AlertEvent, NotificationLog
from app.services.alerts import rule_meta

Transport = Callable[[str, dict[str, Any]], tuple[int, str]]

SUGGESTIONS = {
    "STOP_LOSS": "关注是否放量下跌；若跌破后无法收回，按计划减仓或离场。",
    "TRAILING_STOP": "从持仓高点回撤达到阈值，检查是否跌破 MA20，考虑分批止盈。",
    "TAKE_PROFIT_1": "到达第一止盈位，可考虑分批止盈，并把止损上移到成本价。",
    "TAKE_PROFIT_2": "到达第二止盈位，剩余仓位可用移动止盈跟踪。",
    "PRICE_IN_BUY_ZONE": "进入计划建仓区间，按计划分批执行，并确认止损位。",
    "PRICE_CHANGE": "波动异常，先检查公告、板块联动与成交量，再决定是否操作。",
}

DISCLAIMER = "本消息由个人工具生成，不构成投资建议。"


# --------------------------------------------------------------------- formatting
def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _condition_text(rule_type: str, threshold: Any, snapshot: dict[str, Any]) -> str:
    price = _fmt(snapshot.get("price"))
    if rule_type == "STOP_LOSS":
        return f"现价 {price} ≤ 止损价 {_fmt(threshold)}"
    if rule_type == "TRAILING_STOP":
        return f"现价 {price} ≤ 移动止盈线 {_fmt(threshold)}"
    if rule_type == "TAKE_PROFIT_1":
        return f"现价 {price} ≥ 止盈一 {_fmt(threshold)}"
    if rule_type == "TAKE_PROFIT_2":
        return f"现价 {price} ≥ 止盈二 {_fmt(threshold)}"
    if rule_type == "PRICE_IN_BUY_ZONE":
        params = snapshot.get("params") or {}
        return f"现价 {price} 进入建仓区间 {_fmt(params.get('buy_low'))} ~ {_fmt(params.get('buy_high'))}"
    if rule_type == "PRICE_CHANGE":
        return f"当日涨跌幅 {_fmt(snapshot.get('pct_chg'))}% 超过阈值 {_fmt(threshold)}%"
    return f"现价 {price} 触发 {rule_type}"


def build_feishu_payload(event: AlertEvent) -> dict[str, Any]:
    """Build an interactive Feishu card for an alert event."""
    snapshot = event.condition_snapshot or {}
    meta = rule_meta(_rule_type_of(event))
    name = snapshot.get("name") or event.code
    price = _fmt(snapshot.get("price"))
    pct = snapshot.get("pct_chg")
    pct_text = f"{pct:+.2f}%" if isinstance(pct, (int, float)) else "—"

    fields = [
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**现价**\n{price}（{pct_text}）"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**触发**\n{meta['label']}"}},
    ]

    holding = snapshot.get("holding")
    if holding:
        fields.append(
            {
                "is_short": True,
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**持仓**\n成本 {_fmt(holding.get('cost_price'))} / "
                        f"{holding.get('shares')} 股 / 浮盈 {_fmt(holding.get('pnl_pct'))}%"
                    ),
                },
            }
        )
        t1 = "T+1，今日不可卖" if holding.get("available_shares") == 0 else "可卖"
        fields.append(
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**可卖**\n{t1}"}}
        )

    plan = snapshot.get("plan")
    if plan:
        fields.append(
            {
                "is_short": False,
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**计划** {plan.get('plan_no') or '—'}（{plan.get('trade_date') or '—'}）\n"
                        f"止损 {_fmt(plan.get('stop_loss'))} · 止盈一 {_fmt(plan.get('take_profit_1'))} · "
                        f"止盈二 {_fmt(plan.get('take_profit_2'))}"
                    ),
                },
            }
        )

    rule_type = _rule_type_of(event)
    elements: list[dict[str, Any]] = [
        {"tag": "div", "fields": fields},
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**条件** {_condition_text(rule_type, snapshot.get('threshold'), snapshot)}",
            },
        },
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**建议** {SUGGESTIONS.get(rule_type, '请结合盘面自行判断。')}",
            },
        },
        {"tag": "hr"},
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": (
                        f"{event.trigger_ts:%Y-%m-%d %H:%M:%S} · {event.code} · {DISCLAIMER}"
                    ),
                }
            ],
        },
    ]

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": meta["color"],
                "title": {"tag": "plain_text", "content": f"[{meta['label']}] {name}（{event.code}）"},
            },
            "elements": elements,
        },
    }


def build_email_text(event: AlertEvent) -> tuple[str, str]:
    snapshot = event.condition_snapshot or {}
    rule_type = _rule_type_of(event)
    meta = rule_meta(rule_type)
    name = snapshot.get("name") or event.code
    subject = f"[{meta['label']}] {name}（{event.code}）"

    lines = [
        subject,
        "",
        f"现价：{_fmt(snapshot.get('price'))}（{_fmt(snapshot.get('pct_chg'))}%）",
        f"条件：{_condition_text(rule_type, snapshot.get('threshold'), snapshot)}",
    ]
    holding = snapshot.get("holding")
    if holding:
        lines.append(
            f"持仓：成本 {_fmt(holding.get('cost_price'))} / {holding.get('shares')} 股 / "
            f"浮盈 {_fmt(holding.get('pnl_pct'))}% / 可卖 {holding.get('available_shares')} 股"
        )
    plan = snapshot.get("plan")
    if plan:
        lines.append(
            f"计划：{plan.get('plan_no') or '—'} 止损 {_fmt(plan.get('stop_loss'))} "
            f"止盈 {_fmt(plan.get('take_profit_1'))}"
        )
    lines += [
        f"建议：{SUGGESTIONS.get(rule_type, '请结合盘面自行判断。')}",
        f"时间：{event.trigger_ts:%Y-%m-%d %H:%M:%S}",
        "",
        DISCLAIMER,
    ]
    return subject, "\n".join(lines)


def _rule_type_of(event: AlertEvent) -> str:
    snapshot = event.condition_snapshot or {}
    rule_type = snapshot.get("rule_type")
    if rule_type:
        return str(rule_type)
    params = snapshot.get("params") or {}
    return str(params.get("rule_type") or "ALERT")


def condition_text(event: AlertEvent) -> str:
    """Public one-line condition description, shared by cards, email and the UI."""
    snapshot = event.condition_snapshot or {}
    return _condition_text(_rule_type_of(event), snapshot.get("threshold"), snapshot)


def _http_transport(url: str, payload: dict[str, Any]) -> tuple[int, str]:
    response = httpx.post(url, json=payload, timeout=10.0)
    return response.status_code, response.text[:2000]


def _feishu_ok(body: str) -> bool:
    """Feishu returns ``{"StatusCode":0,"code":0,...}`` on success."""
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return False
    code = data.get("code", data.get("StatusCode"))
    return str(code) == "0"


# --------------------------------------------------------------------- notifier
class Notifier:
    def __init__(
        self,
        *,
        webhook: str | None = None,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.webhook = webhook if webhook is not None else settings.feishu_webhook
        self.transport = transport or _http_transport
        self.sleep = sleep

    # ---------------------------------------------------------------- channels
    def _send_feishu(self, event: AlertEvent) -> tuple[bool, str, str]:
        if not self.webhook:
            return False, "feishu webhook 未配置", ""
        payload = build_feishu_payload(event)
        try:
            status, body = self.transport(self.webhook, payload)
        except Exception as exc:  # noqa: BLE001 - notification must never crash the loop
            return False, f"请求异常：{exc}", ""
        ok = status == 200 and _feishu_ok(body)
        return ok, body, "feishu"

    def _send_email(self, event: AlertEvent) -> bool:
        if not (settings.smtp_host and settings.smtp_to and settings.smtp_from):
            return False
        subject, text = build_email_text(event)
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = settings.smtp_from
        message["To"] = settings.smtp_to
        message.set_content(text)

        try:
            if settings.smtp_use_ssl:
                server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=15)
            else:
                server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)
            with server:
                if settings.smtp_user:
                    server.login(settings.smtp_user, settings.smtp_password)
                server.send_message(message)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("email fallback failed: {}", exc)
            return False

    # ---------------------------------------------------------------- delivery
    def send_event(
        self,
        session: Session,
        event: AlertEvent,
        *,
        retries: int = 3,
        backoff: float = 1.0,
    ) -> NotificationLog:
        """Send one event, with retry + channel fallback, and persist the log."""
        last_log: NotificationLog | None = None

        for attempt in range(1, retries + 1):
            ok, body, channel = self._send_feishu(event)
            last_log = NotificationLog(
                event_id=event.id,
                channel=channel or "feishu",
                payload=build_feishu_payload(event) if self.webhook else None,
                response=body,
                status="success" if ok else "failed",
                retry_count=attempt - 1,
            )
            session.add(last_log)
            session.flush()

            if ok:
                event.status = "sent"
                event.notified_at = datetime.now()
                session.commit()
                return last_log

            logger.warning("feishu send failed (attempt {}/{}): {}", attempt, retries, body[:120])
            if attempt < retries:
                self.sleep(min(30.0, backoff * (2 ** (attempt - 1))))

        if self._send_email(event):
            log = NotificationLog(
                event_id=event.id,
                channel="email",
                payload=None,
                response="email fallback succeeded",
                status="success",
                retry_count=retries,
            )
            session.add(log)
            event.status = "sent"
            event.notified_at = datetime.now()
            session.commit()
            return log

        event.status = "failed"
        session.commit()
        assert last_log is not None
        return last_log

    def notify_pending(self, session: Session, limit: int = 20) -> dict[str, int]:
        from app.services.alerts import pending_events

        summary = {"sent": 0, "failed": 0}
        for event in pending_events(session, limit=limit):
            log = self.send_event(session, event)
            summary["sent" if log.status == "success" else "failed"] += 1
        return summary
