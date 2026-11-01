"""订单计划（spec §6.3，dev_plan M5-6）。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

import bot.execution.planner as planner_mod
from bot.config import ExecutionConfig, RebalanceConfig
from bot.domain.filters import check_order, is_multiple
from bot.domain.types import Balance, Side, SymbolStatus
from bot.execution.client_order_id import OrderKind
from bot.execution.planner import (
    BuyCandidate,
    count_skips,
    plan_buys,
    plan_liquidation,
    plan_sells,
    price_limit_order,
)
from bot.strategy import rebalance
from bot.strategy.rebalance import SkipReason
from tests.exec_helpers import ABS, AVG, BOOKS, EXEC, REL, RULES, make_rules

D = Decimal
C = D("10000")


def bal(**kw: str | tuple[str, str]) -> dict[str, Balance]:
    out = {}
    for asset, v in kw.items():
        free, locked = (v, "0") if isinstance(v, str) else v
        out[asset] = Balance(asset, D(free), D(locked))
    return out


def sells(targets: dict[str, float], balances: dict[str, Balance], cfg: RebalanceConfig = REL, **kw: Any):  # type: ignore[no-untyped-def]
    kw.setdefault("managed_assets", {a for a in balances if a != "USDT"} | {"USDT"})
    return plan_sells(
        targets,
        kw.pop("capital", C),
        balances,
        kw.pop("rules", RULES),
        kw.pop("books", BOOKS),
        kw.pop("avg", AVG),
        cfg,
        kw.pop("exec_cfg", EXEC),
        **kw,
    )


def reasons(plan: Any) -> dict[str, str]:
    return {s.symbol: s.reason.value for s in plan.skipped}


def test_planner_uses_the_shared_should_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    assert planner_mod.should_skip is rebalance.should_skip  # 研究引擎与订单计划共用同一个函数
    calls: list[tuple[Any, ...]] = []

    def spy(*a: Any, **k: Any) -> Any:
        calls.append(a)
        return rebalance.should_skip(*a, **k)

    monkeypatch.setattr(planner_mod, "should_skip", spy)
    plan = sells({"BTCUSDT": 0.3, "ETHUSDT": 0.2}, bal(USDT="10000", SOL="1"))
    assert len(calls) == 3  # BTC、ETH、SOL（受管持仓）各一次
    plan_buys(plan.buy_candidates, D("10000"), RULES, BOOKS, AVG, REL, EXEC)
    assert len(calls) == 5  # 缩小后的金额也经过 should_skip


@pytest.mark.parametrize(
    ("cfg", "expected"),
    [(ABS, SkipReason.TOLERANCE_BAND), (REL, None)],
)
def test_tolerance_band_modes(cfg: RebalanceConfig, expected: SkipReason | None) -> None:
    # 卫星单币：目标 3.5%，当前 2.0%：absolute 0.02 跳过；relative 0.25 × 0.035 → 交易
    eth_qty = D("200") / D("3000.5")  # 当前市值约 200 = 2%
    plan = sells({"ETHUSDT": 0.035}, bal(USDT="9800", ETH=str(eth_qty)), cfg)
    if expected is None:
        assert [c.symbol for c in plan.buy_candidates] == ["ETHUSDT"]
    else:
        assert reasons(plan) == {"ETHUSDT": expected.value}


def test_relative_band_skips_small_relative_change() -> None:
    eth_qty = D("300") / D("3000.5")  # 当前 3.0%，目标 3.5%：差 0.005 < 0.25 × 0.035
    plan = sells({"ETHUSDT": 0.035}, bal(USDT="9700", ETH=str(eth_qty)), REL)
    assert reasons(plan) == {"ETHUSDT": "tolerance_band"}
    forced = sells({"ETHUSDT": 0.035}, bal(USDT="9700", ETH=str(eth_qty)), REL, ignore_band=True)
    assert [c.symbol for c in forced.buy_candidates] == ["ETHUSDT"]  # 忽略容忍带：差额 50 USDT ≥ 10
    tiny = sells({"ETHUSDT": 0.035}, bal(USDT="9655", ETH=str(D("345") / D("3000.5"))), REL, ignore_band=True)
    assert reasons(tiny) == {"ETHUSDT": "below_min_trade"}  # 最小下单额仍然生效


def test_below_min_trade() -> None:
    plan = sells({"BTCUSDT": 0.0009}, bal(USDT="10000"), REL)  # 9 USDT < 10
    assert reasons(plan) == {"BTCUSDT": "below_min_trade"}


def test_sell_all_when_target_zero_or_dust() -> None:
    plan = sells({}, bal(USDT="100", SOL=("1.2345", "0.5")))
    (o,) = plan.sells
    assert o.symbol == "SOLUSDT" and o.side is Side.SELL and o.kind is OrderKind.SELL
    assert o.quantity == D("1.234")  # 只卖 free，按 step 0.001 向下取整；locked 不动
    dust = sells({}, bal(USDT="100", SOL="0.04"))  # 约 4 USDT < minNotional 5
    assert reasons(dust) == {"SOLUSDT": "dust"} and dust.sells == ()


def test_partial_sell_qty_and_price() -> None:
    btc = D("0.1")  # 约 5000.5 USDT = 50%
    plan = sells({"BTCUSDT": 0.3}, bal(USDT="5000", BTC=str(btc)))
    (o,) = plan.sells
    delta = D("0.3") * C - btc * D("50005")
    assert o.delta_usdt == delta
    assert o.quantity == (-delta / D("50000")).quantize(D("0.00001"), rounding="ROUND_FLOOR")
    raw = D("50000") * (1 - EXEC.max_slippage)  # 49850
    assert o.price == D("49850.00") and o.price >= raw  # 卖价向上取整
    assert check_order(RULES["BTCUSDT"], Side.SELL, o.price, o.quantity, AVG["BTCUSDT"]) == []


def test_non_managed_assets_are_never_sold_or_counted() -> None:
    balances = bal(USDT="10000", BNB="10")
    rules = {**RULES, "BNBUSDT": make_rules("BNBUSDT", "BNB", step="0.001")}
    books = {**BOOKS, "BNBUSDT": (D("600"), D("600.1"))}
    plan = sells({}, balances, rules=rules, books=books, managed_assets={"USDT"})
    assert plan.sells == () and plan.skipped == ()
    # 目标里有 BNB，但用户自己的 BNB 不计入当前持仓
    plan = sells({"BNBUSDT": 0.1}, balances, rules=rules, books=books, managed_assets={"USDT"})
    (c,) = plan.buy_candidates
    assert c.current_weight == 0.0 and c.delta_usdt == D("1000")


def test_not_tradable_blocked_no_quote_no_ref() -> None:
    rules = {**RULES, "LUNAUSDT": make_rules("LUNAUSDT", "LUNA", status=SymbolStatus.BREAK)}
    books = {**BOOKS, "SOLUSDT": (D("0"), D("0"))}
    avg = {k: v for k, v in AVG.items() if k != "ETHUSDT"}
    plan = sells(
        {"LUNAUSDT": 0.1, "NEWUSDT": 0.1, "SOLUSDT": 0.1, "ETHUSDT": 0.0, "BTCUSDT": 0.1},
        bal(USDT="10000", ETH="1", LUNA="5"),
        rules=rules,
        books=books,
        avg=avg,
        blocked_symbols={"BTCUSDT"},
    )
    assert reasons(plan) == {
        "LUNAUSDT": "not_tradable",
        "NEWUSDT": "not_tradable",
        "SOLUSDT": "no_quote",
        "ETHUSDT": "no_reference_price",
        "BTCUSDT": "blocked",
    }
    assert count_skips(plan.skipped) == {
        "not_tradable": 2,
        "no_quote": 1,
        "no_reference_price": 1,
        "blocked": 1,
    }


def test_zero_capital_and_validation() -> None:
    plan = sells({"BTCUSDT": 0.1}, bal(USDT="0"), capital=D("0"))
    assert reasons(plan) == {"BTCUSDT": "zero_capital"}
    with pytest.raises(ValueError):
        sells({"BTCUSDT": 0.1}, bal(USDT="0"), capital=D("-1"))
    with pytest.raises(ValueError):
        sells({"BTCUSDT": -0.1}, bal(USDT="0"))


def test_filter_rejection_is_reported() -> None:
    rules = {**RULES, "SOLUSDT": make_rules("SOLUSDT", "SOL", step="0.001", max_qty="1")}
    plan = sells({}, bal(USDT="0", SOL="50"), rules=rules)
    assert reasons(plan) == {"SOLUSDT": "filter_rejected"}
    assert "maxQty" in plan.skipped[0].detail


def test_buys_full_and_prices() -> None:
    plan = sells({"BTCUSDT": 0.3, "ETHUSDT": 0.2}, bal(USDT="10000"))
    bp = plan_buys(plan.buy_candidates, D("10000"), RULES, BOOKS, AVG, REL, EXEC)
    assert bp.scale == 1 and bp.requested_usdt == D("5000")
    by = {o.symbol: o for o in bp.buys}
    btc = by["BTCUSDT"]
    assert btc.kind is OrderKind.BUY and btc.price == D("50160.03")  # 50010 × 1.003 = 50160.03
    assert btc.quantity == D("0.05980")  # 3000 / 50160.03 向下取整到 0.00001
    assert btc.notional <= D("3000")
    assert is_multiple(by["ETHUSDT"].price, D("0.01"))


def test_buys_scaled_proportionally_when_funds_short() -> None:
    plan = sells({"BTCUSDT": 0.3, "ETHUSDT": 0.2}, bal(USDT="10000"))
    bp = plan_buys(plan.buy_candidates, D("2000"), RULES, BOOKS, AVG, REL, EXEC)
    available = D("2000") * (1 - EXEC.fee_buffer)
    assert bp.available_usdt == available and bp.scale == available / D("5000")
    by = {o.symbol: o for o in bp.buys}
    assert by["BTCUSDT"].delta_usdt == D("3000") * bp.scale
    assert by["ETHUSDT"].delta_usdt == D("2000") * bp.scale
    assert sum(o.notional for o in bp.buys) <= available


def test_scaled_buy_below_min_trade_is_skipped_via_should_skip() -> None:
    plan = sells({"BTCUSDT": 0.3, "ETHUSDT": 0.0015}, bal(USDT="10000"), ignore_band=True)
    assert {c.symbol for c in plan.buy_candidates} == {"BTCUSDT", "ETHUSDT"}  # ETH 15 USDT ≥ 10
    bp = plan_buys(plan.buy_candidates, D("1000"), RULES, BOOKS, AVG, REL, EXEC)
    assert [o.symbol for o in bp.buys] == ["BTCUSDT"]
    assert reasons(bp) == {"ETHUSDT": "below_min_trade"}  # 缩小后约 5 USDT
    empty = plan_buys(plan.buy_candidates, D("0"), RULES, BOOKS, AVG, REL, EXEC)
    assert empty.buys == () and set(reasons(empty).values()) == {"below_min_trade"}


def test_buy_candidates_filtered_before_scaling() -> None:
    cands = [
        BuyCandidate("BTCUSDT", 0.3, 0.0, D("3000"), D("0")),
        BuyCandidate("ETHUSDT", 0.2, 0.0, D("2000"), D("0")),
        BuyCandidate("GONEUSDT", 0.1, 0.0, D("1000"), D("0")),
    ]
    books = {**BOOKS, "ETHUSDT": (D("3000"), D("0"))}
    bp = plan_buys(cands, D("4000"), RULES, books, AVG, REL, EXEC, blocked_symbols={"BTCUSDT"})
    assert reasons(bp) == {"BTCUSDT": "blocked", "ETHUSDT": "no_quote", "GONEUSDT": "not_tradable"}
    assert bp.requested_usdt == 0 and bp.scale == 1
    no_ref = plan_buys(cands[:1], D("4000"), RULES, BOOKS, {}, REL, EXEC)
    assert reasons(no_ref) == {"BTCUSDT": "no_reference_price"}
    tight = {**RULES, "BTCUSDT": make_rules("BTCUSDT", "BTC", step="1")}  # 1 BTC 的 step：3000 USDT 买不到
    rej = plan_buys(cands[:1], D("4000"), tight, BOOKS, AVG, REL, EXEC)
    assert reasons(rej) == {"BTCUSDT": "filter_rejected"}


def test_percent_price_clamp_marks_may_not_fill() -> None:
    # avgPrice 远低于盘口：买价上限 ref × 1.2 < ask → 收窄，可能无法成交（照常下单）
    avg = {**AVG, "BTCUSDT": D("40000")}
    plan = sells({"BTCUSDT": 0.3}, bal(USDT="10000"), avg=avg)
    bp = plan_buys(plan.buy_candidates, D("10000"), RULES, BOOKS, avg, REL, EXEC)
    (o,) = bp.buys
    assert o.price == D("48000.00") and o.price_clamped and o.may_not_fill
    pr = price_limit_order(Side.SELL, D("100"), D("100.1"), D("0.003"), RULES["SOLUSDT"], D("200"))
    assert pr.clamped and pr.price == D("160.00") and pr.may_not_fill  # 卖价下限 ref × 0.8


def test_liquidation_plan_uses_kill_kind_and_slippage() -> None:
    cfg = ExecutionConfig(kill_slippage=D("0.01"))
    plan = plan_liquidation(
        bal(USDT="5", BTC="0.01", SOL="0.01"),
        RULES,
        BOOKS,
        AVG,
        REL,
        cfg,
        managed_assets={"USDT", "BTC", "SOL"},
    )
    (o,) = plan.sells
    assert o.kind is OrderKind.KILL and o.symbol == "BTCUSDT" and o.slippage == D("0.01")
    assert o.price == D("49500.00") and o.quantity == D("0.01")
    assert reasons(plan) == {"SOLUSDT": "dust"}
    empty = plan_liquidation(bal(USDT="5"), RULES, BOOKS, AVG, REL, cfg, managed_assets={"USDT"})
    assert empty.sells == () and empty.skipped == ()
