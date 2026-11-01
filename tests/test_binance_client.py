from __future__ import annotations

from datetime import date

import httpx
import pytest

from bot.domain.errors import ExchangeError, RateLimitedError
from bot.exchange.binance.client import BinanceHttpClient, RateLimitState
from bot.exchange.binance.market_data import BinanceMarketData
from tests.helpers import day_ms, kline_row

BASE = "https://data-api.binance.vision"


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def make_client(handler, clock: Clock, rate: RateLimitState | None = None) -> BinanceHttpClient:  # type: ignore[no-untyped-def]
    return BinanceHttpClient(
        BASE, transport=httpx.MockTransport(handler), sleep=clock.sleep, now=clock.now, rate_state=rate
    )


def test_rejects_unlisted_host() -> None:
    with pytest.raises(ValueError, match="不允许"):
        BinanceHttpClient("https://evil.example.com")
    with pytest.raises(ValueError):
        BinanceHttpClient("http://data-api.binance.vision")


def test_used_weight_above_80pct_pauses_until_next_minute() -> None:
    clock = Clock(1_800_000_010.0)  # 一分钟内的第 10 秒附近
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        return httpx.Response(200, json={}, headers={"X-MBX-USED-WEIGHT-1M": "4900"})

    c = make_client(handler, clock)
    c.get("/api/v3/ping")
    assert clock.sleeps == []
    c.get("/api/v3/ping")  # 4900 ≥ 0.8 × 6000 → 等到下一分钟
    assert len(clock.sleeps) == 1
    assert int(clock.t) % 60 == 0
    assert len(calls) == 2


def test_limit_read_from_exchange_info() -> None:
    rate = RateLimitState()
    rate.set_limit_from_exchange_info(
        {
            "rateLimits": [
                {"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 1200}
            ]
        }
    )
    assert rate.weight_limit_1m == 1200


def test_429_blocks_process_until_retry_after() -> None:
    clock = Clock()
    state = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "30"})
        return httpx.Response(200, json={"ok": 1})

    rate = RateLimitState()
    c = make_client(handler, clock, rate)
    with pytest.raises(RateLimitedError) as ei:
        c.get("/api/v3/ping")
    assert ei.value.http_status == 429
    # 共享同一个限频状态的另一个客户端也要等待
    c2 = make_client(handler, clock, rate)
    assert c2.get("/api/v3/ping") == {"ok": 1}
    assert clock.sleeps == [30.0]


def test_418_bans_all_further_requests() -> None:
    clock = Clock()
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(418, headers={"Retry-After": "120"})

    c = make_client(handler, clock)
    with pytest.raises(RateLimitedError):
        c.get("/api/v3/ping")
    with pytest.raises(RateLimitedError):
        c.get("/api/v3/ping")
    assert len(calls) == 1  # 第二次请求没有真正发出


def test_5xx_retried_then_raises() -> None:
    clock = Clock()
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, json={"code": -1008, "msg": "busy"})

    with pytest.raises(ExchangeError) as ei:
        make_client(handler, clock).get("/api/v3/ping")
    assert ei.value.code == -1008
    assert len(calls) == 4
    assert clock.sleeps == [1.0, 2.0, 4.0]


def test_network_error_retried_then_succeeds() -> None:
    clock = Clock()
    state = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] < 3:
            raise httpx.ConnectError("boom", request=req)
        return httpx.Response(200, json=[1])

    assert make_client(handler, clock).get("/api/v3/ping") == [1]
    assert clock.sleeps == [1.0, 2.0]


def test_4xx_maps_error_code_without_retry() -> None:
    clock = Clock()
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})

    with pytest.raises(ExchangeError) as ei:
        make_client(handler, clock).get("/api/v3/klines")
    assert ei.value.code == -1121
    assert len(calls) == 1


def test_klines_paginate_and_drop_unclosed() -> None:
    clock = Clock()
    start = date(2020, 1, 1)
    days = [date.fromordinal(start.toordinal() + i) for i in range(1500)]
    server_ms = day_ms(days[-1]) + 3_600_000  # 最后一天开盘后 1 小时：最后一根未收盘
    seen_starts = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": server_ms})
        q = req.url.params
        st = int(q["startTime"])
        seen_starts.append(st)
        rows = [kline_row(d, 100.0) for d in days if day_ms(d) >= st][: int(q["limit"])]
        return httpx.Response(200, json=rows)

    md = BinanceMarketData(make_client(handler, clock))
    df = md.get_daily_klines("BTCUSDT", start, days[-1])
    assert len(seen_starts) == 2  # 1000 + 500
    assert len(df) == 1499  # 未收盘的最后一根被丢弃
    assert df["date"].iloc[0] == "2020-01-01"
    assert df["date"].iloc[-1] == days[-2].isoformat()
    assert df["close"].iloc[0] == "100.00000000"  # 价格保持字符串
