"""SymbolRules 的 Decimal 取整与过滤器校验（spec §6.3，GH/filters.md）。纯函数，不做 I/O。

放在 domain 层，是为了让订单计划（execution/）和 SimSpotAccount（backtest/）不必 import
`bot.exchange.binance`（CLAUDE.md 硬性约束：只有 app/ 和 exchange/ 可以 import 它）。
`bot.exchange.binance.rules` 负责解析 exchangeInfo，并重新导出这里的函数。

- 数量一律按 stepSize 向下取整；价格买单向下、卖单向上取整到 tickSize。
- 过滤器中值为 0 的项不检查（filters.md）。
- PERCENT_PRICE_BY_SIDE：价格超出区间时收窄到边界，再向区间内侧取整。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext

from bot.domain.types import Side, SymbolRules

_PREC = 60  # 取整时使用的 Decimal 精度，避免默认 28 位在极小 step 下截断


@dataclass(frozen=True, slots=True)
class FilterViolation:
    filter: str  # PRICE_FILTER / LOT_SIZE / NOTIONAL / PERCENT_PRICE_BY_SIDE / MAX_POSITION
    detail: str

    def __str__(self) -> str:
        return f"{self.filter}: {self.detail}"


# ---- 取整 ----


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """向下取整到 step 的整数倍；step ≤ 0 表示不限制。"""
    if step <= 0:
        return value
    with localcontext() as ctx:
        ctx.prec = _PREC
        return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    """向上取整到 step 的整数倍；step ≤ 0 表示不限制。"""
    if step <= 0:
        return value
    with localcontext() as ctx:
        ctx.prec = _PREC
        return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def is_multiple(value: Decimal, step: Decimal) -> bool:
    if step <= 0:
        return True
    with localcontext() as ctx:
        ctx.prec = _PREC
        return value % step == 0


def round_qty(qty: Decimal, rules: SymbolRules) -> Decimal:
    """数量一律向下取整到 stepSize。"""
    return floor_to_step(qty, rules.step_size)


def round_price(price: Decimal, side: Side, rules: SymbolRules) -> Decimal:
    """买价向下、卖价向上取整到 tickSize（spec §6.3）。"""
    if side is Side.BUY:
        return floor_to_step(price, rules.tick_size)
    return ceil_to_step(price, rules.tick_size)


def percent_price_bounds(
    rules: SymbolRules, side: Side, ref_price: Decimal
) -> tuple[Decimal | None, Decimal | None]:
    """PERCENT_PRICE_BY_SIDE 的价格区间 (下限, 上限)；None 表示该侧不检查。"""
    if side is Side.BUY:
        down, up = rules.bid_multiplier_down, rules.bid_multiplier_up
    else:
        down, up = rules.ask_multiplier_down, rules.ask_multiplier_up
    lo = ref_price * down if down is not None else None
    hi = ref_price * up if up is not None else None
    return lo, hi


def has_percent_price(rules: SymbolRules) -> bool:
    return any(
        m is not None
        for m in (
            rules.bid_multiplier_up,
            rules.bid_multiplier_down,
            rules.ask_multiplier_up,
            rules.ask_multiplier_down,
        )
    )


def limit_price(
    price: Decimal, side: Side, rules: SymbolRules, ref_price: Decimal | None
) -> tuple[Decimal, bool]:
    """按方向取整，再按 PERCENT_PRICE_BY_SIDE 收窄到区间并向区间内侧重新取整。

    返回 (价格, 是否被收窄)。区间内没有合法 tick 时，结果仍可能不合法，交给 check_order 拒绝。
    """
    p = round_price(price, side, rules)
    if ref_price is None or ref_price <= 0:
        return p, False
    lo, hi = percent_price_bounds(rules, side, ref_price)
    if hi is not None and p > hi:
        return floor_to_step(hi, rules.tick_size), True
    if lo is not None and p < lo:
        return ceil_to_step(lo, rules.tick_size), True
    return p, False


# ---- 校验 ----


def check_order(
    rules: SymbolRules,
    side: Side,
    price: Decimal,
    qty: Decimal,
    ref_price: Decimal | None,
    *,
    position: Decimal | None = None,
) -> list[FilterViolation]:
    """按 filters.md 校验 LIMIT 订单。返回空列表表示全部通过。

    ref_price 为 avgPrice。交易对有 PERCENT_PRICE_BY_SIDE 而 ref_price 缺失时，保守地判为不通过。
    position 为**整个账户**（不扣外部基线）该基础资产的 free + locked + 未成交买单数量，只用于买单的
    MAX_POSITION；None 表示调用方不做这项检查（例如重试时数量只会变小，D-073）。
    """
    v: list[FilterViolation] = []
    if price <= 0:
        v.append(FilterViolation("PRICE_FILTER", f"价格 {price} 必须为正"))
    else:
        if rules.min_price > 0 and price < rules.min_price:
            v.append(FilterViolation("PRICE_FILTER", f"价格 {price} < minPrice {rules.min_price}"))
        if rules.max_price > 0 and price > rules.max_price:
            v.append(FilterViolation("PRICE_FILTER", f"价格 {price} > maxPrice {rules.max_price}"))
        if not is_multiple(price, rules.tick_size):
            v.append(
                FilterViolation("PRICE_FILTER", f"价格 {price} 不是 tickSize {rules.tick_size} 的整数倍")
            )
    if qty <= 0:
        v.append(FilterViolation("LOT_SIZE", f"数量 {qty} 必须为正"))
    else:
        if rules.min_qty > 0 and qty < rules.min_qty:
            v.append(FilterViolation("LOT_SIZE", f"数量 {qty} < minQty {rules.min_qty}"))
        if rules.max_qty > 0 and qty > rules.max_qty:
            v.append(FilterViolation("LOT_SIZE", f"数量 {qty} > maxQty {rules.max_qty}"))
        if not is_multiple(qty, rules.step_size):
            v.append(FilterViolation("LOT_SIZE", f"数量 {qty} 不是 stepSize {rules.step_size} 的整数倍"))
    notional = price * qty
    if rules.min_notional > 0 and notional < rules.min_notional:
        v.append(FilterViolation("NOTIONAL", f"金额 {notional} < minNotional {rules.min_notional}"))
    if rules.max_notional is not None and notional > rules.max_notional:
        v.append(FilterViolation("NOTIONAL", f"金额 {notional} > maxNotional {rules.max_notional}"))
    if has_percent_price(rules):
        if ref_price is None or ref_price <= 0:
            v.append(FilterViolation("PERCENT_PRICE_BY_SIDE", "缺少参考价 avgPrice，无法校验"))
        else:
            lo, hi = percent_price_bounds(rules, side, ref_price)
            if lo is not None and price < lo:
                v.append(FilterViolation("PERCENT_PRICE_BY_SIDE", f"价格 {price} < 下限 {lo}"))
            if hi is not None and price > hi:
                v.append(FilterViolation("PERCENT_PRICE_BY_SIDE", f"价格 {price} > 上限 {hi}"))
    cap = rules.max_position
    if side is Side.BUY and cap is not None and position is not None and position + qty > cap:
        v.append(FilterViolation("MAX_POSITION", f"买入后持仓 {position + qty} > maxPosition {cap}"))
    return v


def is_dust(qty: Decimal, price: Decimal, rules: SymbolRules) -> bool:
    """灰尘：持仓为正，但数量低于 minQty 或市值低于 minNotional，无法卖出（spec §6.9）。"""
    if qty <= 0:
        return False
    return (rules.min_qty > 0 and qty < rules.min_qty) or qty * price < rules.min_notional
