"""执行层测试共用的构造工具：规则、时钟、SimSpotAccount、执行器。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from bot.backtest.sim_account import SimSpotAccount
from bot.config import ExecutionConfig, RebalanceConfig
from bot.domain.types import SymbolRules, SymbolStatus
from bot.execution.spot_executor import SpotExecutor
from bot.persistence.repo import StateRepo

D = Decimal
RUN_DATE = date(2026, 11, 2)
STRATEGY = "mom1"
ACCOUNT = "spot-main"
EXEC = ExecutionConfig(unknown_status_query_delays_sec=[1.0, 3.0, 10.0, 30.0, 60.0])
REL = RebalanceConfig(threshold_mode="relative", threshold_rel=0.25, min_trade_usdt=D("10"))
ABS = RebalanceConfig(threshold_mode="absolute", threshold=0.02, min_trade_usdt=D("10"))


def make_rules(
    symbol: str,
    base: str,
    *,
    tick: str = "0.01",
    step: str = "0.0001",
    min_qty: str | None = None,
    max_qty: str = "9000",
    min_notional: str = "5",
    max_notional: str | None = "9000000",
    status: SymbolStatus = SymbolStatus.TRADING,
    pps: bool = True,
) -> SymbolRules:
    return SymbolRules(
        symbol=symbol,
        base_asset=base,
        quote_asset="USDT",
        status=status,
        tick_size=D(tick),
        min_price=D(tick),
        max_price=D("1000000"),
        step_size=D(step),
        min_qty=D(min_qty if min_qty is not None else step),
        max_qty=D(max_qty),
        min_notional=D(min_notional),
        max_notional=D(max_notional) if max_notional is not None else None,
        bid_multiplier_up=D("1.2") if pps else None,
        bid_multiplier_down=D("0.5") if pps else None,
        ask_multiplier_up=D("2") if pps else None,
        ask_multiplier_down=D("0.8") if pps else None,
        avg_price_mins=5 if pps else None,
    )


RULES: dict[str, SymbolRules] = {
    "BTCUSDT": make_rules("BTCUSDT", "BTC", step="0.00001"),
    "ETHUSDT": make_rules("ETHUSDT", "ETH", step="0.0001"),
    "SOLUSDT": make_rules("SOLUSDT", "SOL", step="0.001"),
}
BOOKS: dict[str, tuple[Decimal, Decimal]] = {
    "BTCUSDT": (D("50000"), D("50010")),
    "ETHUSDT": (D("3000"), D("3001")),
    "SOLUSDT": (D("100"), D("100.1")),
}
AVG: dict[str, Decimal] = {s: (b + a) / 2 for s, (b, a) in BOOKS.items()}


class TickClock:
    """每次调用前进 1 毫秒的 UTC 时钟。"""

    def __init__(self, start: datetime = datetime(2026, 11, 2, 0, 10, tzinfo=UTC)) -> None:
        self.t = start

    def __call__(self) -> datetime:
        self.t += timedelta(milliseconds=1)
        return self.t


class Sleeper:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, s: float) -> None:
        self.calls.append(s)


def make_sim(balances: Mapping[str, Decimal] | None = None, **kw: Any) -> SimSpotAccount:
    return SimSpotAccount(
        rules=kw.pop("rules", RULES),
        balances=balances if balances is not None else {"USDT": D("10000")},
        books=kw.pop("books", BOOKS),
        avg_prices=kw.pop("avg_prices", AVG),
        account_id=ACCOUNT,
        **kw,
    )


def open_repo(tmp_path: Path, clock: TickClock | None = None) -> StateRepo:
    return StateRepo.open(tmp_path / "state.db", clock=clock or TickClock())


def make_executor(
    account: Any,
    repo: StateRepo,
    *,
    run_id: str = "mom1:2026-11-02",
    run_date: date = RUN_DATE,
    clock: TickClock | None = None,
    sleeper: Sleeper | None = None,
    **kw: Any,
) -> SpotExecutor:
    return SpotExecutor(
        account,
        repo,
        kw.pop("exec_cfg", EXEC),
        strategy_id=STRATEGY,
        account_id=ACCOUNT,
        run_id=run_id,
        run_date=run_date,
        sleep=sleeper or Sleeper(),
        clock=clock or TickClock(),
        **kw,
    )
