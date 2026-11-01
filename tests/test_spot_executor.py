"""现货执行器（spec §6.5～§6.8，dev_plan M5-7）。

重点：执行状态未知的全部分支（§6.6）、崩溃后不重复下单、绝不出现"同一意图换新 ID 重下"。
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from bot.backtest.sim_account import FaultKind, SimBook, SimSpotAccount, SimulatedCrash
from bot.domain.errors import ExchangeError, ExchangeUnavailableError, RateLimitedError
from bot.domain.types import IntentStatus, OrderRequest, OrderResult, OrderStatus, Side
from bot.execution.client_order_id import OrderKind, parse_client_order_id
from bot.execution.planner import PlannedOrder, plan_buys, plan_liquidation, plan_sells
from bot.execution.spot_executor import (
    EV_INFERRED_FILL,
    EV_INTENT_CLOSED,
    EV_INTENT_RESOLVED,
    EV_INTENT_UNRESOLVED,
    EV_OPEN_ORDER_CANCELED,
    EV_ORDER_INCOMPLETE,
    EV_ORDER_REJECTED,
    EV_ORDER_STATUS_UNKNOWN,
    EV_RECONCILE_DIFF,
    KILL_MAX_ATTEMPTS,
    MonotonicClock,
    intent_status_for,
)
from bot.persistence.repo import NewIntent, StateRepo
from tests.exec_helpers import (
    ACCOUNT,
    AVG,
    BOOKS,
    EXEC,
    REL,
    RULES,
    STRATEGY,
    Sleeper,
    TickClock,
    make_executor,
    make_sim,
    open_repo,
)

D = Decimal
DELAYS = [1.0, 3.0, 10.0, 30.0, 60.0]
UNKNOWN_KINDS = [FaultKind.TIMEOUT, FaultKind.HTTP_5XX, FaultKind.STATUS_UNKNOWN]


def order(
    symbol: str = "BTCUSDT",
    side: Side = Side.BUY,
    qty: str = "0.01",
    price: str = "50160.03",
    kind: OrderKind | None = None,
) -> PlannedOrder:
    return PlannedOrder(
        symbol=symbol,
        side=side,
        kind=kind or (OrderKind.BUY if side is Side.BUY else OrderKind.SELL),
        quantity=D(qty),
        price=D(price),
        slippage=EXEC.max_slippage,
        target_weight=0.05,
        current_weight=0.0,
        delta_usdt=D("500"),
        reference_price=AVG[symbol],
    )


def intents(repo: StateRepo) -> list[Any]:
    return repo.list_intents(STRATEGY, ACCOUNT)


def assert_no_resend_after_unknown(repo: StateRepo, sim: SimSpotAccount) -> None:
    """关键不变式：状态未知 / 查不到的意图之后没有更高的 attempt；交易所收到的每个 ID 只出现一次。"""
    groups: dict[tuple[date, int], list[Any]] = defaultdict(list)
    for i in intents(repo):
        groups[(i.run_date, i.seq)].append(i)
    for rows in groups.values():
        rows.sort(key=lambda r: r.attempt)
        for prev, nxt in itertools.pairwise(rows):
            assert prev.status not in ("UNKNOWN", "NOT_FOUND", "SENT"), f"{prev.cid} 之后又发了 {nxt.cid}"
    sent = [r.client_order_id for r in sim.placed]
    assert len(sent) == len(set(sent))


# ---- 正常路径 ----


def test_buy_and_sell_happy_path(tmp_path: Path) -> None:
    sim = make_sim({"USDT": D("10000"), "SOL": D("10")})
    repo = open_repo(tmp_path)
    ex = make_executor(sim, repo)
    rep = ex.execute([order("SOLUSDT", Side.SELL, "5", "99.70"), order()])
    assert [o.status for o in rep.outcomes] == [IntentStatus.FILLED, IntentStatus.FILLED]
    sell, buy = rep.outcomes
    assert sell.executed_qty == D("5") and sell.commission("USDT") == D("0.5")
    assert buy.executed_qty == D("0.01") and buy.commission("BTC") == D("0.00001")
    rows = {r.symbol: r for r in intents(repo)}
    assert rows["BTCUSDT"].status == "FILLED" and rows["BTCUSDT"].exchange_order_id is not None
    assert rows["BTCUSDT"].base_balance_before == 0 and rows["SOLUSDT"].base_balance_before == D("10")
    p = parse_client_order_id(rows["BTCUSDT"].cid)
    assert p is not None and p.kind is OrderKind.BUY and p.attempt == 1
    fills = repo.list_fills(STRATEGY, ACCOUNT)
    assert {(f.symbol, f.base_asset, f.quote_asset) for f in fills} == {
        ("SOLUSDT", "SOL", "USDT"),
        ("BTCUSDT", "BTC", "USDT"),
    }
    pos = repo.get_position(ACCOUNT, STRATEGY, "BTC")  # 首次买入自动登记为受管（spec §7）
    assert pos is not None and pos.managed and pos.updated_at == rows["BTCUSDT"].created_at
    assert not [e for e in rep.events if e.level == "WARN"]
    assert ex.execute([]).outcomes == []


def test_intents_are_written_ahead_before_any_order(tmp_path: Path) -> None:
    repo = open_repo(tmp_path)
    seen: list[dict[str, str]] = []

    class Spy(SimSpotAccount):
        def place_order(self, req: OrderRequest) -> OrderResult:
            seen.append({r.symbol: r.status for r in intents(repo)})
            return super().place_order(req)

    sim = Spy(rules=RULES, balances={"USDT": D("10000")}, books=BOOKS, avg_prices=AVG, account_id=ACCOUNT)
    make_executor(sim, repo).execute([order(), order("ETHUSDT", qty="0.1", price="3010.00")])
    assert seen[0] == {"BTCUSDT": "SENT", "ETHUSDT": "PLANNED"}  # 第二笔在第一笔发送前就已写库
    assert seen[1] == {"BTCUSDT": "FILLED", "ETHUSDT": "SENT"}


def test_input_validation(tmp_path: Path) -> None:
    ex = make_executor(make_sim(), open_repo(tmp_path))
    with pytest.raises(ValueError, match="重复"):
        ex.execute([order(), order()])
    with pytest.raises(ValueError):
        ex.execute([order()], max_attempts=0)


class RefillingSim(SimSpotAccount):
    """每次查询盘口时补充流动性，并把卖一价抬高 bump（模拟重试时行情变化）。"""

    bump = D("5")

    def get_book_ticker(self, symbol: str) -> tuple[Decimal, Decimal]:
        b = self.books[symbol]
        self.set_book(symbol, b.bid, b.ask + self.bump)
        return super().get_book_ticker(symbol)


def test_partial_fill_retries_with_next_attempt(tmp_path: Path) -> None:
    sim = RefillingSim(
        rules=RULES,
        balances={"USDT": D("10000")},
        avg_prices=AVG,
        account_id=ACCOUNT,
        books={**BOOKS, "BTCUSDT": SimBook(D("50000"), D("50010"), ask_qty=D("0.004"))},
    )
    repo = open_repo(tmp_path)
    rep = make_executor(sim, repo).execute([order()])
    (out,) = rep.outcomes
    assert out.status is IntentStatus.FILLED and out.executed_qty == D("0.01") and len(out.cids) == 2
    a1, a2 = (parse_client_order_id(c) for c in out.cids)
    assert a1 is not None and a2 is not None and a1.seq == a2.seq and (a1.attempt, a2.attempt) == (1, 2)
    rows = sorted(intents(repo), key=lambda r: r.attempt)
    assert [r.status for r in rows] == ["PARTIAL", "FILLED"]
    assert rows[1].limit_price == D("50165.04")  # 重新获取盘口后按 (50015 × 1.003) 向下取整
    assert rows[1].base_balance_before == D("0.004") - D("0.000004")  # 第 1 次的成交净额
    total_spend = sum(r.intended_qty * r.limit_price for r in rows if r.attempt == 2) + D("0.004") * D(
        "50010"
    )
    assert total_spend <= D("0.01") * D("50160.03")  # 买单重试不超过原预算
    assert_no_resend_after_unknown(repo, sim)


def test_incomplete_after_max_attempts(tmp_path: Path) -> None:
    sim = make_sim(books={**BOOKS, "BTCUSDT": SimBook(D("50000"), D("50010"), ask_qty=D("0.004"))})
    repo = open_repo(tmp_path)
    rep = make_executor(sim, repo).execute([order()])
    (out,) = rep.outcomes
    assert out.status is IntentStatus.EXPIRED and out.executed_qty == D("0.004") and len(out.cids) == 2
    assert [e.type for e in rep.events] == [EV_ORDER_INCOMPLETE]
    assert rep.events[0].level == "WARN"


def test_remaining_below_filters_stops_quietly(tmp_path: Path) -> None:
    # 剩余 0.00001 BTC（约 0.5 USDT）低于 minNotional：不追单，也不发 ORDER_INCOMPLETE
    sim = RefillingSim(
        rules=RULES,
        balances={"USDT": D("10000")},
        avg_prices=AVG,
        account_id=ACCOUNT,
        books={**BOOKS, "BTCUSDT": SimBook(D("50000"), D("50010"), ask_qty=D("0.00999"))},
    )
    rep = make_executor(sim, open_repo(tmp_path)).execute([order()])
    (out,) = rep.outcomes
    assert len(out.cids) == 1 and "无法下单" in out.detail and rep.events == []


def test_kill_orders_use_k_kind_and_three_attempts(tmp_path: Path) -> None:
    sim = make_sim(
        {"USDT": D("0"), "BTC": D("1")},
        books={**BOOKS, "BTCUSDT": SimBook(D("50000"), D("50010"), bid_qty=D("0.1"))},
    )
    plan = plan_liquidation(sim.get_balances(), RULES, BOOKS, AVG, REL, EXEC, managed_assets={"BTC", "USDT"})
    rep = make_executor(sim, open_repo(tmp_path)).execute(list(plan.sells), max_attempts=KILL_MAX_ATTEMPTS)
    (out,) = rep.outcomes
    assert len(out.cids) == 3 and all("-K" in c for c in out.cids)


def test_rejected_is_not_retried(tmp_path: Path) -> None:
    sim = make_sim()
    sim.reject_next_order(-2010, "Account has insufficient balance")
    repo = open_repo(tmp_path)
    rep = make_executor(sim, repo).execute([order()])
    (out,) = rep.outcomes
    assert out.status is IntentStatus.REJECTED and len(sim.placed) == 1
    (row,) = intents(repo)
    assert row.error_code == -2010 and row.status == "REJECTED"
    assert [e.type for e in rep.events] == [EV_ORDER_REJECTED]


# ---- 执行状态未知（§6.6） ----


@pytest.mark.parametrize("kind", UNKNOWN_KINDS)
def test_unknown_then_found_immediately(tmp_path: Path, kind: FaultKind) -> None:
    sim = make_sim()
    sim.inject_place_fault(kind, executed=True)
    repo, sleeper = open_repo(tmp_path), Sleeper()
    ex = make_executor(sim, repo, sleeper=sleeper)
    rep = ex.execute([order()])
    (out,) = rep.outcomes
    assert out.status is IntentStatus.FILLED and out.executed_qty == D("0.01")
    assert sleeper.calls == [1.0]  # 第一次查询就查到
    assert len(sim.placed) == 1 and not ex.blocked_symbols
    assert len(repo.list_fills(STRATEGY, ACCOUNT)) == 1  # 用 myTrades 补全成交
    assert [e.type for e in rep.events] == [EV_INTENT_RESOLVED]
    (row,) = intents(repo)
    assert row.status == "FILLED" and "状态未知" in (row.note or "")
    assert_no_resend_after_unknown(repo, sim)


@pytest.mark.parametrize("kind", UNKNOWN_KINDS)
def test_unknown_then_found_after_delayed_visibility(tmp_path: Path, kind: FaultKind) -> None:
    sim = make_sim()
    sim.inject_place_fault(kind, executed=True)
    sim.hidden_queries = 2
    sleeper = Sleeper()
    rep = make_executor(sim, open_repo(tmp_path), sleeper=sleeper).execute([order()])
    assert rep.outcomes[0].status is IntentStatus.FILLED
    assert sleeper.calls == [1.0, 3.0, 10.0]
    assert len(sim.placed) == 1


@pytest.mark.parametrize("kind", UNKNOWN_KINDS)
def test_unknown_and_never_found_blocks_symbol(tmp_path: Path, kind: FaultKind) -> None:
    sim = make_sim()
    sim.inject_place_fault(kind, executed=False)
    repo, sleeper = open_repo(tmp_path), Sleeper()
    ex = make_executor(sim, repo, sleeper=sleeper)
    rep = ex.execute([order(), order("ETHUSDT", qty="0.1", price="3010.00")])
    btc, eth = rep.outcomes
    assert btc.status is IntentStatus.NOT_FOUND and eth.status is IntentStatus.FILLED
    assert sleeper.calls == DELAYS and sim.stats.get_order_calls == 5
    assert ex.blocked_symbols == {"BTCUSDT"} and rep.blocked_symbols == frozenset({"BTCUSDT"})
    unknown = [e for e in rep.events if e.type == EV_ORDER_STATUS_UNKNOWN]
    assert len(unknown) == 1 and unknown[0].level == "WARN"
    # 本次运行不再对该 symbol 下任何订单：之后的执行和买单计划都会跳过
    again = ex.execute([order(qty="0.02")])
    assert again.outcomes == [] and again.skipped == [("BTCUSDT", "blocked")]
    plan = plan_sells(
        {"BTCUSDT": 0.3},
        D("10000"),
        sim.get_balances(),
        RULES,
        BOOKS,
        AVG,
        REL,
        EXEC,
        managed_assets={"USDT"},
        blocked_symbols=ex.blocked_symbols,
    )
    assert [s.reason.value for s in plan.skipped] == ["blocked"]
    assert [r.client_order_id for r in sim.placed if r.symbol == "BTCUSDT"] == [btc.cids[0]]
    assert_no_resend_after_unknown(repo, sim)


def test_unknown_with_failing_queries_stays_unknown(tmp_path: Path) -> None:
    sim = make_sim()
    sim.inject_place_fault(FaultKind.TIMEOUT, executed=False)
    sim.fail_next_queries(2)  # 前 2 次查询网络错误，后 3 次 -2013：不是"全部 -2013"
    repo = open_repo(tmp_path)
    ex = make_executor(sim, repo)
    rep = ex.execute([order()])
    assert rep.outcomes[0].status is IntentStatus.UNKNOWN
    assert intents(repo)[0].status == "UNKNOWN" and ex.blocked_symbols == {"BTCUSDT"}
    assert [e.type for e in rep.events] == [EV_ORDER_STATUS_UNKNOWN]
    assert_no_resend_after_unknown(repo, sim)


def test_unknown_resolved_as_partial_does_not_chase(tmp_path: Path) -> None:
    sim = make_sim(books={**BOOKS, "BTCUSDT": SimBook(D("50000"), D("50010"), ask_qty=D("0.004"))})
    sim.inject_place_fault(FaultKind.HTTP_5XX, executed=True)
    repo = open_repo(tmp_path)
    rep = make_executor(sim, repo).execute([order()])
    (out,) = rep.outcomes
    assert out.status is IntentStatus.PARTIAL and out.executed_qty == D("0.004")
    assert len(sim.placed) == 1 and "不再追单" in out.detail
    assert_no_resend_after_unknown(repo, sim)


def test_unknown_on_retry_attempt(tmp_path: Path) -> None:
    sim = RefillingSim(
        rules=RULES,
        balances={"USDT": D("10000")},
        avg_prices=AVG,
        account_id=ACCOUNT,
        books={**BOOKS, "BTCUSDT": SimBook(D("50000"), D("50010"), ask_qty=D("0.004"))},
    )
    repo = open_repo(tmp_path)
    ex = make_executor(sim, repo)
    calls = {"n": 0}
    orig = sim.place_order

    def second_unknown(req: OrderRequest) -> OrderResult:
        calls["n"] += 1
        if calls["n"] == 2:  # 第 2 次尝试时状态未知（订单没有到达撮合）
            sim.inject_place_fault(FaultKind.STATUS_UNKNOWN, executed=False)
        return orig(req)

    sim.place_order = second_unknown  # type: ignore[method-assign]
    rep = ex.execute([order()], max_attempts=3)
    (out,) = rep.outcomes
    assert out.status is IntentStatus.NOT_FOUND and len(out.cids) == 2  # 第 3 次绝不会发出
    assert ex.blocked_symbols == {"BTCUSDT"}
    assert_no_resend_after_unknown(repo, sim)


class RestingSim(SimSpotAccount):
    """下单后返回 NEW（模拟 IOC 订单意外停留在挂单状态）。"""

    def place_order(self, req: OrderRequest) -> OrderResult:
        self.placed.append(req)
        assert req.price is not None
        return self.add_open_order(req.symbol, req.client_order_id, req.side, req.quantity, req.price)


def test_non_final_order_is_canceled_and_recorded(tmp_path: Path) -> None:
    sim = RestingSim(
        rules=RULES, balances={"USDT": D("10000")}, books=BOOKS, avg_prices=AVG, account_id=ACCOUNT
    )
    repo, sleeper = open_repo(tmp_path), Sleeper()
    rep = make_executor(sim, repo, sleeper=sleeper).execute([order()])
    (out,) = rep.outcomes
    assert out.status is IntentStatus.EXPIRED and sim.stats.cancel_calls == 1 and len(sim.placed) == 1
    assert sleeper.calls == DELAYS


def test_non_final_order_that_cannot_be_canceled_blocks(tmp_path: Path) -> None:
    sim = RestingSim(
        rules=RULES, balances={"USDT": D("10000")}, books=BOOKS, avg_prices=AVG, account_id=ACCOUNT
    )
    sim.fail_next_cancels(1)
    repo = open_repo(tmp_path)
    ex = make_executor(sim, repo)
    rep = ex.execute([order()])
    assert rep.outcomes[0].status is IntentStatus.UNKNOWN and ex.blocked_symbols == {"BTCUSDT"}
    assert intents(repo)[0].status == "UNKNOWN"


class RaisingSim(SimSpotAccount):
    exc: Exception = RuntimeError()

    def place_order(self, req: OrderRequest) -> OrderResult:
        self.placed.append(req)
        raise self.exc


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (RateLimitedError(429, 30.0), "REJECTED", 429),
        (RateLimitedError(418, None), "REJECTED", 418),
        (ExchangeUnavailableError("对时失败"), "CLOSED", None),
        (SimulatedCrash("killed"), "SENT", None),  # 进程被杀：意图保持 SENT，下次运行处理
    ],
)
def test_send_errors_propagate(tmp_path: Path, exc: Exception, status: str, code: int | None) -> None:
    sim = RaisingSim(
        rules=RULES, balances={"USDT": D("10000")}, books=BOOKS, avg_prices=AVG, account_id=ACCOUNT
    )
    sim.exc = exc
    repo = open_repo(tmp_path)
    with pytest.raises(type(exc)):
        make_executor(sim, repo).execute([order()])
    (row,) = intents(repo)
    assert row.status == status and row.error_code == code


# ---- KILL ----


def test_abort_before_next_order(tmp_path: Path) -> None:
    sim = make_sim()
    repo = open_repo(tmp_path)
    flag = {"kill": False}

    ex = make_executor(sim, repo, should_abort=lambda: flag["kill"])
    orig = sim.place_order

    def place_then_kill(req: OrderRequest) -> OrderResult:
        flag["kill"] = True  # 第一笔订单之后出现 KILL 文件
        return orig(req)

    sim.place_order = place_then_kill  # type: ignore[method-assign]
    rep = ex.execute([order(), order("ETHUSDT", qty="0.1", price="3010.00")])
    assert rep.aborted and len(sim.placed) == 1
    assert rep.skipped == [("ETHUSDT", "aborted")]
    eth = next(r for r in intents(repo) if r.symbol == "ETHUSDT")
    assert eth.status == "CLOSED" and "KILL" in (eth.note or "")


def test_abort_before_retry(tmp_path: Path) -> None:
    sim = make_sim(books={**BOOKS, "BTCUSDT": SimBook(D("50000"), D("50010"), ask_qty=D("0.004"))})
    n = {"checks": 0}

    def abort() -> bool:
        n["checks"] += 1
        return n["checks"] > 1  # 第一次检查放行，重试前的检查触发

    rep = make_executor(sim, open_repo(tmp_path), should_abort=abort).execute([order()])
    assert rep.aborted and len(sim.placed) == 1 and rep.outcomes[0].detail.startswith("KILL")
    assert rep.events == []  # 中止时不发 ORDER_INCOMPLETE


# ---- 崩溃与下一次运行的对账（§6.6 第 3 步） ----


def _next_run(sim: SimSpotAccount, tmp_path: Path, clock: TickClock, sleeper: Sleeper | None = None) -> Any:
    repo = StateRepo.open(tmp_path / "state.db", clock=clock)  # 新进程：重新打开数据库
    ex = make_executor(
        sim, repo, run_id="mom1:2026-11-03", run_date=date(2026, 11, 3), clock=clock, sleeper=sleeper
    )
    return repo, ex


def test_crash_after_order_executed_does_not_duplicate(tmp_path: Path) -> None:
    clock = TickClock()
    sim = make_sim()
    repo = open_repo(tmp_path, clock)
    sim.inject_place_fault(FaultKind.CRASH, executed=True)
    with pytest.raises(SimulatedCrash):
        make_executor(sim, repo, clock=clock).execute([order()])
    assert intents(repo)[0].status == "SENT"
    repo.close()

    repo2, ex2 = _next_run(sim, tmp_path, clock)
    rep = ex2.reconcile_pending_intents()
    (res,) = rep.resolved
    assert res.previous_status is IntentStatus.SENT and res.new_status is IntentStatus.FILLED
    assert intents(repo2)[0].status == "FILLED" and len(repo2.list_fills(STRATEGY, ACCOUNT)) == 1
    pos = repo2.get_position(ACCOUNT, STRATEGY, "BTC")
    assert pos is not None and pos.managed
    # 订单计划按实际余额重算：目标 5% ≈ 500 USDT 已经满足，不会再下单
    plan = plan_sells(
        {"BTCUSDT": 0.05},
        D("10000"),
        sim.get_balances(),
        RULES,
        BOOKS,
        AVG,
        REL,
        EXEC,
        managed_assets={"USDT", "BTC"},
    )
    assert plan.sells == () and plan.buy_candidates == ()
    assert [s.reason.value for s in plan.skipped] == ["tolerance_band"]
    assert sim.stats.place_calls == 1
    assert ex2.reconcile_pending_intents().resolved == []  # 已无遗留


def test_crash_before_order_reached_exchange(tmp_path: Path) -> None:
    clock = TickClock()
    sim = make_sim()
    repo = open_repo(tmp_path, clock)
    sim.inject_place_fault(FaultKind.CRASH, executed=False)
    with pytest.raises(SimulatedCrash):
        make_executor(sim, repo, clock=clock).execute([order()])
    _repo2, ex2 = _next_run(sim, tmp_path, clock)
    rep = ex2.reconcile_pending_intents(sim.get_balances())
    assert [r.new_status for r in rep.resolved] == [IntentStatus.CLOSED]
    assert [e.type for e in rep.events] == [EV_INTENT_CLOSED]
    assert sim.stats.place_calls == 1


def test_not_found_next_run_balance_explained(tmp_path: Path) -> None:
    clock = TickClock()
    sim = make_sim()
    repo = open_repo(tmp_path, clock)
    sim.inject_place_fault(FaultKind.TIMEOUT, executed=False)
    make_executor(sim, repo, clock=clock).execute([order()])
    assert intents(repo)[0].status == "NOT_FOUND"
    repo2, ex2 = _next_run(sim, tmp_path, clock)
    rep = ex2.reconcile_pending_intents()
    assert rep.resolved[0].new_status is IntentStatus.CLOSED and rep.events[0].type == EV_INTENT_CLOSED
    assert rep.events[0].level == "INFO" and not ex2.blocked_symbols
    assert intents(repo2)[0].status == "CLOSED"


def test_not_found_next_run_balance_unexplained(tmp_path: Path) -> None:
    clock = TickClock()
    sim = make_sim()
    repo = open_repo(tmp_path, clock)
    sim.inject_place_fault(FaultKind.TIMEOUT, executed=False)
    make_executor(sim, repo, clock=clock).execute([order()])
    sim.adjust_balance("BTC", D("0.01"))  # 余额变了，但查不到订单，也没有已知成交
    repo2, ex2 = _next_run(sim, tmp_path, clock)
    rep = ex2.reconcile_pending_intents()
    (ev,) = rep.events
    assert ev.type == EV_INFERRED_FILL and ev.level == "WARN"
    assert D(str(ev.payload["diff"])) == D("0.01")
    assert intents(repo2)[0].status == "CLOSED" and "推断成交" in (intents(repo2)[0].note or "")


def test_not_found_then_found_next_run(tmp_path: Path) -> None:
    clock = TickClock()
    sim = make_sim()
    repo = open_repo(tmp_path, clock)
    sim.inject_place_fault(FaultKind.HTTP_5XX, executed=True)
    sim.hidden_queries = 5  # 本次运行的 5 次查询全部 -2013
    make_executor(sim, repo, clock=clock).execute([order()])
    assert intents(repo)[0].status == "NOT_FOUND"
    repo2, ex2 = _next_run(sim, tmp_path, clock)
    rep = ex2.reconcile_pending_intents()
    (res,) = rep.resolved
    assert res.previous_status is IntentStatus.NOT_FOUND and res.new_status is IntentStatus.FILLED
    assert rep.events[0].type == EV_INTENT_RESOLVED and rep.events[0].level == "WARN"
    assert len(repo2.list_fills(STRATEGY, ACCOUNT)) == 1
    assert sim.stats.place_calls == 1


def test_pending_query_failure_blocks_symbol(tmp_path: Path) -> None:
    clock = TickClock()
    sim = make_sim()
    repo = open_repo(tmp_path, clock)
    sim.inject_place_fault(FaultKind.TIMEOUT, executed=False)
    make_executor(sim, repo, clock=clock).execute([order()])
    repo2, ex2 = _next_run(sim, tmp_path, clock)
    sim.fail_next_queries(1)
    rep = ex2.reconcile_pending_intents()
    assert rep.unresolved == [intents(repo2)[0].cid] and ex2.blocked_symbols == {"BTCUSDT"}
    assert rep.events[0].type == EV_INTENT_UNRESOLVED
    assert intents(repo2)[0].status == "NOT_FOUND"  # 保持原状态，下次再查


def test_planned_leftover_is_checked_and_closed(tmp_path: Path) -> None:
    clock = TickClock()
    sim = make_sim()
    repo = open_repo(tmp_path, clock)
    repo.create_intents(
        [
            NewIntent(
                "mom1-261102-aaaaaaaa-B001-1",
                "r",
                STRATEGY,
                ACCOUNT,
                "BTCUSDT",
                Side.BUY,
                "B",
                date(2026, 11, 2),
                1,
                1,
                D("0.01"),
                D("50000"),
                "BTC",
                D("0"),
            )
        ]
    )
    _, ex = _next_run(sim, tmp_path, clock)
    rep = ex.reconcile_pending_intents()
    assert rep.resolved[0].previous_status is IntentStatus.PLANNED
    assert rep.resolved[0].new_status is IntentStatus.CLOSED


def test_found_pending_order_still_open_is_canceled(tmp_path: Path) -> None:
    clock = TickClock()
    sim = make_sim()
    repo = open_repo(tmp_path, clock)
    cid = "mom1-261102-aaaaaaaa-B001-1"
    repo.create_intents(
        [
            NewIntent(
                cid,
                "r",
                STRATEGY,
                ACCOUNT,
                "BTCUSDT",
                Side.BUY,
                "B",
                date(2026, 11, 2),
                1,
                1,
                D("0.01"),
                D("40000"),
                "BTC",
                D("0"),
            )
        ]
    )
    repo.update_intent(cid, status=IntentStatus.SENT)
    sim.add_open_order("BTCUSDT", cid, Side.BUY, D("0.01"), D("40000"))
    _, ex = _next_run(sim, tmp_path, clock)
    rep = ex.reconcile_pending_intents()
    assert rep.resolved[0].new_status is IntentStatus.EXPIRED and sim.stats.cancel_calls == 1


# ---- 撤单（§6.7） ----


def test_cancel_strategy_prefixed_open_orders(tmp_path: Path) -> None:
    sim = make_sim()
    sim.add_open_order("BTCUSDT", "mom1-old-1", Side.BUY, D("0.01"), D("40000"))
    sim.add_open_order("BTCUSDT", "web_123", Side.BUY, D("0.01"), D("40000"))
    sim.add_open_order("ETHUSDT", "mom1-old-2", Side.SELL, D("1"), D("4000"))
    repo = open_repo(tmp_path)
    ex = make_executor(sim, repo)
    rep = ex.cancel_strategy_open_orders(["BTCUSDT", "ETHUSDT", "BTCUSDT"])
    assert sorted(o.client_order_id for o in rep.canceled) == ["mom1-old-1", "mom1-old-2"]
    assert [o.client_order_id for o in rep.foreign_open_orders] == ["web_123"]
    assert [o.client_order_id for o in sim.get_open_orders("BTCUSDT")] == ["web_123"]  # 非本策略的挂单不动
    assert {e.type for e in rep.events} == {EV_OPEN_ORDER_CANCELED}
    assert not ex.blocked_symbols


def test_cancel_unknown_or_unverifiable_blocks(tmp_path: Path) -> None:
    sim = make_sim()
    sim.add_open_order("BTCUSDT", "mom1-old-1", Side.BUY, D("0.01"), D("40000"))
    sim.fail_next_cancels(1)

    class BrokenOpenOrders(SimSpotAccount):
        def get_open_orders(self, symbol: str) -> list[OrderResult]:
            if symbol == "ETHUSDT":
                raise ExchangeUnavailableError("down")
            return super().get_open_orders(symbol)

    sim.__class__ = BrokenOpenOrders
    ex = make_executor(sim, open_repo(tmp_path))
    rep = ex.cancel_strategy_open_orders(["BTCUSDT", "ETHUSDT"])
    assert ex.blocked_symbols == {"BTCUSDT", "ETHUSDT"} and rep.canceled == []
    assert [e.type for e in rep.events] == [EV_ORDER_STATUS_UNKNOWN]


def test_cancel_rejected_falls_back_to_query(tmp_path: Path) -> None:
    sim = make_sim()
    sim.add_open_order("BTCUSDT", "mom1-old-1", Side.BUY, D("0.01"), D("40000"))

    class AlreadyGone(SimSpotAccount):
        def cancel_order(self, symbol: str, client_order_id: str) -> OrderResult:
            super().cancel_order(symbol, client_order_id)  # 实际已撤
            raise ExchangeError(-2011, "Unknown order sent.")

    sim.__class__ = AlreadyGone
    rep = make_executor(sim, open_repo(tmp_path)).cancel_strategy_open_orders(["BTCUSDT"])
    assert rep.canceled[0].status is OrderStatus.CANCELED


# ---- 对账（§6.8） ----


def test_reconcile_positions(tmp_path: Path) -> None:
    clock = TickClock()
    sim = make_sim({"USDT": D("10000")})
    repo = open_repo(tmp_path, clock)
    ex = make_executor(sim, repo, clock=clock)
    first = ex.reconcile_positions()
    assert [(d.asset, d.initialized) for d in first.diffs] == [("USDT", True)] and first.events == []
    ex.execute([order(), order("SOLUSDT", qty="0.05", price="100.40")])  # 扣手续费后 SOL 市值 < 5：灰尘
    after = ex.reconcile_positions()
    assert after.diffs == [] and after.events == []  # 本地记录 + 成交净额 == 交易所余额
    assert (
        after.quantities["BTC"] == D("0.00999")
        and after.quantities["USDT"] == sim.get_balances()["USDT"].total
    )
    sol = repo.get_position(ACCOUNT, STRATEGY, "SOL")
    assert sol is not None and sol.dust and sol.qty == D("0.04995")

    sim.adjust_balance("USDT", D("-50"))  # 无法解释的变化
    sim.adjust_balance("BTC", D("0.000015"))  # > 1 个 stepSize，但市值 < 1 USDT
    rep = ex.reconcile_positions()
    by = {d.asset: d for d in rep.diffs}
    assert by["USDT"].diff == D("-50") and by["USDT"].alert
    assert by["BTC"].diff == D("0.000015") and not by["BTC"].alert
    levels = {e.payload["asset"]: e.level for e in rep.events if e.type == EV_RECONCILE_DIFF}
    assert levels == {"USDT": "WARN", "BTC": "INFO"}
    pos = repo.get_position(ACCOUNT, STRATEGY, "USDT")
    assert pos is not None and pos.qty == sim.get_balances()["USDT"].total  # 以交易所数值覆盖

    sim.adjust_balance("BTC", D("0.000001"))  # ≤ 1 个 stepSize：不算差额
    assert ex.reconcile_positions(prices={"BTC": D("50000"), "SOL": D("100")}).diffs == []


def test_reconcile_unknown_price_alerts(tmp_path: Path) -> None:
    sim = make_sim({"USDT": D("100")})
    repo = open_repo(tmp_path)
    ex = make_executor(sim, repo)
    ex.reconcile_positions()
    repo.upsert_position(ACCOUNT, STRATEGY, "XYZ", qty=D("1"), managed=True)  # 执行环境中不存在的资产
    rep = ex.reconcile_positions()
    (d,) = rep.diffs
    assert d.asset == "XYZ" and d.diff_usdt is None and d.alert


# ---- 端到端：计划 + 执行 + 对账 ----


def test_end_to_end_rebalance_with_sim(tmp_path: Path) -> None:
    clock = TickClock()
    sim = make_sim({"USDT": D("7000"), "SOL": D("30")})
    repo = open_repo(tmp_path, clock)
    repo.upsert_position(ACCOUNT, STRATEGY, "SOL", qty=D("30"), managed=True)
    ex = make_executor(sim, repo, clock=clock)
    ex.reconcile_pending_intents()
    ex.cancel_strategy_open_orders(["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    ex.reconcile_positions()
    targets = {"BTCUSDT": 0.3, "ETHUSDT": 0.2}
    plan = plan_sells(
        targets, D("10000"), sim.get_balances(), RULES, BOOKS, AVG, REL, EXEC, managed_assets={"USDT", "SOL"}
    )
    assert [o.symbol for o in plan.sells] == ["SOLUSDT"]
    sell_rep = ex.execute(list(plan.sells))
    balances = sim.get_balances()
    buys = plan_buys(
        plan.buy_candidates,
        balances["USDT"].free,
        RULES,
        BOOKS,
        AVG,
        REL,
        EXEC,
        blocked_symbols=sell_rep.blocked_symbols,
    )
    buy_rep = ex.execute(list(buys.buys), balances=balances)
    assert all(o.status is IntentStatus.FILLED for o in sell_rep.outcomes + buy_rep.outcomes)
    final = ex.reconcile_positions()
    assert final.diffs == [] and "SOL" not in sim.get_balances()
    b = sim.get_balances()
    assert abs(b["BTC"].total * D("50005") - D("3000")) < D("20")
    assert {p.asset for p in repo.list_positions(ACCOUNT, STRATEGY, managed_only=True)} == {
        "BTC",
        "ETH",
        "SOL",
        "USDT",
    }
    # 立即重算：没有任何新订单（spec §14 第 3 条）
    again = plan_sells(
        targets, D("10000"), b, RULES, BOOKS, AVG, REL, EXEC, managed_assets={"USDT", "BTC", "ETH"}
    )
    assert again.sells == () and again.buy_candidates == ()


# ---- 小工具 ----


def test_intent_status_mapping() -> None:
    def r(status: OrderStatus, executed: str) -> OrderResult:
        return OrderResult("BTCUSDT", "c", status, D(executed), D("0"))

    assert intent_status_for(r(OrderStatus.FILLED, "1")) is IntentStatus.FILLED
    assert intent_status_for(r(OrderStatus.EXPIRED, "0.5")) is IntentStatus.PARTIAL
    assert intent_status_for(r(OrderStatus.EXPIRED_IN_MATCH, "0")) is IntentStatus.EXPIRED
    assert intent_status_for(r(OrderStatus.CANCELED, "0")) is IntentStatus.EXPIRED
    assert intent_status_for(r(OrderStatus.REJECTED, "0")) is IntentStatus.REJECTED
    assert intent_status_for(r(OrderStatus.NEW, "0")) is IntentStatus.UNKNOWN


def test_monotonic_clock() -> None:
    from datetime import UTC, datetime

    fixed = datetime(2026, 1, 1, tzinfo=UTC)
    c = MonotonicClock(lambda: fixed)
    a, b, d = c(), c(), c()
    assert a < b < d


def test_balances_fetched_when_not_given(tmp_path: Path) -> None:
    sim = make_sim({"USDT": D("10000"), "BTC": D("0.5")})
    repo = open_repo(tmp_path)
    make_executor(sim, repo).execute([order()])
    assert intents(repo)[0].base_balance_before == D("0.5")
