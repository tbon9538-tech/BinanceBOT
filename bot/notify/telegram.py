"""Telegram 通知（spec §12）：只用 sendMessage，**只发通知，不接收任何命令**（doc/DECISIONS.md D-006）。

没有配置 token 或 chat_id 时自动降级为只写日志。发送失败只记录 WARN，不影响主流程。
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

API_HOST = "https://api.telegram.org"
MAX_LEN = 4000  # Telegram 单条消息上限约 4096 字符


class TelegramNotifier:
    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
        *,
        enabled: bool = True,
        transport: httpx.BaseTransport | None = None,
        timeout_sec: float = 10.0,
    ) -> None:
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID", "")
        self.enabled = enabled and bool(self.token) and bool(self.chat_id)
        self._http = httpx.Client(timeout=timeout_sec, transport=transport) if self.enabled else None

    def send(self, text: str, level: str = "INFO") -> bool:
        """返回是否发送成功。未启用时只写日志。"""
        lvl = {"WARN": logging.WARNING, "WARNING": logging.WARNING, "CRITICAL": logging.CRITICAL}.get(
            level, logging.INFO
        )
        log.log(lvl, text)
        if not self.enabled or self._http is None:
            return False
        prefix = {"WARN": "⚠️ ", "WARNING": "⚠️ ", "CRITICAL": "🚨 "}.get(level, "")
        body = (prefix + text)[:MAX_LEN]
        try:
            resp = self._http.post(
                f"{API_HOST}/bot{self.token}/sendMessage", data={"chat_id": self.chat_id, "text": body}
            )
            if resp.status_code != 200:
                log.warning("Telegram 发送失败：HTTP %s", resp.status_code)
                return False
            return True
        except httpx.HTTPError as e:
            log.warning("Telegram 发送失败：%s", type(e).__name__)
            return False
