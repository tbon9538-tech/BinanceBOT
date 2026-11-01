"""外部心跳（spec §12、§13）：每次运行成功（run 状态 DONE）后请求一次心跳地址。

外部服务（例如 healthchecks.io）在约定周期内没收到 ping 时由它告警，弥补"程序没运行就无法自我告警"的盲区。
只允许 spec §13 列出的域名；失败只记录 WARN，不影响运行。

地址从环境变量 HEARTBEAT_URL（.env）读取，不放在 config.yaml：地址里的 uuid 相当于密钥，
拿到它就能冒充本程序 ping，而仓库是公开的（D-071）。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from urllib.parse import urlparse

import httpx

from bot.config import HEARTBEAT_HOSTS
from bot.domain.errors import ConfigError

log = logging.getLogger(__name__)

ENV_VAR = "HEARTBEAT_URL"


def _allowed(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname in HEARTBEAT_HOSTS


def url_from_env(environ: Mapping[str, str] | None = None) -> str | None:
    """返回 HEARTBEAT_URL；未设置或为空时返回 None（不发心跳）。域名不在允许列表中时报错（错误信息不含地址）。"""
    env = os.environ if environ is None else environ
    url = env.get(ENV_VAR, "").strip()
    if not url:
        return None
    if not _allowed(url):
        raise ConfigError(f"{ENV_VAR} 只允许 https 且域名属于 {sorted(HEARTBEAT_HOSTS)}（spec §13）")
    return url


def ping(url: str | None, *, transport: httpx.BaseTransport | None = None, timeout_sec: float = 10.0) -> bool:
    if not url:
        return False
    if not _allowed(url):
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
