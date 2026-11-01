"""exchangeInfo → SymbolRules，过滤器校验与 Decimal 取整（spec §6.3，GH/filters.md）。

- 本模块负责解析 exchangeInfo（纯函数，不做 I/O）。
- 取整与校验的实现在 `bot.domain.filters`（订单计划与 SimSpotAccount 也要用，而它们不得
  import `bot.exchange.binance`），这里重新导出，便于 exchange 层统一使用。
- MIN_NOTIONAL 已无交易对使用，只做解析容错；同时存在时取较大的下限。
- 状态不在官方枚举内的交易对按不可交易处理（SymbolStatus.UNKNOWN）；
  `isSpotTradingAllowed == false` 同样视为不可交易。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from bot.domain.filters import (
    FilterViolation,
    ceil_to_step,
    check_order,
    floor_to_step,
    has_percent_price,
    is_dust,
    is_multiple,
    limit_price,
    percent_price_bounds,
    round_price,
    round_qty,
)
from bot.domain.types import SymbolRules, SymbolStatus

__all__ = [
    "FilterViolation",
    "ceil_to_step",
    "check_order",
    "floor_to_step",
    "has_percent_price",
    "is_dust",
    "is_multiple",
    "limit_price",
    "parse_exchange_info",
    "parse_status",
    "parse_symbol_rules",
    "percent_price_bounds",
    "round_price",
    "round_qty",
]

log = logging.getLogger(__name__)

_ZERO = Decimal(0)


# ---- 解析 ----


def _dec(raw: Any, default: Decimal = _ZERO) -> Decimal:
    if raw is None:
        return default
    if isinstance(raw, float):
        raise TypeError("exchangeInfo 数值必须以字符串解析，禁止经过 float")
    return Decimal(str(raw))


def _opt_positive(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    v = _dec(raw)
    return v if v > 0 else None


def parse_status(raw: Any) -> SymbolStatus:
    try:
        return SymbolStatus(str(raw))
    except ValueError:
        log.warning("交易对状态 %r 不在官方枚举内，按不可交易处理", raw)
        return SymbolStatus.UNKNOWN


def parse_symbol_rules(sym: Mapping[str, Any]) -> SymbolRules:
    """解析 exchangeInfo.symbols 中的一项。缺少的过滤器按"不检查"处理。"""
    filters = {f.get("filterType"): f for f in sym.get("filters", [])}
    price = filters.get("PRICE_FILTER", {})
    lot = filters.get("LOT_SIZE", {})
    # NOTIONAL 优先；MIN_NOTIONAL 已无交易对使用，只做解析容错。两者都在时取较大的下限（更保守）
    min_notional = _ZERO
    max_notional: Decimal | None = None
    if "NOTIONAL" in filters:
        min_notional = _dec(filters["NOTIONAL"].get("minNotional"))
        max_notional = _opt_positive(filters["NOTIONAL"].get("maxNotional"))
    if "MIN_NOTIONAL" in filters:
        min_notional = max(min_notional, _dec(filters["MIN_NOTIONAL"].get("minNotional")))
    pps = filters.get("PERCENT_PRICE_BY_SIDE")
    avg_mins: int | None = None
    if pps is not None:
        bid_up = _opt_positive(pps.get("bidMultiplierUp"))
        bid_down = _opt_positive(pps.get("bidMultiplierDown"))
        ask_up = _opt_positive(pps.get("askMultiplierUp"))
        ask_down = _opt_positive(pps.get("askMultiplierDown"))
        avg_mins = int(pps["avgPriceMins"]) if pps.get("avgPriceMins") is not None else None
    elif "PERCENT_PRICE" in filters:
        # 旧版过滤器：买卖两侧用同一个区间（保守地按同样规则校验）
        pp = filters["PERCENT_PRICE"]
        bid_up = ask_up = _opt_positive(pp.get("multiplierUp"))
        bid_down = ask_down = _opt_positive(pp.get("multiplierDown"))
        avg_mins = int(pp["avgPriceMins"]) if pp.get("avgPriceMins") is not None else None
    else:
        bid_up = bid_down = ask_up = ask_down = None
    return SymbolRules(
        symbol=str(sym["symbol"]),
        base_asset=str(sym["baseAsset"]),
        quote_asset=str(sym["quoteAsset"]),
        status=parse_status(sym.get("status")),
        tick_size=_dec(price.get("tickSize")),
        min_price=_dec(price.get("minPrice")),
        max_price=_dec(price.get("maxPrice")),
        step_size=_dec(lot.get("stepSize")),
        min_qty=_dec(lot.get("minQty")),
        max_qty=_dec(lot.get("maxQty")),
        min_notional=min_notional,
        max_notional=max_notional,
        bid_multiplier_up=bid_up,
        bid_multiplier_down=bid_down,
        ask_multiplier_up=ask_up,
        ask_multiplier_down=ask_down,
        avg_price_mins=avg_mins,
        spot_allowed=bool(sym.get("isSpotTradingAllowed", True)),
    )


def parse_exchange_info(info: Mapping[str, Any]) -> dict[str, SymbolRules]:
    """symbol → SymbolRules。单个交易对解析失败时跳过并记录 WARN（该交易对视为不可交易）。"""
    out: dict[str, SymbolRules] = {}
    for sym in info.get("symbols", []):
        try:
            rules = parse_symbol_rules(sym)
        except (KeyError, ValueError, TypeError, ArithmeticError) as e:
            log.warning("exchangeInfo 中 %s 解析失败，视为不可交易：%s", sym.get("symbol"), e)
            continue
        out[rules.symbol] = rules
    return out
