"""DeepSeek client with budget, retry and call logging.

The client is deliberately small and offline-first: every call is explicit, the
daily budget is checked before the request, and the token/cost record is written
to ``ai_call_log`` even when the call fails. Nothing in the monitoring loop calls
this module.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from typing import Any

import httpx
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import AICallLog

Transport = Callable[[str, dict[str, str], dict[str, Any]], tuple[int, str]]


class AIError(RuntimeError):
    """Any AI failure that should be shown to the user or logged."""


@dataclass(slots=True)
class AIUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0


def _http_transport(url: str, headers: dict[str, str], payload: dict[str, Any]) -> tuple[int, str]:
    response = httpx.post(
        url,
        headers=headers,
        json=payload,
        timeout=settings.deepseek_timeout_seconds,
    )
    return response.status_code, response.text


def _extract_json(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise AIError(f"模型未返回 JSON：{content[:200]}")
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise AIError(f"JSON 解析失败：{exc}") from exc
    if not isinstance(parsed, dict):
        raise AIError("模型返回的 JSON 不是对象")
    return parsed


def _day_start(day: date) -> datetime:
    return datetime.combine(day, dtime.min)


def spend_today(session: Session, day: date | None = None) -> float:
    day = day or date.today()
    total = session.scalar(
        select(func.coalesce(func.sum(AICallLog.cost), 0.0)).where(
            AICallLog.created_at >= _day_start(day)
        )
    )
    return float(total or 0.0)


def usage_summary(session: Session, day: date | None = None) -> dict:
    day = day or date.today()
    rows = list(
        session.scalars(select(AICallLog).where(AICallLog.created_at >= _day_start(day)))
    )
    cost = round(sum(row.cost for row in rows), 4)
    return {
        "date": day.isoformat(),
        "calls": len(rows),
        "success": sum(1 for row in rows if row.status == "success"),
        "failed": sum(1 for row in rows if row.status == "failed"),
        "input_tokens": sum(row.input_tokens for row in rows),
        "output_tokens": sum(row.output_tokens for row in rows),
        "cost": cost,
        "budget": settings.ai_daily_budget_cny,
        "remaining": round(max(0.0, settings.ai_daily_budget_cny - cost), 4),
    }


class DeepSeekClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        retries: int = 3,
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.deepseek_api_key
        self.base_url = (base_url or settings.deepseek_base_url).rstrip("/")
        self.model = model or settings.deepseek_model
        self.transport = transport or _http_transport
        self.sleep = sleep
        self.retries = retries

    # ---------------------------------------------------------------- request
    def complete_json(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 1400,
        temperature: float = 0.2,
    ) -> tuple[dict, AIUsage]:
        if not self.api_key:
            raise AIError("未配置 DEEPSEEK_API_KEY，请在 .env 中填写后重启")

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if settings.ai_json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                status, body = self.transport(url, headers, payload)
                if status != 200:
                    raise AIError(f"DeepSeek HTTP {status}: {body[:300]}")
                data = json.loads(body)
                content = data["choices"][0]["message"]["content"]
                usage = data.get("usage") or {}
                parsed = _extract_json(content)
                return parsed, AIUsage(
                    input_tokens=int(usage.get("prompt_tokens", 0)),
                    output_tokens=int(usage.get("completion_tokens", 0)),
                )
            except Exception as exc:  # noqa: BLE001 - retry transport/parse issues
                last_error = exc
                logger.warning("DeepSeek attempt {}/{} failed: {}", attempt + 1, self.retries, exc)
                if attempt + 1 < self.retries:
                    self.sleep(1.0 * (2**attempt))

        raise AIError(str(last_error) if last_error else "DeepSeek 调用失败")

    def call(
        self,
        session: Session,
        *,
        purpose: str,
        prompt_version: str,
        system: str,
        user: str,
        code: str | None = None,
        max_tokens: int = 1400,
    ) -> tuple[dict, AICallLog]:
        """Budget-checked call that always writes an ``ai_call_log`` row."""
        spent = spend_today(session)
        if spent >= settings.ai_daily_budget_cny:
            raise AIError(
                f"AI 今日预算已用完（{spent:.3f} / {settings.ai_daily_budget_cny:.2f} 元）"
            )

        started = time.perf_counter()
        try:
            parsed, usage = self.complete_json(system, user, max_tokens=max_tokens)
            latency = int((time.perf_counter() - started) * 1000)
            cost = (
                usage.input_tokens / 1_000_000 * settings.ai_input_price_cny
                + usage.output_tokens / 1_000_000 * settings.ai_output_price_cny
            )
            log = AICallLog(
                code=code,
                purpose=purpose,
                model=self.model,
                prompt_version=prompt_version,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost=round(cost, 6),
                latency_ms=latency,
                status="success",
            )
            session.add(log)
            session.commit()
            logger.info(
                "AI {} {}: {} in / {} out, ¥{:.4f}, {} ms",
                purpose,
                code or "-",
                usage.input_tokens,
                usage.output_tokens,
                cost,
                latency,
            )
            return parsed, log
        except Exception as exc:  # noqa: BLE001 - failure must be recorded
            latency = int((time.perf_counter() - started) * 1000)
            log = AICallLog(
                code=code,
                purpose=purpose,
                model=self.model,
                prompt_version=prompt_version,
                input_tokens=0,
                output_tokens=0,
                cost=0.0,
                latency_ms=latency,
                status="failed",
                error=str(exc)[:1000],
            )
            session.add(log)
            session.commit()
            raise


__all__ = [
    "AIError",
    "AIUsage",
    "DeepSeekClient",
    "spend_today",
    "usage_summary",
]
