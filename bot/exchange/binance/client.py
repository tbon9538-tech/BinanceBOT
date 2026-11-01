"""币安 REST 客户端：HTTP、签名（HMAC/Ed25519）、限频、错误映射（spec §3.5、§3.7、§6.1）。

- 公开 GET 与签名 GET：网络错误或 5xx 按 1、2、4 秒退避，最多重试 3 次。
- 下单 / 撤单（POST、DELETE）**一律不自动重试**：超时、网络中断、5xx、-1006/-1007 抛出
  RequestStatusUnknown，由账户层转换为 UnknownOrderStatus（spec §6.6）。
- 签名：先把参数 percent-encode 成 query string，再对这个字符串签名（2026-01-15 起强制，否则 -1022）。
  所有签名请求的参数都放在 query string 里，body 为空。
- 日志只记录方法和路径，绝不记录 query string（其中有签名）。
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Literal
from urllib.parse import quote, urlencode, urlparse

import httpx

from bot.domain.errors import BotError, ExchangeError, ExchangeUnavailableError, RateLimitedError
from bot.exchange.binance.credentials import ApiCredentials, redact_secrets

log = logging.getLogger(__name__)

# spec §0、§13：交易与行情 REST 只允许这些域名
ALLOWED_API_HOSTS = frozenset(
    {"data-api.binance.vision", "testnet.binance.vision", "demo-api.binance.com", "api.binance.com"}
)

GET_RETRY_DELAYS_SEC = (1.0, 2.0, 4.0)  # GET 遇到网络错误或 5xx 时的退避（spec §6.1）
DEFAULT_RETRY_AFTER_SEC = 60.0
WEIGHT_PAUSE_RATIO = 0.8
ORDER_PAUSE_RATIO = 0.8  # 下单计数达到 ORDERS 限额的 80% 时，等到下一个窗口
MAX_ORDER_WAIT_SEC = 60.0  # 需要等待更久时直接按限频处理，停止运行
UNKNOWN_STATUS_CODES = frozenset({-1006, -1007})  # 官方：execution status unknown
TIMESTAMP_ERROR_CODE = -1021
API_KEY_HEADER = "X-MBX-APIKEY"

_INTERVAL_SEC = {"S": 1, "M": 60, "H": 3600, "D": 86400}
_INTERVAL_NAME_SEC = {"SECOND": 1, "MINUTE": 60, "HOUR": 3600, "DAY": 86400}
_ORDER_COUNT_HEADER_RE = re.compile(r"^x-mbx-order-count-(\d+)([smhd])$", re.IGNORECASE)


class RequestStatusUnknown(BotError):
    """下单 / 撤单请求已经发出，但执行结果未知：超时、网络中断、5xx、-1006/-1007（spec §6.6）。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class RateLimitState:
    """进程内共享的限频状态。data-api 与 api.binance.com 共用同一个 IP 权重额度（spec §3.5）。"""

    weight_limit_1m: int = 6000
    used_weight_1m: int = 0
    used_at_minute: int = -1
    blocked_until: float = 0.0
    banned: bool = False
    order_limits: dict[int, int] = field(default_factory=dict)  # 窗口秒数 → ORDERS 限额
    order_counts: dict[int, tuple[int, int]] = field(default_factory=dict)  # 窗口秒数 → (已下单数, 窗口序号)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set_limit_from_exchange_info(self, info: Mapping[str, Any]) -> None:
        for rl in info.get("rateLimits", []):
            kind = rl.get("rateLimitType")
            num = max(int(rl.get("intervalNum", 1)), 1)
            if kind == "REQUEST_WEIGHT" and rl.get("interval") == "MINUTE":
                with self._lock:
                    self.weight_limit_1m = int(int(rl["limit"]) / num)
            elif kind == "ORDERS" and rl.get("interval") in _INTERVAL_NAME_SEC:
                with self._lock:
                    self.order_limits[num * _INTERVAL_NAME_SEC[rl["interval"]]] = int(rl["limit"])

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

    def order_wait_time(self, now: float) -> float:
        """下单前额外需要等待的秒数（按响应头 X-MBX-ORDER-COUNT-* 与 exchangeInfo 的 ORDERS 限额）。"""
        wait = 0.0
        with self._lock:
            for sec, limit in self.order_limits.items():
                count, window = self.order_counts.get(sec, (0, -1))
                current = int(now // sec)
                if window == current and count >= ORDER_PAUSE_RATIO * limit:
                    wait = max(wait, (current + 1) * sec - now)
        return wait

    def record_used_weight(self, used: int, now: float) -> None:
        with self._lock:
            self.used_weight_1m = used
            self.used_at_minute = int(now // 60)

    def record_order_count(self, interval_sec: int, count: int, now: float) -> None:
        with self._lock:
            self.order_counts[interval_sec] = (count, int(now // interval_sec))

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
        return ExchangeError(-resp.status_code, redact_secrets(resp.text[:200]), resp.status_code)


def format_param(value: Any) -> str:
    """请求参数转字符串：Decimal 不用科学计数法，bool 用 true/false。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, float):
        raise TypeError("请求参数不得使用 float（spec §6.1：一律 Decimal）")
    return str(value)


def encode_params(params: Mapping[str, Any]) -> str:
    """按插入顺序把参数 percent-encode 成 query string（RFC 3986：只保留 A-Za-z0-9-_.~）。"""
    return urlencode([(k, format_param(v)) for k, v in params.items()], quote_via=quote)


def sign_query(params: Mapping[str, Any], creds: ApiCredentials) -> str:
    """先 percent-encode 再签名，返回附带 signature 的完整 query string（spec §6.1）。"""
    payload = encode_params(params)
    signature = creds.signer.sign(payload)
    return f"{payload}&signature={quote(signature, safe='')}"


def _interval_seconds(num: str, letter: str) -> int:
    return int(num) * _INTERVAL_SEC[letter.upper()]


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
        credentials: ApiCredentials | None = None,
        recv_window_ms: int = 5000,
    ) -> None:
        host = urlparse(base_url).hostname
        if urlparse(base_url).scheme != "https" or host not in ALLOWED_API_HOSTS:
            raise ValueError(f"不允许的 API 地址：{base_url}（spec §13）")
        if not 0 < recv_window_ms <= 60000:
            raise ValueError("recvWindow 必须在 (0, 60000] 毫秒之间")
        self.base_url = base_url.rstrip("/")
        self.rate = rate_state or RateLimitState()
        self._sleep = sleep
        self._now = now
        self._credentials = credentials
        self.recv_window_ms = recv_window_ms
        self._http = httpx.Client(
            base_url=self.base_url, timeout=httpx.Timeout(timeout_sec, connect=5.0), transport=transport
        )
        self._time_offset_ms: float | None = None
        self._time_offset_at = 0.0
        if credentials is not None:
            # httpx 在 INFO 级别会记录完整 URL（含签名）：带凭据时强制提高到 WARNING
            for name in ("httpx", "httpcore"):
                lg = logging.getLogger(name)
                if lg.getEffectiveLevel() < logging.WARNING:
                    lg.setLevel(logging.WARNING)

    def close(self) -> None:
        self._http.close()

    @property
    def has_credentials(self) -> bool:
        return self._credentials is not None

    # ---- 发送与限频 ----

    def _wait_for_capacity(self, *, order: bool) -> None:
        now = self._now()
        wait = self.rate.wait_time(now)
        if order:
            order_wait = self.rate.order_wait_time(now)
            if order_wait > MAX_ORDER_WAIT_SEC:
                raise RateLimitedError(429, order_wait)
            wait = max(wait, order_wait)
        if wait > 0:
            log.info(
                "限频等待 %.1f 秒（已用权重 %s/%s）",
                wait,
                self.rate.used_weight_1m,
                self.rate.weight_limit_1m,
            )
            self._sleep(wait)

    def _record_headers(self, resp: httpx.Response) -> None:
        now = self._now()
        used = resp.headers.get("X-MBX-USED-WEIGHT-1M")
        if used is not None and used.isdigit():
            self.rate.record_used_weight(int(used), now)
        for name, value in resp.headers.items():
            m = _ORDER_COUNT_HEADER_RE.match(name)
            if m and value.isdigit():
                self.rate.record_order_count(_interval_seconds(m.group(1), m.group(2)), int(value), now)

    def _send(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        order: bool = False,
    ) -> httpx.Response:
        """发送一次请求：等待限频、记录响应头、处理 429/418。网络错误原样抛出 httpx.TransportError。"""
        self._wait_for_capacity(order=order)
        resp = self._http.request(
            method, url, params=dict(params) if params is not None else None, headers=headers
        )
        self._record_headers(resp)
        if resp.status_code == 429:
            retry_after = _retry_after(resp) or DEFAULT_RETRY_AFTER_SEC
            self.rate.record_429(retry_after, self._now())
            raise RateLimitedError(429, retry_after)
        if resp.status_code == 418:
            self.rate.record_418()
            raise RateLimitedError(418, _retry_after(resp))
        return resp

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """公开 GET。网络错误与 5xx 按 1/2/4 秒退避重试；429/418/4xx 不重试。"""
        attempt = 0
        while True:
            try:
                resp = self._send("GET", path, params=dict(params or {}))
            except httpx.TransportError as e:
                if attempt >= len(GET_RETRY_DELAYS_SEC):
                    raise
                log.warning("GET %s 网络错误：%s，%.0f 秒后重试", path, e, GET_RETRY_DELAYS_SEC[attempt])
                self._sleep(GET_RETRY_DELAYS_SEC[attempt])
                attempt += 1
                continue
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

    # ---- 时间 ----

    def server_time_ms(self, max_age_sec: float = 600.0) -> int:
        """服务器当前时间（毫秒）。时钟偏移每 max_age_sec 秒刷新一次（spec §3.7）。"""
        now = self._now()
        if self._time_offset_ms is None or now - self._time_offset_at > max_age_sec:
            before = self._now()
            server = int(self.get("/api/v3/time")["serverTime"])
            after = self._now()
            self._time_offset_ms = server - (before + after) / 2 * 1000
            self._time_offset_at = after
            now = after
        return math.floor(now * 1000 + self._time_offset_ms)

    def sync_time(self) -> None:
        """强制重新计算时钟偏移。"""
        self._time_offset_ms = None
        self.server_time_ms()

    # ---- 签名请求 ----

    def _require_credentials(self) -> ApiCredentials:
        if self._credentials is None:
            raise ExchangeError(-2015, "没有配置 API 凭据，不能发送签名请求")
        return self._credentials

    def _signed_url(self, path: str, params: Mapping[str, Any] | None) -> str:
        creds = self._require_credentials()
        try:
            timestamp = self.server_time_ms()
        except httpx.TransportError as e:
            raise ExchangeUnavailableError(f"同步服务器时间失败：{type(e).__name__}") from e
        full = dict(params or {})
        full["recvWindow"] = self.recv_window_ms
        full["timestamp"] = timestamp
        return f"{path}?{sign_query(full, creds)}"

    def _auth_headers(self) -> dict[str, str]:
        return {API_KEY_HEADER: self._require_credentials().api_key}

    def signed_get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """签名 GET（USER_DATA）。网络错误与 5xx 按 1/2/4 秒重试；-1021 时重新对时后再试一次。"""
        attempt = 0
        resynced = False
        while True:
            url = self._signed_url(path, params)  # 每次重试都重新取 timestamp、重新签名
            try:
                resp = self._send("GET", url, headers=self._auth_headers())
            except httpx.TransportError as e:
                if attempt >= len(GET_RETRY_DELAYS_SEC):
                    raise ExchangeUnavailableError(
                        f"GET {path} 多次网络错误：{type(e).__name__}: {redact_secrets(str(e))}"
                    ) from e
                log.warning(
                    "GET %s 网络错误：%s，%.0f 秒后重试",
                    path,
                    type(e).__name__,
                    GET_RETRY_DELAYS_SEC[attempt],
                )
                self._sleep(GET_RETRY_DELAYS_SEC[attempt])
                attempt += 1
                continue
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
                err = _exchange_error(resp)
                if err.code == TIMESTAMP_ERROR_CODE and not resynced:
                    log.warning("GET %s 返回 -1021（时间戳超出 recvWindow），重新对时后再试一次", path)
                    self.sync_time()
                    resynced = True
                    continue
                raise err
            return resp.json()

    def signed_order_request(
        self, method: Literal["POST", "DELETE"], path: str, params: Mapping[str, Any]
    ) -> Any:
        """下单 / 撤单：只发送一次，绝不自动重试（spec §6.1、§6.6）。

        - 请求发出之前失败（对时失败、限频）：抛出 ExchangeUnavailableError / RateLimitedError，
          订单肯定没有发出；
        - 发出之后超时、网络中断、5xx、-1006/-1007：抛出 RequestStatusUnknown；
        - 其他 4xx：抛出 ExchangeError（交易所明确拒绝）。
        """
        url = self._signed_url(path, params)
        headers = self._auth_headers()
        try:
            resp = self._send(method, url, headers=headers, order=(method == "POST"))
        except httpx.TransportError as e:
            log.warning("%s %s 网络错误（执行状态未知）：%s", method, path, type(e).__name__)
            raise RequestStatusUnknown(f"{type(e).__name__}: {redact_secrets(str(e))}") from e
        if resp.status_code >= 500:
            err = _exchange_error(resp)
            log.warning("%s %s 返回 HTTP %s（执行状态未知）", method, path, resp.status_code)
            raise RequestStatusUnknown(f"HTTP {resp.status_code} [{err.code}] {err.message}")
        if resp.status_code >= 400:
            err = _exchange_error(resp)
            if err.code in UNKNOWN_STATUS_CODES:
                log.warning("%s %s 返回 %s（执行状态未知）", method, path, err.code)
                raise RequestStatusUnknown(f"[{err.code}] {err.message}")
            raise err
        try:
            body = resp.json()
        except ValueError as e:
            raise RequestStatusUnknown("HTTP 200 但响应不是合法 JSON") from e
        if isinstance(body, dict) and isinstance(body.get("code"), int) and body["code"] < 0:
            if body["code"] in UNKNOWN_STATUS_CODES:
                raise RequestStatusUnknown(f"[{body['code']}] {body.get('msg', '')}")
            raise ExchangeError(int(body["code"]), str(body.get("msg", "")), resp.status_code)
        return body
