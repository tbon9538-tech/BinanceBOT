"""BinanceSpotAccount：每个接口的正常响应与错误码映射（dev_plan M5-2）。全部 mock。"""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest

from bot.domain.errors import ExchangeError, ExchangeUnavailableError, UnknownOrderStatus
from bot.domain.types import (
    Env,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    SymbolStatus,
    TimeInForce,
)
from bot.exchange.binance.credentials import ApiCredentials, HmacSigner, KeyType
from bot.exchange.binance.spot_account import ENV_BASE_URLS, BinanceSpotAccount, LiveTradingNotAllowed
from tests.test_binance_client import Clock
from tests.test_rules_filters import BTC_FILTERS

D = Decimal
CREDS = ApiCredentials("k" * 64, KeyType.HMAC, HmacSigner("s" * 64))
CID = "mom1-261102-3f9a1c2b-B007-1"

EXCHANGE_INFO = {
    "rateLimits": [
        {"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 6000},
        {"rateLimitType": "ORDERS", "interval": "SECOND", "intervalNum": 10, "limit": 50},
    ],
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
            "filters": BTC_FILTERS,
        },
        {"symbol": "LUNAUSDT", "status": "BREAK", "baseAsset": "LUNA", "quoteAsset": "USDT", "filters": []},
    ],
}

FULL_RESPONSE = {
    "symbol": "BTCUSDT",
    "orderId": 28,
    "orderListId": -1,
    "clientOrderId": CID,
    "transactTime": 1507725176595,
    "price": "50150.00",
    "origQty": "0.01000",
    "executedQty": "0.00800",
    "origQuoteOrderQty": "0.000000",
    "cummulativeQuoteQty": "400.10000000",
    "status": "EXPIRED",
    "timeInForce": "IOC",
    "type": "LIMIT",
    "side": "BUY",
    "workingTime": 1507725176595,
    "selfTradePreventionMode": "NONE",
    "expiryReason": "INSUFFICIENT_LIQUIDITY",
    "fills": [
        {
            "price": "50000.00",
            "qty": "0.00500",
            "commission": "0.00000500",
            "commissionAsset": "BTC",
            "tradeId": 56,
        },
        {
            "price": "50033.33",
            "qty": "0.00300",
            "commission": "0.00000300",
            "commissionAsset": "BTC",
            "tradeId": 57,
        },
    ],
}

Handler = Callable[[httpx.Request], httpx.Response]


def make_account(
    routes: dict[tuple[str, str], Any], calls: list[httpx.Request] | None = None
) -> BinanceSpotAccount:
    clock = Clock()

    def handler(req: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(req)
        if req.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": int(clock.t * 1000)})
        route = routes[(req.method, req.url.path)]
        if callable(route):
            out: httpx.Response = route(req)
            return out
        return httpx.Response(200, json=route)

    return BinanceSpotAccount(
        Env.TESTNET,
        CREDS,
        account_id="spot-main",
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        now=clock.now,
    )


def _params(req: httpx.Request) -> dict[str, str]:
    return dict(parse_qsl(req.url.query.decode("ascii")))


def _order_req() -> OrderRequest:
    return OrderRequest(
        "BTCUSDT", Side.BUY, OrderType.LIMIT, D("0.01000"), CID, D("50150.00"), TimeInForce.IOC
    )


def test_live_requires_explicit_flag() -> None:
    with pytest.raises(LiveTradingNotAllowed):
        BinanceSpotAccount(Env.LIVE, CREDS, account_id="spot-main")
    acct = BinanceSpotAccount("live", CREDS, account_id="spot-main", allow_live=True)  # 只构造，不发请求
    assert acct.client.base_url == "https://api.binance.com"
    assert ENV_BASE_URLS[Env.TESTNET] == "https://testnet.binance.vision"
    assert ENV_BASE_URLS[Env.DEMO] == "https://demo-api.binance.com"
    assert BinanceSpotAccount(Env.DEMO, CREDS, account_id="x").client.base_url == ENV_BASE_URLS[Env.DEMO]


def test_rules_cached_and_rate_limits_loaded() -> None:
    calls: list[httpx.Request] = []
    acct = make_account({("GET", "/api/v3/exchangeInfo"): EXCHANGE_INFO}, calls)
    r = acct.get_rules("BTCUSDT")
    assert r.tick_size == D("0.01") and r.is_trading
    assert acct.get_rules("LUNAUSDT").status is SymbolStatus.BREAK
    assert set(acct.get_all_rules()) == {"BTCUSDT", "LUNAUSDT"}
    assert sum(1 for c in calls if c.url.path == "/api/v3/exchangeInfo") == 1
    assert acct.client.rate.order_limits == {10: 50}
    with pytest.raises(ExchangeError) as ei:
        acct.get_rules("NOPEUSDT")
    assert ei.value.code == -1121


def test_market_endpoints() -> None:
    calls: list[httpx.Request] = []
    acct = make_account(
        {
            ("GET", "/api/v3/ticker/bookTicker"): {
                "symbol": "BTCUSDT",
                "bidPrice": "50000.01",
                "bidQty": "1",
                "askPrice": "50000.02",
                "askQty": "2",
            },
            ("GET", "/api/v3/avgPrice"): {"mins": 5, "price": "49999.12345678", "closeTime": 1},
            ("GET", "/api/v3/ticker/price"): [
                {"symbol": "BTCUSDT", "price": "50000.00"},
                {"symbol": "ETHUSDT", "price": "3000.10"},
            ],
        },
        calls,
    )
    assert acct.get_book_ticker("BTCUSDT") == (D("50000.01"), D("50000.02"))
    assert acct.get_avg_price("BTCUSDT") == D("49999.12345678")
    assert acct.get_prices(["ETHUSDT", "BTCUSDT"]) == {"BTCUSDT": D("50000.00"), "ETHUSDT": D("3000.10")}
    assert acct.get_prices([]) == {}
    price_req = next(c for c in calls if c.url.path == "/api/v3/ticker/price")
    assert json.loads(price_req.url.params["symbols"]) == ["BTCUSDT", "ETHUSDT"]
    assert all("X-MBX-APIKEY" not in c.headers for c in calls)  # 公开接口不带 Key


def test_public_network_error_maps_to_unavailable() -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=req)

    acct = make_account({("GET", "/api/v3/avgPrice"): boom})
    with pytest.raises(ExchangeUnavailableError):
        acct.get_avg_price("BTCUSDT")


