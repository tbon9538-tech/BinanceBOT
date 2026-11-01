"""签名请求、不重试的下单请求、限频（spec §3.5、§3.7、§6.1）。全部用 httpx.MockTransport。"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Callable
from urllib.parse import parse_qsl

import httpx
import pytest

from bot.domain.errors import ExchangeError, ExchangeUnavailableError, RateLimitedError
from bot.exchange.binance.client import BinanceHttpClient, RateLimitState, RequestStatusUnknown
from bot.exchange.binance.credentials import ApiCredentials, HmacSigner, KeyType
from tests.test_binance_client import Clock

BASE = "https://testnet.binance.vision"
KEY = "testkeyAAAA0000111122223333444455556666777788889999ZZZZ"
SECRET = "testsecretBBBB0000111122223333"
CREDS = ApiCredentials(KEY, KeyType.HMAC, HmacSigner(SECRET))
SERVER_OFFSET_MS = 5_000  # 服务器时钟比本地快 5 秒

Handler = Callable[[httpx.Request], httpx.Response]


def make(
    handler: Handler, clock: Clock, rate: RateLimitState | None = None, creds: ApiCredentials | None = CREDS
):
    def wrapped(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": int(clock.t * 1000) + SERVER_OFFSET_MS})
        return handler(req)

    return BinanceHttpClient(
        BASE,
        transport=httpx.MockTransport(wrapped),
        sleep=clock.sleep,
        now=clock.now,
        rate_state=rate,
        credentials=creds,
        recv_window_ms=5000,
    )


def _query(req: httpx.Request) -> str:
    return req.url.query.decode("ascii")


def test_signed_get_header_timestamp_and_signature() -> None:
    clock = Clock()
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"balances": []})

    c = make(handler, clock)
    assert c.signed_get("/api/v3/account", {"omitZeroBalances": True}) == {"balances": []}
    req = seen[0]
    assert req.headers["X-MBX-APIKEY"] == KEY
    q = _query(req)
    payload, sig = q.rsplit("&signature=", 1)
    assert sig == hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    params = dict(parse_qsl(payload))
    assert params["omitZeroBalances"] == "true"
    assert params["recvWindow"] == "5000"
    assert int(params["timestamp"]) == int(clock.t * 1000) + SERVER_OFFSET_MS  # 本地时间 + 偏移
    assert req.content == b""  # 参数全部在 query string 中


def test_signed_get_sends_exactly_the_signed_non_ascii_query() -> None:
    clock = Clock()
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=[])

    make(handler, clock).signed_get("/api/v3/openOrders", {"symbol": "币安人生USDT"})
    q = _query(seen[0])
    assert q.startswith("symbol=%E5%B8%81%E5%AE%89%E4%BA%BA%E7%94%9FUSDT&")
    payload, sig = q.rsplit("&signature=", 1)
    assert sig == hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def test_signed_get_retries_5xx_then_succeeds_and_resigns() -> None:
    clock = Clock()
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(_query(req))
        if len(seen) < 3:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"ok": 1})

    assert make(handler, clock).signed_get("/api/v3/order", {"symbol": "BTCUSDT"}) == {"ok": 1}
    assert clock.sleeps == [1.0, 2.0]
    assert len(set(seen)) == 3  # 每次重试都重新取 timestamp 并重新签名


def test_signed_get_network_errors_raise_unavailable() -> None:
    clock = Clock()

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=req)

    with pytest.raises(ExchangeUnavailableError):
        make(handler, clock).signed_get("/api/v3/account")
    assert clock.sleeps == [1.0, 2.0, 4.0]


def test_signed_get_resyncs_time_once_on_1021() -> None:
    clock = Clock()
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(400, json={"code": -1021, "msg": "Timestamp outside recvWindow"})
        return httpx.Response(200, json={"ok": 1})

    assert make(handler, clock).signed_get("/api/v3/account") == {"ok": 1}
    assert len(calls) == 2

    calls.clear()

    def always(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"code": -1021, "msg": "Timestamp outside recvWindow"})

    with pytest.raises(ExchangeError) as ei:
        make(always, clock).signed_get("/api/v3/account")
    assert ei.value.code == -1021 and len(calls) == 2


@pytest.mark.parametrize(
    "response",
    [
        lambda req: (_ for _ in ()).throw(httpx.ReadTimeout("timed out", request=req)),
        lambda req: (_ for _ in ()).throw(httpx.RemoteProtocolError("closed", request=req)),
        lambda req: httpx.Response(503, json={"code": -1008, "msg": "busy"}),
        lambda req: httpx.Response(500, text="<html>"),
        lambda req: httpx.Response(408, json={"code": -1007, "msg": "Timeout waiting for response"}),
        lambda req: httpx.Response(400, json={"code": -1006, "msg": "Unexpected response"}),
        lambda req: httpx.Response(200, text="not json"),
    ],
    ids=["timeout", "disconnect", "503", "500-html", "-1007", "-1006", "bad-json"],
)
@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_order_requests_never_retry_and_report_unknown(method: str, response: Handler) -> None:
    clock = Clock()
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.method)
        return response(req)

    with pytest.raises(RequestStatusUnknown):
        make(handler, clock).signed_order_request(method, "/api/v3/order", {"symbol": "BTCUSDT"})  # type: ignore[arg-type]
    assert calls == [method]  # 只发送了一次
    assert clock.sleeps == []


@pytest.mark.parametrize("code", [-1013, -2010, -1021, -2011])
def test_order_request_business_error_is_rejection(code: int) -> None:
    clock = Clock()
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"code": code, "msg": "rejected"})

    with pytest.raises(ExchangeError) as ei:
        make(handler, clock).signed_order_request("POST", "/api/v3/order", {"symbol": "BTCUSDT"})
    assert ei.value.code == code and len(calls) == 1


def test_order_request_429_and_418() -> None:
    clock = Clock()
    rate = RateLimitState()

    def h429(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, headers={"Retry-After": "7"}, json={"code": -1015, "msg": "Too many orders"}
        )

    with pytest.raises(RateLimitedError) as ei:
        make(h429, clock, rate).signed_order_request("POST", "/api/v3/order", {"symbol": "BTCUSDT"})
    assert ei.value.http_status == 429
    assert rate.blocked_until == pytest.approx(clock.t + 7)

    def h418(req: httpx.Request) -> httpx.Response:
        return httpx.Response(418, headers={"Retry-After": "120"})

    with pytest.raises(RateLimitedError):
        make(h418, clock, rate).signed_get("/api/v3/account")
    assert rate.banned
    with pytest.raises(RateLimitedError):  # 封禁后不再发出任何请求
        make(h418, clock, rate).signed_order_request("POST", "/api/v3/order", {"symbol": "BTCUSDT"})


def test_order_count_header_pauses_next_order() -> None:
    clock = Clock(1_800_000_003.0)  # 10 秒窗口内的第 3 秒
    rate = RateLimitState()
    rate.set_limit_from_exchange_info(
        {
            "rateLimits": [
                {"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 6000},
                {"rateLimitType": "ORDERS", "interval": "SECOND", "intervalNum": 10, "limit": 50},
                {"rateLimitType": "ORDERS", "interval": "DAY", "intervalNum": 1, "limit": 160000},
            ]
        }
    )
    assert rate.order_limits == {10: 50, 86400: 160000}

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": 1},
            headers={
                "X-MBX-ORDER-COUNT-10S": "45",
                "X-MBX-ORDER-COUNT-1D": "100",
                "X-MBX-USED-WEIGHT-1M": "10",
            },
        )

    c = make(handler, clock, rate)
    c.signed_order_request("POST", "/api/v3/order", {"symbol": "BTCUSDT"})
    assert rate.order_counts[10][0] == 45
    assert clock.sleeps == []
    c.signed_order_request("POST", "/api/v3/order", {"symbol": "BTCUSDT"})  # 45 ≥ 0.8 × 50：等到下一个窗口
    assert len(clock.sleeps) == 1 and int(clock.t) % 10 == 0
    c.signed_get("/api/v3/account")  # 查询类请求不受下单计数影响
    assert len(clock.sleeps) == 1


def test_order_wait_too_long_is_rate_limited() -> None:
    clock = Clock(1_800_000_000.0)
    rate = RateLimitState(order_limits={86400: 100})
    rate.record_order_count(86400, 95, clock.t)
    c = make(lambda req: httpx.Response(200, json={}), clock, rate)
    with pytest.raises(RateLimitedError):
        c.signed_order_request("POST", "/api/v3/order", {"symbol": "BTCUSDT"})


def test_signed_request_without_credentials_fails() -> None:
    clock = Clock()
    c = make(lambda req: httpx.Response(200, json={}), clock, creds=None)
    with pytest.raises(ExchangeError, match="凭据"):
        c.signed_get("/api/v3/account")


def test_recv_window_bounds() -> None:
    with pytest.raises(ValueError):
        BinanceHttpClient(BASE, recv_window_ms=60001)


def test_no_secret_or_signature_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    clock = Clock()
    n = {"i": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        n["i"] += 1
        if n["i"] == 1:
            raise httpx.ReadTimeout(f"timed out {req.url}", request=req)
        return httpx.Response(503, text=f"echo {req.url}")

    c = make(handler, clock)
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    with pytest.raises(RequestStatusUnknown) as e1:
        c.signed_order_request("POST", "/api/v3/order", {"symbol": "BTCUSDT"})
    with pytest.raises(ExchangeError) as e2:
        c.signed_get("/api/v3/account")
    text = caplog.text + str(e1.value) + str(e2.value)
    assert SECRET not in text and KEY not in text
    assert "signature=" not in text.replace("signature=***", "")
