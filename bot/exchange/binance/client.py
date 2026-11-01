"""币安 REST 客户端：HTTP、限频、错误映射（spec §3.5、§6.1）。

M1 只实现公开 GET 接口；签名与下单在 M5 加入。
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from bot.domain.errors import ExchangeError, RateLimitedError

log = logging.getLogger(__name__)

# spec §0、§13：交易与行情 REST 只允许这些域名
ALLOWED_API_HOSTS = frozenset(
    {"data-api.binance.vision", "testnet.binance.vision", "demo-api.binance.com", "api.binance.com"}
)

GET_RETRY_DELAYS_SEC = (1.0, 2.0, 4.0)  # GET 遇到网络错误或 5xx 时的退避（spec §6.1）
DEFAULT_RETRY_AFTER_SEC = 60.0
WEIGHT_PAUSE_RATIO = 0.8


@dataclass
class RateLimitState:
    """进程内共享的限频状态。data-api 与 api.binance.com 共用同一个 IP 权重额度（spec §3.5）。"""

    weight_limit_1m: int = 6000
    used_weight_1m: int = 0
    used_at_minute: int = -1
    blocked_until: float = 0.0
    banned: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set_limit_from_exchange_info(self, info: Mapping[str, Any]) -> None:
        for rl in info.get("rateLimits", []):
            if rl.get("rateLimitType") == "REQUEST_WEIGHT" and rl.get("interval") == "MINUTE":
                per_min = int(rl["limit"]) / max(int(rl.get("intervalNum", 1)), 1)
                with self._lock:
                    self.weight_limit_1m = int(per_min)

    def wait_time(self, now: float) -> float:
        """发请求前需要等待的秒数；被封禁时抛出 RateLimitedError。"""
        with self._lock:
            if self.banned:
                raise RateLimitedError(418, None)
            wait = max(0.0, self.blocked_until - now)
            minute = int(now // 60)
            if (
                minute == self.used_at_minute
                and self.used_weight_1m >= WEIGHT_PAUSE_RATIO * self.weight_limit_1m
            ):
                wait = max(wait, (minute + 1) * 60 - now)
            return wait

    def record_used_weight(self, used: int, now: float) -> None:
        with self._lock:
            self.used_weight_1m = used
            self.used_at_minute = int(now // 60)

    def record_429(self, retry_after: float, now: float) -> None:
        with self._lock:
            self.blocked_until = max(self.blocked_until, now + retry_after)

    def record_418(self) -> None:
        with self._lock:
            self.banned = True


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None


def _exchange_error(resp: httpx.Response) -> ExchangeError:
    try:
        body = resp.json()
        return ExchangeError(int(body["code"]), str(body.get("msg", "")), resp.status_code)
    except Exception:  # 非 JSON 响应（例如 WAF 403 页面）
        return ExchangeError(-resp.status_code, resp.text[:200], resp.status_code)


class BinanceHttpClient:
    """同步 HTTP 客户端。sleep / now 可注入，便于测试。"""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_sec: float = 10.0,
        rate_state: RateLimitState | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        host = urlparse(base_url).hostname
        if urlparse(base_url).scheme != "https" or host not in ALLOWED_API_HOSTS:
            raise ValueError(f"不允许的 API 地址：{base_url}（spec §13）")
        self.base_url = base_url.rstrip("/")
        self.rate = rate_state or RateLimitState()
        self._sleep = sleep
        self._now = now
        self._http = httpx.Client(
            base_url=self.base_url, timeout=httpx.Timeout(timeout_sec, connect=5.0), transport=transport
        )
        self._time_offset_ms: float | None = None
        self._time_offset_at = 0.0

    def close(self) -> None:
        self._http.close()

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """公开 GET。网络错误与 5xx 按 1/2/4 秒退避重试；429/418/4xx 不重试。"""
        attempt = 0
        while True:
            wait = self.rate.wait_time(self._now())
            if wait > 0:
                log.info(
                    "限频等待 %.1f 秒（已用权重 %s/%s）",
                    wait,
                    self.rate.used_weight_1m,
                    self.rate.weight_limit_1m,
                )
                self._sleep(wait)
            try:
                resp = self._http.get(path, params=dict(params or {}))
            except httpx.TransportError as e:
                if attempt >= len(GET_RETRY_DELAYS_SEC):
                    raise
                log.warning("GET %s 网络错误：%s，%.0f 秒后重试", path, e, GET_RETRY_DELAYS_SEC[attempt])
                self._sleep(GET_RETRY_DELAYS_SEC[attempt])
                attempt += 1
                continue
            used = resp.headers.get("X-MBX-USED-WEIGHT-1M")
            if used is not None and used.isdigit():
                self.rate.record_used_weight(int(used), self._now())
            if resp.status_code == 429:
                retry_after = _retry_after(resp) or DEFAULT_RETRY_AFTER_SEC
                self.rate.record_429(retry_after, self._now())
                raise RateLimitedError(429, retry_after)
            if resp.status_code == 418:
                self.rate.record_418()
                raise RateLimitedError(418, _retry_after(resp))
            if resp.status_code >= 500:
                if attempt >= len(GET_RETRY_DELAYS_SEC):
                    raise _exchange_error(resp)
                log.warning(
                    "GET %s 返回 %s，%.0f 秒后重试", path, resp.status_code, GET_RETRY_DELAYS_SEC[attempt]
                )
                self._sleep(GET_RETRY_DELAYS_SEC[attempt])
                attempt += 1
                continue
            if resp.status_code >= 400:
                raise _exchange_error(resp)
            return resp.json()

    def server_time_ms(self, max_age_sec: float = 600.0) -> int:
        """服务器当前时间（毫秒）。时钟偏移每 max_age_sec 秒刷新一次。"""
        now = self._now()
        if self._time_offset_ms is None or now - self._time_offset_at > max_age_sec:
            before = self._now()
            server = int(self.get("/api/v3/time")["serverTime"])
            after = self._now()
            self._time_offset_ms = server - (before + after) / 2 * 1000
            self._time_offset_at = after
            now = after
        return math.floor(now * 1000 + self._time_offset_ms)
