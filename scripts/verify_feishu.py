"""Send a Feishu webhook test card.

Run:
    .venv\\python.exe scripts\\verify_feishu.py

Requires FEISHU_WEBHOOK in .env (copy from .env.example first).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def main() -> int:
    webhook = os.getenv("FEISHU_WEBHOOK", "").strip()
    if not webhook:
        print("未配置 FEISHU_WEBHOOK。")
        print("请在项目根目录创建 .env，并写入：")
        print("  FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxxx")
        return 2

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "TradeAI 通知测试"},
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            "**状态**：✅ 飞书 Webhook 配置成功\n"
                            "**项目**：TradeAI 价格监控\n"
                            "**说明**：这是一条测试消息，后续将用于止损、止盈和买点提醒。"
                        ),
                    },
                },
                {
                    "tag": "note",
                    "elements": [
                        {"tag": "plain_text", "content": "本消息由个人工具生成，不构成投资建议。"}
                    ],
                },
            ],
        },
    }

    try:
        response = httpx.post(webhook, json=payload, timeout=15)
        print(f"HTTP {response.status_code}")
        print(response.text)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"发送失败：{exc}")
        return 1

    print("飞书测试消息已发送。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
