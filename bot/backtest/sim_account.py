"""内存中的 SpotAccount 实现，用于编排层集成测试、执行回放与故障演练（dev_plan §6.3、M6-6）。

- 过滤器校验与实盘共用 bot.domain.filters，不通过时抛出 ExchangeError(-1013)；
- LIMIT IOC 成交模型：单档盘口，买价 ≥ ask 时按 ask 成交、卖价 ≤ bid 时按 bid 成交，
  成交量不超过盘口数量（盘口数量被消耗），未成交部分立即过期（EXPIRED）；
- 手续费从收到的资产中扣除：买单扣 base，卖单扣 quote（spec §6.5 第 4 条、§6.9）；
- 故障注入：下单前 / 下单后抛出 UnknownOrderStatus（超时、5xx、-1007）、模拟进程崩溃、
  查单在前 N 次返回 -2013（延迟可见）、查单网络错误、拒单、外部余额变化。
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from bot.domain.errors import ExchangeError, ExchangeUnavailableError, UnknownOrderStatus
from bot.domain.filters import check_order
from bot.domain.types import (
    Balance,
    CommissionRates,
    Fill,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    Side,
    SymbolRules,
    TimeInForce,
)

_ZERO = Decimal(0)
_COMMISSION_Q = Decimal("0.00000001")
INFINITE_QTY = Decimal("1e18")


class SimulatedCrash(Exception):
    """模拟进程在下单请求发出后、收到响应前被杀（测试用）。"""


class FaultKind(StrEnum):
    TIMEOUT = "timeout"  # 读超时 → UnknownOrderStatus
    HTTP_5XX = "5xx"  # HTTP 503 → UnknownOrderStatus
    STATUS_UNKNOWN = "-1007"  # 业务码 -1007 → UnknownOrderStatus
    CRASH = "crash"  # 抛出 SimulatedCrash（不是 UnknownOrderStatus）


_FAULT_REASONS = {
    FaultKind.TIMEOUT: "ReadTimeout: The read operation timed out",
    FaultKind.HTTP_5XX: "HTTP 503 [-1008] Service Unavailable",
    FaultKind.STATUS_UNKNOWN: "[-1007] Timeout waiting for response from backend server.",
    FaultKind.CRASH: "simulated crash",
}


@dataclass
class PlaceFault:
    kind: FaultKind
    executed: bool  # True：订单已在交易所执行，之后才出错；False：订单没有到达撮合
    symbol: str | None = None  # None 表示任何交易对
    times: int = 1


@dataclass
class SimBook:
    bid: Decimal
    ask: Decimal
    bid_qty: Decimal = INFINITE_QTY
    ask_qty: Decimal = INFINITE_QTY


@dataclass
class _Order:
    result: OrderResult
    side: Side


@dataclass
class SimStats:
    place_calls: int = 0
    get_order_calls: int = 0
    cancel_calls: int = 0
    accepted_cids: list[str] = field(default_factory=list)


class SimSpotAccount:
    """可注入故障的内存现货账户。所有数值为 Decimal；时间来自注入的 now()。"""

    def __init__(
        self,
        *,
        rules: Mapping[str, SymbolRules],
        balances: Mapping[str, Decimal | Balance],
        books: Mapping[str, SimBook | tuple[Decimal, Decimal]],
        avg_prices: Mapping[str, Decimal] | None = None,
        prices: Mapping[str, Decimal] | None = None,
        commission_rate: Decimal = Decimal("0.001"),
        account_id: str = "sim",
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.account_id = account_id
        self.rules: dict[str, SymbolRules] = dict(rules)
        self.balances: dict[str, Balance] = {}
        for asset, b in balances.items():
            self.balances[asset] = b if isinstance(b, Balance) else Balance(asset, b, _ZERO)
        self.books: dict[str, SimBook] = {}
        for sym, bk in books.items():
            if isinstance(bk, SimBook):
                self.set_book(sym, bk.bid, bk.ask, bk.bid_qty, bk.ask_qty)
            else:
                self.set_book(sym, bk[0], bk[1])
        self.avg_prices: dict[str, Decimal] = dict(avg_prices or {})
        self.prices: dict[str, Decimal] = dict(prices or {})
        self.commission_rate = commission_rate
        self._now = now
        self._orders: dict[str, _Order] = {}
        self._trades: dict[int, list[Fill]] = {}
        self._open_orders: dict[str, OrderResult] = {}
        self._order_ids = itertools.count(1)
        self._trade_ids = itertools.count(1)
        self._place_faults: list[PlaceFault] = []
        self._reject_next: list[tuple[int, str]] = []
        self._query_faults = 0
        self._cancel_unknown = 0
        self.hidden_queries = 0  # 每个订单的前 N 次查询返回 -2013（延迟可见）
        self._query_counts: dict[str, int] = {}
        self.placed: list[OrderRequest] = []  # 所有到达本账户的下单请求（含被拒、故障的）
        self.stats = SimStats()

    # ---- 配置与故障注入 ----

    def set_book(
        self,
        symbol: str,
        bid: Decimal,
        ask: Decimal,
        bid_qty: Decimal = INFINITE_QTY,
        ask_qty: Decimal = INFINITE_QTY,
    ) -> None:
        self.books[symbol] = SimBook(bid, ask, bid_qty, ask_qty)

    def set_avg_price(self, symbol: str, price: Decimal) -> None:
        self.avg_prices[symbol] = price

    def adjust_balance(self, asset: str, delta: Decimal) -> None:
        """外部余额变化（人工出入金、testnet 重置等）。"""
        b = self.balances.get(asset, Balance(asset, _ZERO, _ZERO))
        self.balances[asset] = Balance(asset, b.free + delta, b.locked)

    def inject_place_fault(
        self, kind: FaultKind | str, *, executed: bool, symbol: str | None = None, times: int = 1
    ) -> None:
        self._place_faults.append(PlaceFault(FaultKind(kind), executed, symbol, times))

    def reject_next_order(self, code: int = -2010, msg: str = "NEW_ORDER_REJECTED") -> None:
        self._reject_next.append((code, msg))

    def fail_next_queries(self, n: int) -> None:
        """接下来 n 次 get_order 抛出 ExchangeUnavailableError（网络错误）。"""
        self._query_faults += n

    def fail_next_cancels(self, n: int) -> None:
        """接下来 n 次 cancel_order 抛出 UnknownOrderStatus。"""
        self._cancel_unknown += n

    def add_open_order(
        self, symbol: str, client_order_id: str, side: Side, qty: Decimal, price: Decimal
    ) -> OrderResult:
        """放入一笔挂单（例如人工或旧版本留下的），用于测试撤单（spec §6.7）。"""
        res = OrderResult(
            symbol=symbol,
            client_order_id=client_order_id,
            status=OrderStatus.NEW,
            executed_qty=_ZERO,
            cumm_quote_qty=_ZERO,
            order_id=next(self._order_ids),
            raw={"symbol": symbol, "side": side.value, "price": str(price), "origQty": str(qty)},
        )
        self._open_orders[client_order_id] = res
        self._orders[client_order_id] = _Order(res, side)
        return res

    # ---- SpotAccount ----

    def get_balances(self) -> dict[str, Balance]:
        return {a: b for a, b in self.balances.items() if b.total != 0}

    def get_rules(self, symbol: str) -> SymbolRules:
        try:
            return self.rules[symbol]
        except KeyError:
            raise ExchangeError(-1121, "Invalid symbol.", 400) from None

    def get_all_rules(self) -> Mapping[str, SymbolRules]:
        return dict(self.rules)

    def get_commission_rates(self, symbol: str) -> CommissionRates:
        self.get_rules(symbol)
        r = self.commission_rate
        return CommissionRates(symbol, {"maker": r, "taker": r, "buyer": _ZERO, "seller": _ZERO})

    def get_book_ticker(self, symbol: str) -> tuple[Decimal, Decimal]:
        self.get_rules(symbol)
        b = self.books[symbol]
        return b.bid, b.ask

    def get_avg_price(self, symbol: str) -> Decimal:
        self.get_rules(symbol)
        if symbol in self.avg_prices:
            return self.avg_prices[symbol]
        b = self.books[symbol]
        return (b.bid + b.ask) / 2

    def get_prices(self, symbols: list[str]) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for s in symbols:
            if s in self.prices:
                out[s] = self.prices[s]
            elif s in self.books:
                b = self.books[s]
                out[s] = (b.bid + b.ask) / 2
        return out

    def place_order(self, req: OrderRequest) -> OrderResult:
        self.stats.place_calls += 1
        self.placed.append(req)
        fault = self._take_fault(req.symbol)
        if fault is not None and not fault.executed:
            self._raise_fault(fault, req)
        if self._reject_next:
            code, msg = self._reject_next.pop(0)
            raise ExchangeError(code, msg, 400)
        result = self._execute(req)
        if fault is not None:
            self._raise_fault(fault, req)
        return result

    def get_order(self, symbol: str, client_order_id: str) -> OrderResult | None:
        self.stats.get_order_calls += 1
        if self._query_faults > 0:
            self._query_faults -= 1
            raise ExchangeUnavailableError("GET /api/v3/order 多次网络错误：ConnectError")
        n = self._query_counts.get(client_order_id, 0) + 1
        self._query_counts[client_order_id] = n
        o = self._orders.get(client_order_id)
        if o is None or o.result.symbol != symbol or n <= self.hidden_queries:
            return None
        r = o.result
        return OrderResult(
            r.symbol,
            r.client_order_id,
            r.status,
            r.executed_qty,
            r.cumm_quote_qty,
            r.order_id,
            (),
            r.expiry_reason,
            r.raw,
        )

    def cancel_order(self, symbol: str, client_order_id: str) -> OrderResult:
        self.stats.cancel_calls += 1
        if self._cancel_unknown > 0:
            self._cancel_unknown -= 1
            raise UnknownOrderStatus(symbol, client_order_id, "ReadTimeout")
        o = self._open_orders.pop(client_order_id, None)
        if o is None or o.symbol != symbol:
            raise ExchangeError(-2011, "Unknown order sent.", 400)
        canceled = OrderResult(
            o.symbol,
            o.client_order_id,
            OrderStatus.CANCELED,
            o.executed_qty,
            o.cumm_quote_qty,
            o.order_id,
            (),
            None,
            o.raw,
        )
        self._orders[client_order_id] = _Order(canceled, self._orders[client_order_id].side)
        return canceled

    def get_open_orders(self, symbol: str) -> list[OrderResult]:
        return [o for o in self._open_orders.values() if o.symbol == symbol]

    def get_trades(self, symbol: str, order_id: int) -> list[Fill]:
        return [f for f in self._trades.get(order_id, []) if f.symbol == symbol]

    # ---- 内部 ----

    def _take_fault(self, symbol: str) -> PlaceFault | None:
        for f in self._place_faults:
            if f.symbol is None or f.symbol == symbol:
                f.times -= 1
                if f.times <= 0:
                    self._place_faults.remove(f)
                return f
        return None

    def _raise_fault(self, fault: PlaceFault, req: OrderRequest) -> None:
        if fault.kind is FaultKind.CRASH:
            raise SimulatedCrash(req.client_order_id)
        raise UnknownOrderStatus(req.symbol, req.client_order_id, _FAULT_REASONS[fault.kind])

    def _execute(self, req: OrderRequest) -> OrderResult:
        rules = self.get_rules(req.symbol)
        if not rules.is_trading:
            raise ExchangeError(-2010, "Market is closed.", 400)
        if req.client_order_id in self._orders:
            raise ExchangeError(-2010, "Duplicate order sent.", 400)
        if (
            req.order_type is not OrderType.LIMIT
            or req.time_in_force is not TimeInForce.IOC
            or req.price is None
        ):
            raise ExchangeError(-1013, "SimSpotAccount 只支持 LIMIT IOC", 400)
        held = self.balances.get(rules.base_asset)
        position = held.free + held.locked if held is not None else _ZERO
        violations = check_order(
            rules, req.side, req.price, req.quantity, self.get_avg_price(req.symbol), position=position
        )
        if violations:
            raise ExchangeError(-1013, f"Filter failure: {violations[0].filter}", 400)
        base, quote = rules.base_asset, rules.quote_asset
        if req.side is Side.BUY:
            need = req.quantity * req.price
            if self._free(quote) < need:
                raise ExchangeError(-2010, "Account has insufficient balance for requested action.", 400)
        elif self._free(base) < req.quantity:
            raise ExchangeError(-2010, "Account has insufficient balance for requested action.", 400)

        book = self.books[req.symbol]
        order_id = next(self._order_ids)
        t = self._now()
        filled = _ZERO
        fill_price = _ZERO
        if req.side is Side.BUY and req.price >= book.ask:
            filled, fill_price = min(req.quantity, book.ask_qty), book.ask
            book.ask_qty -= filled
        elif req.side is Side.SELL and req.price <= book.bid:
            filled, fill_price = min(req.quantity, book.bid_qty), book.bid
            book.bid_qty -= filled
        fills: list[Fill] = []
        quote_amt = filled * fill_price
        if filled > 0:
            if req.side is Side.BUY:
                commission = (filled * self.commission_rate).quantize(_COMMISSION_Q, ROUND_HALF_UP)
                self._add(base, filled - commission)
                self._add(quote, -quote_amt)
                c_asset = base
            else:
                commission = (quote_amt * self.commission_rate).quantize(_COMMISSION_Q, ROUND_HALF_UP)
                self._add(base, -filled)
                self._add(quote, quote_amt - commission)
                c_asset = quote
            fills.append(
                Fill(
                    req.symbol,
                    next(self._trade_ids),
                    order_id,
                    req.side,
                    fill_price,
                    filled,
                    quote_amt,
                    commission,
                    c_asset,
                    t,
                )
            )
        status = OrderStatus.FILLED if filled == req.quantity else OrderStatus.EXPIRED
        raw = {
            "symbol": req.symbol,
            "orderId": order_id,
            "clientOrderId": req.client_order_id,
            "side": req.side.value,
            "price": format(req.price, "f"),
            "origQty": format(req.quantity, "f"),
            "status": status.value,
        }
        result = OrderResult(
            symbol=req.symbol,
            client_order_id=req.client_order_id,
            status=status,
            executed_qty=filled,
            cumm_quote_qty=quote_amt,
            order_id=order_id,
            fills=tuple(fills),
            expiry_reason="INSUFFICIENT_LIQUIDITY" if status is OrderStatus.EXPIRED else None,
            raw=raw,
        )
        self._orders[req.client_order_id] = _Order(result, req.side)
        self._trades[order_id] = fills
        self.stats.accepted_cids.append(req.client_order_id)
        return result

    def _free(self, asset: str) -> Decimal:
        b = self.balances.get(asset)
        return b.free if b is not None else _ZERO

    def _add(self, asset: str, delta: Decimal) -> None:
        self.adjust_balance(asset, delta)