def test_balances_and_commission() -> None:
    calls: list[httpx.Request] = []
    acct = make_account(
        {
            ("GET", "/api/v3/account"): {
                "balances": [
                    {"asset": "BTC", "free": "0.12345678", "locked": "0.00000000"},
                    {"asset": "USDT", "free": "100.5", "locked": "1.5"},
                    {"asset": "DUST", "free": "0", "locked": "0"},
                ]
            },
            ("GET", "/api/v3/account/commission"): {
                "symbol": "BTCUSDT",
                "standardCommission": {"maker": "0.001", "taker": "0.001", "buyer": "0", "seller": "0"},
                "taxCommission": {"maker": "0", "taker": "0.0001", "buyer": "0", "seller": "0"},
                "specialCommission": {"maker": "0", "taker": "0", "buyer": "0", "seller": "0"},
                "discount": {
                    "enabledForAccount": True,
                    "enabledForSymbol": False,
                    "discountAsset": "BNB",
                    "discount": "0.75",
                },
            },
        },
        calls,
    )
    acct.sync_time()
    bals = acct.get_balances()
    assert bals["BTC"].free == D("0.12345678") and bals["USDT"].total == D("102.0")
    assert "DUST" not in bals
    acct_req = next(c for c in calls if c.url.path == "/api/v3/account")
    assert _params(acct_req)["omitZeroBalances"] == "true" and "signature" in _params(acct_req)
    rates = acct.get_commission_rates("BTCUSDT")
    assert rates.rate(Side.BUY) == D("0.0011") and not rates.discount_enabled


def test_place_order_params_and_full_response() -> None:
    calls: list[httpx.Request] = []
    acct = make_account({("POST", "/api/v3/order"): FULL_RESPONSE}, calls)
    res = acct.place_order(_order_req())
    req = next(c for c in calls if c.method == "POST")
    p = _params(req)
    assert p["type"] == "LIMIT" and p["timeInForce"] == "IOC" and p["newOrderRespType"] == "FULL"
    assert p["quantity"] == "0.01000" and p["price"] == "50150.00" and p["newClientOrderId"] == CID
    assert p["side"] == "BUY" and p["symbol"] == "BTCUSDT"
    assert res.status is OrderStatus.EXPIRED and res.executed_qty == D("0.008") and res.order_id == 28
    assert res.expiry_reason == "INSUFFICIENT_LIQUIDITY"
    assert [f.trade_id for f in res.fills] == [56, 57]
    assert res.fills[0].commission == D("0.000005") and res.fills[0].commission_asset == "BTC"
    assert res.fills[1].quote_qty == D("50033.33") * D("0.003")
    assert res.fills[0].time.year == 2017 and res.fills[0].side is Side.BUY


