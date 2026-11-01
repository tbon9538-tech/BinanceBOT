"""外部心跳（spec §12、§13）：每次运行成功（run 状态 DONE）后请求一次 heartbeat_url。

外部服务（例如 healthchecks.io）在约定周期内没收到 ping 时由它告警，弥补"程序没运行就无法自我告警"的盲区。
只允许 spec §13 列出的域名；失败只记录 WARN，不影响运行。
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

from bot.config import HEARTBEAT_HOSTS

log = logging.getLogger(__name__)


def ping(url: str | None, *, transport: httpx.BaseTransport | None = None, timeout_sec: float = 10.0) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in HEARTBEAT_HOSTS:
        log.warning("心跳地址不在允许列表中，已忽略")
        return False
    try:
        with httpx.Client(timeout=timeout_sec, transport=transport) as c:
            resp = c.get(url)
        if resp.status_code >= 300:
            log.warning("心跳 ping 失败：HTTP %s", resp.status_code)
            return False
        return True
    except httpx.HTTPError as e:
        log.warning("心跳 ping 失败：%s", type(e).__name__)
        return False
