"""exchangeInfo 解析、过滤器校验与 Decimal 取整（spec §6.3，filters.md）。含 hypothesis 属性测试。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from bot.domain.types import Side, SymbolRules, SymbolStatus
from bot.exchange.binance.rules import (
    ceil_to_step,
    check_order,
    floor_to_step,
    is_dust,
    is_multiple,
    limit_price,
    parse_exchange_info,
    parse_symbol_rules,
    percent_price_bounds,
    round_price,
    round_qty,
)
from tests.exec_helpers import make_rules

D = Decimal

# 2026-11 testnet exchangeInfo 中 BTCUSDT 的真实过滤器
BTC_FILTERS: list[dict[str, Any]] = [
    {
        "filterType": "PRICE_FILTER",
        "minPrice": "0.01000000",
        "maxPrice": "1000000.00000000",
        "tickSize": "0.01000000",
    },
    {"filterType": "LOT_SIZE", "minQty": "0.00001000", "maxQty": "9000.00000000", "stepSize": "0.00001000"},
    {"filterType": "ICEBERG_PARTS", "limit": 100},
    {
        "filterType": "MARKET_LOT_SIZE",
        "minQty": "0.00000000",
        "maxQty": "133.97193096",
        "stepSize": "0.00000000",
    },
    {
        "filterType": "TRAILING_DELTA",
        "minTrailingAboveDelta": 10,
        "maxTrailingAboveDelta": 2000,
        "minTrailingBelowDelta": 10,
        "maxTrailingBelowDelta": 2000,
    },
    {
        "filterType": "PERCENT_PRICE_BY_SIDE",
        "bidMultiplierUp": "1.2",
        "bidMultiplierDown": "0.5",
        "askMultiplierUp": "2",
        "askMultiplierDown": "0.8",
        "avgPriceMins": 5,
    },
    {
        "filterType": "NOTIONAL",
        "minNotional": "5.00000000",
        "applyMinToMarket": True,
        "maxNotional": "9000000.00000000",
        "applyMaxToMarket": False,
        "avgPriceMins": 5,
    },
    {"filterType": "MAX_NUM_ORDERS", "maxNumOrders": 200},
]


def _sym(**kw: Any) -> dict[str, Any]:
    base = {
        "symbol": "BTCUSDT",
        "status": "TRADING",
        "baseAsset": "BTC",
        "quoteAsset": "USDT",
        "isSpotTradingAllowed": True,
        "filters": BTC_FILTERS,
    }
    base.update(kw)
    return base


def test_parse_real_btcusdt_filters() -> None:
    r = parse_symbol_rules(_sym())
    assert r.tick_size == D("0.01") and r.step_size == D("0.00001") and r.min_qty == D("0.00001")
    assert r.max_qty == D("9000") and r.min_notional == D("5") and r.max_notional == D("9000000")
    assert (r.bid_multiplier_up, r.bid_multiplier_down) == (D("1.2"), D("0.5"))
    assert (r.ask_multiplier_up, r.ask_multiplier_down) == (D("2"), D("0.8"))
    assert r.avg_price_mins == 5 and r.is_trading
    assert all(isinstance(x, Decimal) for x in (r.tick_size, r.min_price, r.max_price, r.step_size))


def test_min_notional_fallback_and_missing_filters() -> None:
    legacy = [f for f in BTC_FILTERS if f["filterType"] != "NOTIONAL"] + [
        {"filterType": "MIN_NOTIONAL", "minNotional": "10.0", "applyToMarket": True, "avgPriceMins": 5}
    ]
    assert parse_symbol_rules(_sym(filters=legacy)).min_notional == D("10")
    both = [*BTC_FILTERS, {"filterType": "MIN_NOTIONAL", "minNotional": "7"}]
    assert parse_symbol_rules(_sym(filters=both)).min_notional == D("7")  # 取较大的下限
    bare = parse_symbol_rules(_sym(filters=[]))
    assert bare.min_notional == 0 and bare.max_notional is None and bare.tick_size == 0
    assert bare.bid_multiplier_up is None


def test_legacy_percent_price_applies_to_both_sides() -> None:
    f = [x for x in BTC_FILTERS if x["filterType"] != "PERCENT_PRICE_BY_SIDE"] + [
        {"filterType": "PERCENT_PRICE", "multiplierUp": "1.1", "multiplierDown": "0.9", "avgPriceMins": 5}
    ]
    r = parse_symbol_rules(_sym(filters=f))
    assert r.bid_multiplier_up == r.ask_multiplier_up == D("1.1")
    assert r.bid_multiplier_down == r.ask_multiplier_down == D("0.9")


@pytest.mark.parametrize(
    ("status", "spot", "expected", "trading"),
    [
        ("TRADING", True, SymbolStatus.TRADING, True),
        ("BREAK", True, SymbolStatus.BREAK, False),
        ("HALT", True, SymbolStatus.HALT, False),
        ("TRADING", False, SymbolStatus.TRADING, False),
        ("PRE_TRADING", True, SymbolStatus.UNKNOWN, False),  # 未知状态一律不可交易
        (None, True, SymbolStatus.UNKNOWN, False),
    ],
)
def test_status_mapping(status: str | None, spot: bool, expected: SymbolStatus, trading: bool) -> None:
    r = parse_symbol_rules(_sym(status=status, isSpotTradingAllowed=spot))
    assert r.status is expected and r.is_trading is trading


def test_parse_exchange_info_skips_malformed() -> None:
    info = {"symbols": [_sym(), {"symbol": "BAD", "filters": []}, _sym(symbol="ETHUSDT", baseAsset="ETH")]}
    rules = parse_exchange_info(info)
    assert set(rules) == {"BTCUSDT", "ETHUSDT"}
    with pytest.raises(TypeError):  # 数值禁止经过 float
        parse_symbol_rules(_sym(filters=[{"filterType": "LOT_SIZE", "stepSize": 0.001}]))


def test_rounding_helpers() -> None:
    assert floor_to_step(D("1.23456789"), D("0.001")) == D("1.234")
    assert ceil_to_step(D("1.2341"), D("0.001")) == D("1.235")
    assert ceil_to_step(D("1.234"), D("0.001")) == D("1.234")
    assert floor_to_step(D("1.5"), D("0")) == D("1.5")  # step 为 0：不取整
    assert floor_to_step(D("7"), D("5")) == D("5")  # 非 10 的幂的 step
    assert format(floor_to_step(D("0.123456789"), D("0.00001000")), "f") == "0.12345000"
    assert is_multiple(D("0.15"), D("0.05")) and not is_multiple(D("0.151"), D("0.05"))


def test_price_rounding_direction() -> None:
    r = make_rules("BTCUSDT", "BTC")
    assert round_price(D("100.019"), Side.BUY, r) == D("100.01")  # 买价向下
    assert round_price(D("100.011"), Side.SELL, r) == D("100.02")  # 卖价向上
    assert round_qty(D("0.00019"), r) == D("0.0001")


def test_percent_price_clamp() -> None:
    r = make_rules("BTCUSDT", "BTC")
    ref = D("100.005")
    # 买价上限 ref × 1.2 = 120.006 → 收窄并向下取整到 120.00
    assert limit_price(D("130"), Side.BUY, r, ref) == (D("120.00"), True)
    # 卖价下限 ref × 0.8 = 80.004 → 收窄并向上取整到 80.01
    assert limit_price(D("50"), Side.SELL, r, ref) == (D("80.01"), True)
    assert limit_price(D("100"), Side.SELL, r, ref) == (D("100"), False)
    assert limit_price(D("100.017"), Side.BUY, r, None) == (D("100.01"), False)
    lo, hi = percent_price_bounds(r, Side.BUY, ref)
    assert lo == ref * D("0.5") and hi == ref * D("1.2")


def _names(v: list[Any]) -> set[str]:
    return {x.filter for x in v}


def test_check_order_each_filter() -> None:
    r = make_rules("BTCUSDT", "BTC", step="0.001", max_qty="10", min_notional="10", max_notional="1000")
    ref = D("100")
    assert check_order(r, Side.BUY, D("100.00"), D("0.5"), ref) == []
    assert _names(check_order(r, Side.BUY, D("100.001"), D("0.5"), ref)) == {"PRICE_FILTER"}
    assert _names(check_order(r, Side.BUY, D("0"), D("0.5"), ref)) >= {"PRICE_FILTER"}
    assert _names(check_order(r, Side.BUY, D("100"), D("0.5005"), ref)) == {"LOT_SIZE"}
    assert _names(check_order(r, Side.BUY, D("100"), D("0"), ref)) >= {"LOT_SIZE"}
    assert _names(check_order(r, Side.SELL, D("100"), D("11"), ref)) >= {"LOT_SIZE"}
    assert _names(check_order(r, Side.BUY, D("100"), D("0.05"), ref)) == {"NOTIONAL"}
    assert _names(check_order(r, Side.SELL, D("150"), D("9"), ref)) == {"NOTIONAL"}  # > maxNotional
    assert _names(check_order(r, Side.BUY, D("121"), D("0.5"), ref)) == {"PERCENT_PRICE_BY_SIDE"}
    assert _names(check_order(r, Side.SELL, D("79"), D("0.5"), ref)) == {"PERCENT_PRICE_BY_SIDE"}
    assert _names(check_order(r, Side.SELL, D("199"), D("0.5"), ref)) == set()  # 卖价上限 2 倍
    # 有 PERCENT_PRICE_BY_SIDE 却没有参考价：保守地判为不通过
    assert _names(check_order(r, Side.BUY, D("100"), D("0.5"), None)) == {"PERCENT_PRICE_BY_SIDE"}
    # 值为 0 的项不检查
    zero = make_rules(
        "X", "X", tick="0", step="0", min_qty="0", max_qty="0", min_notional="0", max_notional=None, pps=False
    )
    assert check_order(zero, Side.BUY, D("1.23456789"), D("0.000000001"), None) == []


def test_is_dust() -> None:
    r = make_rules("SOLUSDT", "SOL", step="0.01", min_qty="0.01", min_notional="5")
    assert is_dust(D("0.04"), D("100"), r)  # 4 USDT < 5
    assert is_dust(D("0.005"), D("10000"), r)  # 数量低于 minQty
    assert not is_dust(D("0.06"), D("100"), r)
    assert not is_dust(D("0"), D("100"), r)


# ---- 属性测试 ----

TICKS = st.sampled_from([D("1"), D("0.1"), D("0.01"), D("0.0001"), D("0.00000001"), D("0.5"), D("5")])
STEPS = st.sampled_from([D("1"), D("0.1"), D("0.001"), D("0.00001"), D("0.00000001"), D("0.25")])
PRICES = st.decimals(min_value=D("0.00000001"), max_value=D("1000000"), places=10, allow_nan=False)
QTYS = st.decimals(min_value=D("0"), max_value=D("100000"), places=10, allow_nan=False)
MULTS = st.sampled_from([None, D("0.5"), D("0.8"), D("0.9")])
UPS = st.sampled_from([None, D("1.1"), D("1.2"), D("2"), D("5")])


@st.composite
def rules_st(draw: st.DrawFn) -> SymbolRules:
    tick, step = draw(TICKS), draw(STEPS)
    min_qty = draw(st.sampled_from([D(0), step, step * 10]))
    return SymbolRules(
        symbol="XUSDT",
        base_asset="X",
        quote_asset="USDT",
        status=SymbolStatus.TRADING,
        tick_size=tick,
        min_price=draw(st.sampled_from([D(0), tick])),
        max_price=draw(st.sampled_from([D(0), D("1000000")])),
        step_size=step,
        min_qty=min_qty,
        max_qty=draw(st.sampled_from([D(0), D("90000")])),
        min_notional=draw(st.sampled_from([D(0), D("5"), D("10")])),
        max_notional=draw(st.sampled_from([None, D("9000000")])),
        bid_multiplier_up=draw(UPS),
        bid_multiplier_down=draw(MULTS),
        ask_multiplier_up=draw(UPS),
        ask_multiplier_down=draw(MULTS),
    )


def _oracle(r: SymbolRules, side: Side, p: Decimal, q: Decimal, ref: Decimal) -> bool:
    """独立实现的合法性判断（直接按 filters.md 逐条写）。"""
    if p <= 0 or q <= 0:
        return False
    if r.min_price and p < r.min_price:
        return False
    if r.max_price and p > r.max_price:
        return False
    if r.tick_size and (p / r.tick_size) != (p / r.tick_size).to_integral_value():
        return False
    if r.min_qty and q < r.min_qty:
        return False
    if r.max_qty and q > r.max_qty:
        return False
    if r.step_size and (q / r.step_size) != (q / r.step_size).to_integral_value():
        return False
    if p * q < r.min_notional:
        return False
    if r.max_notional is not None and p * q > r.max_notional:
        return False
    up = r.bid_multiplier_up if side is Side.BUY else r.ask_multiplier_up
    down = r.bid_multiplier_down if side is Side.BUY else r.ask_multiplier_down
    if up is not None and p > ref * up:
        return False
    return not (down is not None and p < ref * down)


@settings(max_examples=400, deadline=None)
@given(rules_st(), st.sampled_from([Side.BUY, Side.SELL]), PRICES, QTYS, PRICES)
def test_rounded_order_passes_all_filters_or_is_explicitly_rejected(
    r: SymbolRules, side: Side, raw_p: Decimal, raw_q: Decimal, ref: Decimal
) -> None:
    p, _ = limit_price(raw_p, side, r, ref)
    q = round_qty(raw_q, r)
    violations = check_order(r, side, p, q, ref)
    assert (violations == []) == _oracle(r, side, p, q, ref)
    if not violations:
        assert is_multiple(p, r.tick_size) and is_multiple(q, r.step_size)


@settings(max_examples=400, deadline=None)
@given(rules_st(), PRICES)
def test_buy_rounds_down_sell_rounds_up(r: SymbolRules, raw: Decimal) -> None:
    buy = round_price(raw, Side.BUY, r)
    sell = round_price(raw, Side.SELL, r)
    assert buy <= raw <= sell
    assert raw - buy < r.tick_size and sell - raw < r.tick_size
    assert is_multiple(buy, r.tick_size) and is_multiple(sell, r.tick_size)


@settings(max_examples=300, deadline=None)
@given(rules_st(), QTYS)
def test_qty_always_floors_to_step(r: SymbolRules, raw: Decimal) -> None:
    q = round_qty(raw, r)
    assert q <= raw and raw - q < r.step_size and is_multiple(q, r.step_size)


@settings(max_examples=400, deadline=None)
@given(rules_st(), st.sampled_from([Side.BUY, Side.SELL]), PRICES, PRICES)
def test_clamped_price_stays_inside_percent_band(
    r: SymbolRules, side: Side, raw: Decimal, ref: Decimal
) -> None:
    p, clamped = limit_price(raw, side, r, ref)
    lo, hi = percent_price_bounds(r, side, ref)
    # 区间内存在合法 tick 时，收窄后的价格一定落在区间内
    if lo is not None and hi is not None:
        assume(ceil_to_step(lo, r.tick_size) <= floor_to_step(hi, r.tick_size))
    if hi is not None:
        assert p <= hi
    if lo is not None:
        assert p >= lo
    assert is_multiple(p, r.tick_size)
    if clamped:
        assert (hi is not None and round_price(raw, side, r) > hi) or (
            lo is not None and round_price(raw, side, r) < lo
        )
