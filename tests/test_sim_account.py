"""SimSpotAccount：IOC 成交模型、手续费、过滤器、故障注入（dev_plan §6.3）。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from bot.backtest.sim_account import FaultKind, SimBook, SimSpotAccount, SimulatedCrash
from bot.domain.errors import ExchangeError, ExchangeUnavailableError, UnknownOrderStatus
from bot.domain.types import OrderRequest, OrderStatus, OrderType, Side, SymbolStatus, TimeInForce
from bot.exchange.base import SpotAccount
from tests.exec_helpers import AVG, RULES, make_rules, make_sim

D = Decimal


def req(cid: str, side: Side, qty: str, price: str, symbol: str = "BTCUSDT") -> OrderRequest:
    return OrderRequest(symbol, side, OrderType.LIMIT, D(qty), cid, D(price), TimeInForce.IOC)


def test_is_a_spot_account() -> None:
    acct: SpotAccount = make_sim()
    assert acct.account_id == "spot-main"
    assert set(acct.get_all_rules()) == set(RULES)
    assert acct.get_commission_rates("BTCUSDT").rate(Side.BUY) == D("0.001")
    assert acct.get_avg_price("BTCUSDT") == AVG["BTCUSDT"]
    assert acct.get_prices(["BTCUSDT", "NOPE"]) == {"BTCUSDT": D("50005")}
    with pytest.raises(ExchangeError):
        acct.get_rules("NOPEUSDT")


def test_buy_fills_at_ask_and_charges_base_commission() -> None:
    sim = make_sim({"USDT": D("1000")})
    res = sim.place_order(req("b1", Side.BUY, "0.01", "50100"))
    assert res.status is OrderStatus.FILLED and res.executed_qty == D("0.01")
    assert res.cumm_quote_qty == D("500.10") and res.fills[0].price == D("50010")
    assert res.fills[0].commission == D("0.00001") and res.fills[0].commission_asset == "BTC"
    b = sim.get_balances()
    assert b["BTC"].free == D("0.00999") and b["USDT"].free == D("499.90")


def test_sell_charges_quote_commission_and_partial_fill() -> None:
    sim = make_sim(
        {"USDT": D("0"), "BTC": D("1")}, books={**{"BTCUSDT": SimBook(D("50000"), D("50010"), D("0.3"))}}
    )
    res = sim.place_order(req("s1", Side.SELL, "0.5", "49900"))
    assert res.status is OrderStatus.EXPIRED and res.executed_qty == D("0.3")  # 盘口只有 0.3
    assert res.expiry_reason == "INSUFFICIENT_LIQUIDITY"
    assert res.fills[0].commission == D("15.00000000") and res.fills[0].commission_asset == "USDT"
    assert sim.get_balances()["USDT"].free == D("15000") - D("15")
    again = sim.place_order(req("s2", Side.SELL, "0.1", "49900"))  # 盘口已被吃光
    assert again.status is OrderStatus.EXPIRED and again.executed_qty == 0
    sim.set_book("BTCUSDT", D("50000"), D("50010"))
    assert sim.place_order(req("s3", Side.SELL, "0.1", "50001")).executed_qty == 0  # 卖价高于 bid 不成交


def test_rejections() -> None:
    sim = make_sim({"USDT": D("100")})
    with pytest.raises(ExchangeError) as ei:
        sim.place_order(req("r1", Side.BUY, "0.01", "50100"))  # 余额不足
    assert ei.value.code == -2010
    with pytest.raises(ExchangeError) as ei:
        sim.place_order(req("r2", Side.BUY, "0.000001", "50100"))  # LOT_SIZE
    assert ei.value.code == -1013
    sim.adjust_balance("USDT", D("10000"))
    sim.place_order(req("r3", Side.BUY, "0.01", "50100"))
    with pytest.raises(ExchangeError, match="Duplicate"):
        sim.place_order(req("r3", Side.BUY, "0.01", "50100"))
    sim.reject_next_order(-2010, "rejected")
    with pytest.raises(ExchangeError):
        sim.place_order(req("r4", Side.BUY, "0.01", "50100"))
    halted = make_sim(
        rules={"BTCUSDT": make_rules("BTCUSDT", "BTC", step="0.00001", status=SymbolStatus.HALT)}
    )
    with pytest.raises(ExchangeError, match="closed"):
        halted.place_order(req("h1", Side.BUY, "0.01", "50100"))
    with pytest.raises(ExchangeError, match="LIMIT IOC"):
        sim.place_order(
            OrderRequest("BTCUSDT", Side.BUY, OrderType.LIMIT, D("0.01"), "g1", D("50100"), TimeInForce.GTC)
        )


@pytest.mark.parametrize("kind", [FaultKind.TIMEOUT, FaultKind.HTTP_5XX, FaultKind.STATUS_UNKNOWN])
def test_unknown_before_and_after_execution(kind: FaultKind) -> None:
    sim = make_sim()
    sim.inject_place_fault(kind, executed=False)
    with pytest.raises(UnknownOrderStatus):
        sim.place_order(req("u1", Side.BUY, "0.01", "50100"))
    assert sim.get_order("BTCUSDT", "u1") is None and "BTC" not in sim.get_balances()
    sim.inject_place_fault(kind, executed=True, symbol="BTCUSDT")
    with pytest.raises(UnknownOrderStatus):
        sim.place_order(req("u2", Side.BUY, "0.01", "50100"))
    found = sim.get_order("BTCUSDT", "u2")
    assert found is not None and found.status is OrderStatus.FILLED and found.fills == ()
    assert found.order_id is not None and len(sim.get_trades("BTCUSDT", found.order_id)) == 1
    assert sim.get_order("ETHUSDT", "u2") is None  # symbol 不符


def test_crash_fault_and_hidden_queries_and_query_errors() -> None:
    sim = make_sim()
    sim.inject_place_fault("crash", executed=True)
    with pytest.raises(SimulatedCrash):
        sim.place_order(req("c1", Side.BUY, "0.01", "50100"))
    sim.hidden_queries = 2
    assert sim.get_order("BTCUSDT", "c1") is None
    assert sim.get_order("BTCUSDT", "c1") is None
    assert sim.get_order("BTCUSDT", "c1") is not None
    sim.fail_next_queries(1)
    with pytest.raises(ExchangeUnavailableError):
        sim.get_order("BTCUSDT", "c1")
    sim.inject_place_fault(FaultKind.TIMEOUT, executed=False, symbol="ETHUSDT", times=2)
    sim.place_order(req("ok", Side.BUY, "0.001", "50100"))  # 其他交易对不受影响
    for cid in ("e1", "e2"):
        with pytest.raises(UnknownOrderStatus):
            sim.place_order(req(cid, Side.BUY, "0.01", "3010", "ETHUSDT"))
    sim.place_order(req("e3", Side.BUY, "0.01", "3010", "ETHUSDT"))
    assert sim.stats.place_calls == 5 and sim.stats.accepted_cids == ["c1", "ok", "e3"]


def test_open_orders_and_cancel() -> None:
    sim = make_sim()
    sim.add_open_order("BTCUSDT", "mom1-old-1", Side.BUY, D("0.01"), D("40000"))
    assert [o.client_order_id for o in sim.get_open_orders("BTCUSDT")] == ["mom1-old-1"]
    sim.fail_next_cancels(1)
    with pytest.raises(UnknownOrderStatus):
        sim.cancel_order("BTCUSDT", "mom1-old-1")
    res = sim.cancel_order("BTCUSDT", "mom1-old-1")
    assert res.status is OrderStatus.CANCELED and sim.get_open_orders("BTCUSDT") == []
    with pytest.raises(ExchangeError) as ei:
        sim.cancel_order("BTCUSDT", "mom1-old-1")
    assert ei.value.code == -2011
    q = sim.get_order("BTCUSDT", "mom1-old-1")
    assert q is not None and q.status is OrderStatus.CANCELED


def test_balance_inputs_and_prices_override() -> None:
    from bot.domain.types import Balance

    sim = SimSpotAccount(
        rules=RULES,
        balances={"USDT": Balance("USDT", D("1"), D("2")), "BTC": D("0")},
        books={"BTCUSDT": (D("1"), D("2"))},
        prices={"BTCUSDT": D("1.5")},
    )
    assert sim.get_balances() == {"USDT": Balance("USDT", D("1"), D("2"))}
    assert sim.get_prices(["BTCUSDT"]) == {"BTCUSDT": D("1.5")}
    assert sim.get_book_ticker("BTCUSDT") == (D("1"), D("2"))
    assert sim.get_avg_price("BTCUSDT") == D("1.5")
    sim.set_avg_price("BTCUSDT", D("1.6"))
    assert sim.get_avg_price("BTCUSDT") == D("1.6")
