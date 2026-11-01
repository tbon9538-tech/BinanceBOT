"""净值与份额（spec §5.2）。纯函数，全部使用 Decimal。

- 受管净值 NAV = USDT（free + locked）+ Σ 受管资产数量 × 执行环境价格；
- 首次运行 units = NAV（单位净值 1.0）；出入金按当时的单位净值申购 / 赎回份额；
- 无法用本系统成交解释的余额变化（> 1 USDT）需要人工登记或确认。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal

from bot.domain.types import Balance

QUOTE = "USDT"
UNEXPLAINED_TOLERANCE_USDT = Decimal("1")


@dataclass(frozen=True, slots=True)
class NavBreakdown:
    nav: Decimal
    cash: Decimal  # USDT free + locked
    positions: dict[str, Decimal]  # 资产 → 市值（USDT）
    unpriced: tuple[str, ...]  # 受管但取不到价格的资产（按 0 计入并告警）

    @property
    def gross(self) -> Decimal:
        return sum(self.positions.values(), Decimal(0)) / self.nav if self.nav > 0 else Decimal(0)


def compute_nav(
    balances: Mapping[str, Balance], prices: Mapping[str, Decimal], managed_assets: Iterable[str]
) -> NavBreakdown:
    """prices：资产 → 以 USDT 计价的价格（执行环境 ticker）。非受管资产不计入。"""
    cash = balances[QUOTE].total if QUOTE in balances else Decimal(0)
    positions: dict[str, Decimal] = {}
    unpriced: list[str] = []
    for asset in sorted(set(managed_assets) - {QUOTE}):
        bal = balances.get(asset)
        if bal is None or bal.total == 0:
            continue
        px = prices.get(asset)
        if px is None or px <= 0:
            unpriced.append(asset)
            continue
        positions[asset] = bal.total * px
    nav = cash + sum(positions.values(), Decimal(0))
    return NavBreakdown(nav=nav, cash=cash, positions=positions, unpriced=tuple(unpriced))


def nav_per_unit(nav: Decimal, units: Decimal) -> Decimal:
    if units <= 0:
        raise ValueError("份额必须为正")
    return nav / units


def initial_units(nav: Decimal) -> Decimal:
    """首次运行：单位净值定为 1.0。"""
    if nav <= 0:
        raise ValueError("首次运行时受管净值必须为正（请先在账户中存入 USDT）")
    return nav


def apply_capital_flow(units: Decimal, nav_before: Decimal, amount: Decimal, kind: str) -> Decimal:
    """出入金：按流动发生前的单位净值申购 / 赎回份额，返回新的份额数。单位净值保持不变。"""
    if amount <= 0:
        raise ValueError("金额必须为正")
    navu = nav_per_unit(nav_before, units)
    if kind == "DEPOSIT":
        return units + amount / navu
    if kind == "WITHDRAW":
        new_units = units - amount / navu
        if new_units <= 0:
            raise ValueError("提现金额超过受管净值")
        return new_units
    raise ValueError(f"未知的资金流类型：{kind}")


def unexplained_changes(
    previous: Mapping[str, Decimal],
    current: Mapping[str, Decimal],
    explained: Mapping[str, Decimal],
    prices: Mapping[str, Decimal],
    tolerance_usdt: Decimal = UNEXPLAINED_TOLERANCE_USDT,
) -> dict[str, Decimal]:
    """余额变化中无法由本系统成交（explained：资产 → 预期净变化，含手续费）解释的部分，按 USDT 计。

    previous / current：资产 → 数量（free + locked）。返回超过容差的 资产 → 差额市值。
    """
    out: dict[str, Decimal] = {}
    for asset in set(previous) | set(current) | set(explained):
        diff = (
            current.get(asset, Decimal(0))
            - previous.get(asset, Decimal(0))
            - explained.get(asset, Decimal(0))
        )
        px = Decimal(1) if asset == QUOTE else prices.get(asset, Decimal(0))
        value = abs(diff) * px
        if value > tolerance_usdt:
            out[asset] = diff * px
    return out
