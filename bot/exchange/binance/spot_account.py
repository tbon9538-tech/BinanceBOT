"""SpotAccount 的币安实现（testnet / demo / live，spec §6.1、§11.2）。

- 只用 spec §6.1 列出的接口；下单一律 `newOrderRespType=FULL`。
- 下单超时、网络中断、5xx、-1006/-1007 → UnknownOrderStatus；查单 -2013 → None。
- live 环境必须显式传入 allow_live=True 才能构造（编排层只在 spec §13 的确认全部通过后才传）。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from bot.domain.errors import ExchangeError, ExchangeUnavailableError, UnknownOrderStatus
from bot.domain.types import (
    Balance,
    CommissionRates,
    Env,
    Fill,
    OrderRequest,
    OrderResult,
    OrderStatus,
    Side,
    SymbolRules,
)
from bot.exchange.binance.client import (
    BinanceHttpClient,
    RateLimitState,
    RequestStatusUnknown,
    format_param,
)
from bot.exchange.binance.credentials import ApiCredentials
from bot.exchange.binance.rules import parse_exchange_info

log = logging.getLogger(__name__)

# spec §0：交易与账户 REST（2026-11-01 核实）
ENV_BASE_URLS: Mapping[Env, str] = {
    Env.TESTNET: "https://testnet.binance.vision",
    Env.DEMO: "https://demo-api.binance.com",
    Env.LIVE: "https://api.binance.com",
}

NO_SUCH_ORDER = -2013
INVALID_SYMBOL = -1121
MY_TRADES_LIMIT = 1000


class LiveTradingNotAllowed(RuntimeError):
    """未经确认就试图构造 live 账户（spec §13，开发阶段禁止 live）。"""


def _dec(raw: Any) -> Decimal:
    if isinstance(raw, float):
        raise TypeError("交易所数值必须以字符串解析，禁止经过 float")
    return Decimal(str(raw))


def _ms_to_dt(ms: Any) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC)


def parse_order(body: Mapping[str, Any], *, cancel: bool = False) -> OrderResult:
    """解析下单 / 查单 / 撤单响应。

    撤单响应中 clientOrderId 是撤单请求的 ID，原订单的 ID 在 origClientOrderId。
    """
    symbol = str(body["symbol"])
    side = Side(str(body["side"]))
    order_id = int(body["orderId"]) if body.get("orderId") is not None else None
    ts_raw = body.get("transactTime") or body.get("updateTime") or body.get("time")
    ts = _ms_to_dt(ts_raw) if ts_raw is not None else datetime.now(UTC)
    fills = []
    for f in body.get("fills", []) or []:
        price, qty = _dec(f["price"]), _dec(f["qty"])
        fills.append(
            Fill(
                symbol=symbol,
                trade_id=int(f["tradeId"]),
                order_id=order_id if order_id is not None else -1,
                side=side,
                price=price,
                qty=qty,
                quote_qty=price * qty,
                commission=_dec(f["commission"]),
                commission_asset=str(f["commissionAsset"]),
                time=ts,
            )
        )
    cid_key = "origClientOrderId" if cancel else "clientOrderId"
    return OrderResult(
        symbol=symbol,
        client_order_id=str(body.get(cid_key) or body["clientOrderId"]),
        status=OrderStatus(str(body["status"])),
        executed_qty=_dec(body["executedQty"]),
        cumm_quote_qty=_dec(body["cummulativeQuoteQty"]),
        order_id=order_id,
        fills=tuple(fills),
        expiry_reason=str(body["expiryReason"]) if body.get("expiryReason") else None,
        raw=dict(body),
    )


def parse_trade(t: Mapping[str, Any]) -> Fill:
    """解析 myTrades 的一项。"""
    return Fill(
        symbol=str(t["symbol"]),
        trade_id=int(t["id"]),
        order_id=int(t["orderId"]),
        side=Side.BUY if t.get("isBuyer") else Side.SELL,
        price=_dec(t["price"]),
        qty=_dec(t["qty"]),
        quote_qty=_dec(t["quoteQty"]),
        commission=_dec(t["commission"]),
        commission_asset=str(t["commissionAsset"]),
        time=_ms_to_dt(t["time"]),
    )


def parse_commission(body: Mapping[str, Any]) -> CommissionRates:
    def part(key: str) -> dict[str, Decimal]:
        return {k: _dec(v) for k, v in (body.get(key) or {}).items()}

    discount = body.get("discount") or {}
    return CommissionRates(
        symbol=str(body["symbol"]),
        standard=part("standardCommission"),
        tax=part("taxCommission"),
        special=part("specialCommission"),
        discount_enabled=bool(discount.get("enabledForAccount")) and bool(discount.get("enabledForSymbol")),
    )


class BinanceSpotAccount:
    """执行环境的现货账户。exchangeInfo 在第一次需要时拉取并缓存（weight 20）。"""

    def __init__(
        self,
        env: Env | str,
        credentials: ApiCredentials,
        *,
        account_id: str,
        allow_live: bool = False,
        recv_window_ms: int = 5000,
        timeout_sec: float = 10.0,
        rate_state: RateLimitState | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        env = Env(env)
        if env is Env.LIVE and not allow_live:
            raise LiveTradingNotAllowed(
                "拒绝构造 live 账户：开发阶段只允许 testnet / demo（spec 开头说明第 4 条、§13）"
            )
        self.env = env
        self.account_id = account_id
        self.client = BinanceHttpClient(
            ENV_BASE_URLS[env],
            timeout_sec=timeout_sec,
            rate_state=rate_state,
            transport=transport,
            sleep=sleep,
            now=now,
            credentials=credentials,
            recv_window_ms=recv_window_ms,
        )
        self._exchange_info: Mapping[str, Any] | None = None
        self._rules: dict[str, SymbolRules] = {}
        log.info("%s 现货账户 %s，API Key %s", env.value, account_id, credentials.masked_key())

    def close(self) -> None:
        self.client.close()

    def sync_time(self) -> None:
        """强制重新计算与服务器的时钟偏移（spec §3.7：每次运行开始时调用）。"""
        self.client.sync_time()

    # ---- 公开行情（执行环境） ----

    def _public_get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        try:
            return self.client.get(path, params)
        except httpx.TransportError as e:
            raise ExchangeUnavailableError(f"GET {path} 多次网络错误：{type(e).__name__}") from e

    def load_exchange_info(self, *, force: bool = False) -> Mapping[str, Any]:
        if self._exchange_info is None or force:
            info: Mapping[str, Any] = self._public_get("/api/v3/exchangeInfo")
            self.client.rate.set_limit_from_exchange_info(info)
            self._exchange_info = info
            self._rules = parse_exchange_info(info)
        return self._exchange_info

    def get_all_rules(self) -> Mapping[str, SymbolRules]:
        self.load_exchange_info()
        return dict(self._rules)

    def get_rules(self, symbol: str) -> SymbolRules:
        self.load_exchange_info()
        try:
            return self._rules[symbol]
        except KeyError:
            raise ExchangeError(INVALID_SYMBOL, f"执行环境中没有交易对 {symbol}") from None

    def get_book_ticker(self, symbol: str) -> tuple[Decimal, Decimal]:
        body = self._public_get("/api/v3/ticker/bookTicker", {"symbol": symbol})
        return _dec(body["bidPrice"]), _dec(body["askPrice"])

    def get_avg_price(self, symbol: str) -> Decimal:
        return _dec(self._public_get("/api/v3/avgPrice", {"symbol": symbol})["price"])

    def get_prices(self, symbols: list[str]) -> dict[str, Decimal]:
        if not symbols:
            return {}
        arg = json.dumps(sorted(set(symbols)), separators=(",", ":"))
        rows = self._public_get("/api/v3/ticker/price", {"symbols": arg})
        return {str(r["symbol"]): _dec(r["price"]) for r in rows}

    # ---- 账户（签名） ----

    def get_balances(self) -> dict[str, Balance]:
        body = self.client.signed_get("/api/v3/account", {"omitZeroBalances": True})
        out: dict[str, Balance] = {}
        for b in body.get("balances", []):
            bal = Balance(str(b["asset"]), _dec(b["free"]), _dec(b["locked"]))
            if bal.total != 0:
                out[bal.asset] = bal
        return out

    def get_commission_rates(self, symbol: str) -> CommissionRates:
        return parse_commission(self.client.signed_get("/api/v3/account/commission", {"symbol": symbol}))

    def place_order(self, req: OrderRequest) -> OrderResult:
        params: dict[str, Any] = {
            "symbol": req.symbol,
            "side": req.side,
            "type": req.order_type,
        }
        if req.time_in_force is not None:
            params["timeInForce"] = req.time_in_force
        params["quantity"] = format_param(req.quantity)
        if req.price is not None:
            params["price"] = format_param(req.price)
        params["newClientOrderId"] = req.client_order_id
        params["newOrderRespType"] = "FULL"
        try:
            body = self.client.signed_order_request("POST", "/api/v3/order", params)
        except RequestStatusUnknown as e:
            raise UnknownOrderStatus(req.symbol, req.client_order_id, e.reason) from e
        try:
            return parse_order(body)
        except (KeyError, ValueError, TypeError, ArithmeticError) as e:
            # 下单已被受理但响应无法解析：同样按状态未知处理，之后用查单确认
            raise UnknownOrderStatus(req.symbol, req.client_order_id, f"无法解析下单响应：{e!r}") from e

    def get_order(self, symbol: str, client_order_id: str) -> OrderResult | None:
        try:
            body = self.client.signed_get(
                "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_order_id}
            )
        except ExchangeError as e:
            if e.code == NO_SUCH_ORDER:
                return None
            raise
        return parse_order(body)

    def cancel_order(self, symbol: str, client_order_id: str) -> OrderResult:
        try:
            body = self.client.signed_order_request(
                "DELETE", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_order_id}
            )
        except RequestStatusUnknown as e:
            raise UnknownOrderStatus(symbol, client_order_id, e.reason) from e
        return parse_order(body, cancel=True)

    def get_open_orders(self, symbol: str) -> list[OrderResult]:
        rows = self.client.signed_get("/api/v3/openOrders", {"symbol": symbol})
        return [parse_order(r) for r in rows]

    def get_trades(self, symbol: str, order_id: int) -> list[Fill]:
        rows = self.client.signed_get(
            "/api/v3/myTrades", {"symbol": symbol, "orderId": order_id, "limit": MY_TRADES_LIMIT}
        )
        return [parse_trade(t) for t in rows]
