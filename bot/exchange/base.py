"""交易所接口（spec §11.2）。

策略、风控、研究引擎只依赖这里的 Protocol，不得 import `bot.exchange.binance`。
v1 只实现 MarketDataSource 与 SpotAccount；FuturesAccount、Transfer 为第二阶段预留。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

import pandas as pd

from bot.domain.types import (
    AccountType,
    Balance,
    CommissionRates,
    Fill,
    FuturesOrderRequest,
    FuturesPosition,
    OrderRequest,
    OrderResult,
    SymbolRules,
)


class MarketDataSource(Protocol):
    """live 只读公共行情（data-api.binance.vision），不需要密钥。"""

    def get_daily_klines(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """返回 [start, end] 内**已收盘**的日线，按日期升序。

        格式为 raw frame（列见 bot.data.klines.KLINE_COLUMNS）：价格与数量是交易所给出的十进制字符串，
        需要计算时用 bot.data.klines.to_float_frame 转换。
        """
        ...

    def get_exchange_info(self) -> Mapping[str, Any]: ...


class SpotAccount(Protocol):
    """执行环境（testnet / demo / live）的现货账户。"""

    account_id: str

    def get_balances(self) -> dict[str, Balance]: ...

    def get_rules(self, symbol: str) -> SymbolRules:
        """交易对不存在时抛出 ExchangeError（-1121）。"""
        ...

    def get_all_rules(self) -> Mapping[str, SymbolRules]:
        """执行环境中全部交易对的规则（含不可交易的；用 SymbolRules.is_trading 判断）。"""
        ...

    def get_commission_rates(self, symbol: str) -> CommissionRates: ...

    def get_book_ticker(self, symbol: str) -> tuple[Decimal, Decimal]:
        """返回 (bid, ask)。"""
        ...

    def get_avg_price(self, symbol: str) -> Decimal: ...

    def get_prices(self, symbols: list[str]) -> dict[str, Decimal]: ...

    def place_order(self, req: OrderRequest) -> OrderResult:
        """超时、网络中断、5xx、-1006/-1007 时抛出 UnknownOrderStatus（spec §6.6）。

        交易所明确拒绝（4xx 业务错误）抛出 ExchangeError；请求尚未发出就失败时抛出
        ExchangeUnavailableError 或 RateLimitedError（订单肯定没有发出）。
        """
        ...

    def get_order(self, symbol: str, client_order_id: str) -> OrderResult | None:
        """订单不存在（-2013）时返回 None。网络错误重试后仍失败抛出 ExchangeUnavailableError。"""
        ...

    def cancel_order(self, symbol: str, client_order_id: str) -> OrderResult:
        """不自动重试；执行状态未知时抛出 UnknownOrderStatus。"""
        ...

    def get_open_orders(self, symbol: str) -> list[OrderResult]: ...

    def get_trades(self, symbol: str, order_id: int) -> list[Fill]: ...


class FuturesAccount(Protocol):
    """第二阶段：U 本位合约账户。只定义接口，v1 不实现。"""

    account_id: str

    def get_positions(self) -> dict[str, FuturesPosition]: ...

    def get_margin_ratio(self) -> Decimal: ...

    def get_funding_rate(self, symbol: str) -> Decimal: ...

    def place_order(self, req: FuturesOrderRequest) -> OrderResult: ...


class Transfer(Protocol):
    """第二阶段：现货 ↔ U 本位合约划转。"""

    def transfer(self, asset: str, amount: Decimal, from_: AccountType, to: AccountType) -> str:
        """返回划转 ID。"""
        ...