@pytest.mark.parametrize(
    "response",
    [
        lambda req: (_ for _ in ()).throw(httpx.ReadTimeout("t", request=req)),
        lambda req: httpx.Response(503, json={"code": -1008, "msg": "busy"}),
        lambda req: httpx.Response(400, json={"code": -1007, "msg": "Timeout waiting for response"}),
        lambda req: httpx.Response(200, json={"symbol": "BTCUSDT"}),  # 无法解析
    ],
    ids=["timeout", "5xx", "-1007", "unparseable"],
)
def test_place_order_unknown_status(response: Handler) -> None:
    calls: list[httpx.Request] = []
    acct = make_account({("POST", "/api/v3/order"): response}, calls)
    with pytest.raises(UnknownOrderStatus) as ei:
        acct.place_order(_order_req())
    assert ei.value.client_order_id == CID and ei.value.symbol == "BTCUSDT"
    assert sum(1 for c in calls if c.method == "POST") == 1


@pytest.mark.parametrize("code", [-1013, -2010])
def test_place_order_rejected(code: int) -> None:
    acct = make_account(
        {
            ("POST", "/api/v3/order"): lambda req: httpx.Response(
                400, json={"code": code, "msg": "Filter failure"}
            )
        }
    )
    with pytest.raises(ExchangeError) as ei:
        acct.place_order(_order_req())
    assert ei.value.code == code


def test_get_order_found_missing_and_error() -> None:
    order = {k: v for k, v in FULL_RESPONSE.items() if k != "fills"} | {"status": "FILLED", "updateTime": 1}
    acct = make_account({("GET", "/api/v3/order"): order})
    res = acct.get_order("BTCUSDT", CID)
    assert res is not None and res.status is OrderStatus.FILLED and res.fills == ()

    missing = make_account(
        {
            ("GET", "/api/v3/order"): lambda r: httpx.Response(
                400, json={"code": -2013, "msg": "Order does not exist."}
            )
        }
    )
    assert missing.get_order("BTCUSDT", CID) is None

    other = make_account(
        {
            ("GET", "/api/v3/order"): lambda r: httpx.Response(
                400, json={"code": -1100, "msg": "Illegal chars"}
            )
        }
    )
    with pytest.raises(ExchangeError):
        other.get_order("BTCUSDT", CID)


def test_get_order_uses_orig_client_order_id() -> None:
    calls: list[httpx.Request] = []
    acct = make_account(
        {("GET", "/api/v3/order"): lambda r: httpx.Response(400, json={"code": -2013, "msg": "x"})}, calls
    )
    acct.get_order("BTCUSDT", CID)
    p = _params(next(c for c in calls if c.url.path == "/api/v3/order"))
    assert p["origClientOrderId"] == CID and p["symbol"] == "BTCUSDT"


def test_cancel_and_open_orders_and_trades() -> None:
    cancel = {
        "symbol": "BTCUSDT",
        "orderId": 4,
        "origClientOrderId": "mom1-old-1",
        "clientOrderId": "cancelMyOrder1",
        "transactTime": 1684804350068,
        "price": "2.0",
        "origQty": "1.0",
        "executedQty": "0.1",
        "cummulativeQuoteQty": "0.2",
        "status": "CANCELED",
        "timeInForce": "GTC",
        "type": "LIMIT",
        "side": "BUY",
    }
    open_order = dict(cancel, clientOrderId="mom1-old-1", status="NEW", executedQty="0")
    trades = [
        {
            "symbol": "BTCUSDT",
            "id": 9,
            "orderId": 4,
            "orderListId": -1,
            "price": "2.0",
            "qty": "0.1",
            "quoteQty": "0.2",
            "commission": "0.0002",
            "commissionAsset": "USDT",
            "time": 1499865549590,
            "isBuyer": False,
            "isMaker": False,
            "isBestMatch": True,
        },
    ]
    calls: list[httpx.Request] = []
    acct = make_account(
        {
            ("DELETE", "/api/v3/order"): cancel,
            ("GET", "/api/v3/openOrders"): [open_order],
            ("GET", "/api/v3/myTrades"): trades,
        },
        calls,
    )
    res = acct.cancel_order("BTCUSDT", "mom1-old-1")
    assert res.client_order_id == "mom1-old-1" and res.status is OrderStatus.CANCELED
    assert acct.get_open_orders("BTCUSDT")[0].status is OrderStatus.NEW
    fills = acct.get_trades("BTCUSDT", 4)
    assert (
        fills[0].side is Side.SELL and fills[0].quote_qty == D("0.2") and fills[0].commission_asset == "USDT"
    )
    trade_req = next(c for c in calls if c.url.path == "/api/v3/myTrades")
    assert _params(trade_req)["orderId"] == "4"


def test_cancel_unknown_status() -> None:
    acct = make_account({("DELETE", "/api/v3/order"): lambda r: httpx.Response(502, text="bad gateway")})
    with pytest.raises(UnknownOrderStatus):
        acct.cancel_order("BTCUSDT", "mom1-x")
