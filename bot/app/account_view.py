"""策略视角的账户（D-062）：交易所余额减去外部基线。

账户里在策略接手之前就有的资产（testnet 预置资产、用户自己的币）记为外部基线：
- 非受管资产：每次运行把基线刷新为当前余额（它们与策略无关）；
- 受管资产：基线在策略第一次买入时冻结，之后的余额变化都归策略（外部变化会触发"无法解释的余额变化"）；
- USDT 不设基线（策略资金由 max_capital_usdt 限制）。
下单、查询等其他调用原样转发给真实账户。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal

from bot.domain.types import Balance, CommissionRates, Fill, OrderRequest, OrderResult, SymbolRules
from bot.exchange.base import SpotAccount
from bot.persistence.repo import StateRepo

QUOTE = "USDT"
_ZERO = Decimal(0)


def refresh_baselines(
    repo: StateRepo,
    account_id: str,
    strategy_id: str,
    raw_balances: Mapping[str, Balance],
    managed: set[str],
    *,
    now: datetime | None = None,
) -> dict[str, Decimal]:
    base = repo.get_external_balances(account_id, strategy_id)
    for asset, bal in raw_balances.items():
        if asset == QUOTE or asset in managed:
            continue
        if base.get(asset) != bal.total:
            repo.set_external_balance(account_id, strategy_id, asset, bal.total, now=now)
            base[asset] = bal.total
    for asset in list(base):
        if asset not in raw_balances and asset not in managed and base[asset] != 0:
            repo.set_external_balance(account_id, strategy_id, asset, _ZERO, now=now)
            base[asset] = _ZERO
    return base


class StrategyAccountView:
    def __init__(self, inner: SpotAccount, baseline: Mapping[str, Decimal]) -> None:
        self.inner = inner
        self.baseline = {a: q for a, q in baseline.items() if q > 0 and a != QUOTE}
        self.account_id = inner.account_id

    def get_balances(self) -> dict[str, Balance]:
        out: dict[str, Balance] = {}
        for asset, b in self.inner.get_balances().items():
            base = self.baseline.get(asset, _ZERO)
            if base <= 0:
                out[asset] = b
                continue
            free = max(b.free - base, _ZERO)
            rest = max(base - b.free, _ZERO)
            locked = max(b.locked - rest, _ZERO)
            if free + locked > 0:
                out[asset] = Balance(asset, free, locked)
        return out

    # ---- 原样转发 ----
    def get_rules(self, symbol: str) -> SymbolRules:
        return self.inner.get_rules(symbol)

    def get_all_rules(self) -> Mapping[str, SymbolRules]:
        return self.inner.get_all_rules()

    def get_commission_rates(self, symbol: str) -> CommissionRates:
        return self.inner.get_commission_rates(symbol)

    def get_book_ticker(self, symbol: str) -> tuple[Decimal, Decimal]:
        return self.inner.get_book_ticker(symbol)

    def get_avg_price(self, symbol: str) -> Decimal:
        return self.inner.get_avg_price(symbol)

    def get_prices(self, symbols: list[str]) -> dict[str, Decimal]:
        return self.inner.get_prices(symbols)

    def place_order(self, req: OrderRequest) -> OrderResult:
        return self.inner.place_order(req)

    def get_order(self, symbol: str, client_order_id: str) -> OrderResult | None:
        return self.inner.get_order(symbol, client_order_id)

    def cancel_order(self, symbol: str, client_order_id: str) -> OrderResult:
        return self.inner.cancel_order(symbol, client_order_id)

    def get_open_orders(self, symbol: str) -> list[OrderResult]:
        return self.inner.get_open_orders(symbol)

    def get_trades(self, symbol: str, order_id: int) -> list[Fill]:
        return self.inner.get_trades(symbol, order_id)
