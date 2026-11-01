"""领域类型（spec §11.2）。只包含纯数据类，不依赖任何 I/O。

价格、数量、金额一律使用 Decimal（spec §6.1）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

CLIENT_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,36}$")  # 按官方最严规则（spec §6.4）


class Env(StrEnum):
    TESTNET = "testnet"
    DEMO = "demo"
    LIVE = "live"


class AccountType(StrEnum):
    SPOT = "SPOT"
    USDM_FUTURES = "USDM_FUTURES"  # 第二阶段


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    LIMIT_MAKER = "LIMIT_MAKER"
    MARKET = "MARKET"


class TimeInForce(StrEnum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


class OrderStatus(StrEnum):
    """交易所订单状态。"""

    NEW = "NEW"
    PENDING_NEW = "PENDING_NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    PENDING_CANCEL = "PENDING_CANCEL"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXPIRED_IN_MATCH = "EXPIRED_IN_MATCH"

    @property
    def is_final(self) -> bool:
        """订单已结束（不会再有新的成交）。"""
        return self in _FINAL_ORDER_STATUSES


_FINAL_ORDER_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
        OrderStatus.EXPIRED_IN_MATCH,
    }
)


class IntentStatus(StrEnum):
    """本地下单意图状态（spec §6.5、§8 order_intents）。"""

    PLANNED = "PLANNED"
    SENT = "SENT"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    NOT_FOUND = "NOT_FOUND"
    CLOSED = "CLOSED"


class SymbolStatus(StrEnum):
    TRADING = "TRADING"
    END_OF_DAY = "END_OF_DAY"
    HALT = "HALT"
    BREAK = "BREAK"
    CANCEL_ONLY = "CANCEL_ONLY"
    UNKNOWN = "UNKNOWN"  # 交易所返回了文档之外的状态：一律按不可交易处理


class Regime(StrEnum):
    RISK_ON = "RISK_ON"
    RISK_OFF = "RISK_OFF"


class SystemState(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    HALTED = "HALTED"
    KILLED = "KILLED"


@dataclass(frozen=True, slots=True)
class InstrumentKey:
    """同一 symbol 在现货与合约中是两个不同的品种。"""

    account_type: AccountType
    symbol: str


@dataclass(frozen=True, slots=True)
class SymbolRules:
    """exchangeInfo 中与下单相关的规则（spec §6.3）。值为 0 的价格/数量项表示该项不检查。"""

    symbol: str
    base_asset: str
    quote_asset: str
    status: SymbolStatus
    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal
    max_notional: Decimal | None = None
    bid_multiplier_up: Decimal | None = None
    bid_multiplier_down: Decimal | None = None
    ask_multiplier_up: Decimal | None = None
    ask_multiplier_down: Decimal | None = None
    avg_price_mins: int | None = None
    spot_allowed: bool = True  # exchangeInfo.isSpotTradingAllowed

    @property
    def is_trading(self) -> bool:
        return self.status is SymbolStatus.TRADING and self.spot_allowed


@dataclass(frozen=True, slots=True)
class Balance:
    asset: str
    free: Decimal
    locked: Decimal

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


@dataclass(frozen=True, slots=True)
class OrderRequest:
    symbol: str
    side: Side
    order_type: OrderType
    quantity: Decimal
    client_order_id: str
    price: Decimal | None = None
    time_in_force: TimeInForce | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("quantity 必须为正")
        if not CLIENT_ORDER_ID_RE.match(self.client_order_id):
            raise ValueError(f"clientOrderId 不合法（只允许 [A-Za-z0-9_-]，≤36）：{self.client_order_id!r}")
        is_limit = self.order_type in (OrderType.LIMIT, OrderType.LIMIT_MAKER)
        if is_limit and (self.price is None or self.price <= 0):
            raise ValueError(f"{self.order_type} 订单必须给出正的 price")
        if self.order_type is OrderType.LIMIT and self.time_in_force is None:
            raise ValueError("LIMIT 订单必须给出 time_in_force")
        if self.order_type is not OrderType.LIMIT and self.time_in_force is not None:
            raise ValueError(f"{self.order_type} 订单不接受 time_in_force")


@dataclass(frozen=True, slots=True)
class Fill:
    symbol: str
    trade_id: int
    order_id: int
    side: Side
    price: Decimal
    qty: Decimal
    quote_qty: Decimal
    commission: Decimal
    commission_asset: str
    time: datetime


@dataclass(frozen=True, slots=True)
class OrderResult:
    symbol: str
    client_order_id: str
    status: OrderStatus
    executed_qty: Decimal
    cumm_quote_qty: Decimal
    order_id: int | None = None
    fills: tuple[Fill, ...] = ()
    expiry_reason: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CommissionRates:
    """账户在某个交易对上的实际费率（GET /api/v3/account/commission）。

    standard / tax / special 各含 maker、taker、buyer、seller 四项；
    按官方 FAQ，一笔成交的费率 = Σ(三类) 的 (maker 或 taker) + (buyer 或 seller)。
    """

    symbol: str
    standard: Mapping[str, Decimal]
    tax: Mapping[str, Decimal] = field(default_factory=dict)
    special: Mapping[str, Decimal] = field(default_factory=dict)
    discount_enabled: bool = False  # BNB 抵扣（v1 关闭，D-009）

    def rate(self, side: Side, *, maker: bool = False) -> Decimal:
        role = "maker" if maker else "taker"
        side_key = "buyer" if side is Side.BUY else "seller"
        total = Decimal(0)
        for part in (self.standard, self.tax, self.special):
            total += part.get(role, Decimal(0)) + part.get(side_key, Decimal(0))
        return total


@dataclass(frozen=True, slots=True)
class TargetPortfolio:
    """一个策略在 t 日的目标（spec §11.2）。

    targets: 账户 → 品种 → 目标。现货为占策略可用资金的权重（≥0，合计 ≤1）；
    合约预留为带符号的名义敞口比例（第二阶段）。
    """

    strategy_id: str
    as_of: date
    targets: Mapping[str, Mapping[InstrumentKey, Decimal]]

    def __post_init__(self) -> None:
        for account_id, legs in self.targets.items():
            spot = [w for k, w in legs.items() if k.account_type is AccountType.SPOT]
            if any(w < 0 for w in spot):
                raise ValueError(f"账户 {account_id} 的现货目标权重不能为负")
            if sum(spot, Decimal(0)) > 1:
                raise ValueError(f"账户 {account_id} 的现货目标权重之和超过 1")

    def total_weight(self, account_id: str) -> Decimal:
        return sum(self.targets.get(account_id, {}).values(), Decimal(0))


# ---- 第二阶段（资金费率套利）预留，v1 不使用 ----


@dataclass(frozen=True, slots=True)
class FuturesPosition:
    symbol: str
    quantity: Decimal  # 带符号：正为多，负为空
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal


@dataclass(frozen=True, slots=True)
class FuturesOrderRequest:
    symbol: str
    side: Side
    quantity: Decimal
    client_order_id: str
    price: Decimal | None = None
    reduce_only: bool = False
