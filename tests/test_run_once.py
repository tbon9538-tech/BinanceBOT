"""run_once 端到端测试：SimSpotAccount + 假行情源 + 临时数据库（不访问网络）。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from bot.app.daemon import next_run_at, run_daemon
from bot.app.run_once import DailyRunner, RunDeps
from bot.backtest.sim_account import SimSpotAccount
from bot.config import AppConfig, AssetsConfig
from bot.data.store import MarketStore
from bot.domain.types import Side, SystemState
from bot.notify.telegram import TelegramNotifier
from bot.persistence.repo import StateRepo
from tests.conftest import CONFIG_PATH
from tests.exec_helpers import make_rules
from tests.helpers import raw_frame

D = Decimal
RUN_DATE = date(2026, 11, 2)
T = RUN_DATE - timedelta(days=1)
ALTS = [f"A{i}" for i in range(1, 7)]
N_DAYS = 420


def _series(seed: int, drift: float, vol: float) -> list[float]:
    rng = np.random.default_rng(seed)
    return list(100 * np.cumprod(1 + drift + rng.normal(0, vol, N_DAYS)))


def market_frames() -> dict[str, pd.DataFrame]:
    start = T - timedelta(days=N_DAYS - 1)
    frames = {"BTCUSDT": raw_frame(start, N_DAYS, close=lambda i, s=_series(1, 0.003, 0.02): s[i], qv=lambda i: 5e9)}
    for k, a in enumerate(ALTS):
        s = _series(10 + k, 0.004 - 0.0005 * k, 0.04)
        frames[f"{a}USDT"] = raw_frame(start, N_DAYS, close=lambda i, s=s: s[i], qv=lambda i, k=k: 1e8 * (k + 1))
    return frames


class FakeMarket:
    def __init__(self, frames: Mapping[str, pd.DataFrame], withhold_last: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        self.frames = frames
        self.withhold_last = set(withhold_last)

    def get_exchange_info(self) -> Mapping[str, Any]:
        return {
            "symbols": [
                {"symbol": s, "baseAsset": s[:-4], "quoteAsset": "USDT", "status": "TRADING", "isSpotTradingAllowed": True}
                for s in self.frames
            ]
        }

    def get_daily_klines(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        df = self.frames[symbol]
        if symbol in self.withhold_last:
            end = end - timedelta(days=1)
        return df[(df["date"] >= start.isoformat()) & (df["date"] <= end.isoformat())].reset_index(drop=True)


def make_account(frames: Mapping[str, pd.DataFrame], usdt: str = "1000") -> SimSpotAccount:
    last = {s: D(f["close"].iloc[-1]) for s, f in frames.items()}
    rules = {s: make_rules(s, s[:-4], step="0.00001", tick="0.0001") for s in frames}
    books = {s: (p * D("0.9995"), p * D("1.0005")) for s, p in last.items()}
    return SimSpotAccount(rules=rules, balances={"USDT": D(usdt)}, books=books, avg_prices=last, prices=last, account_id="spot-main")


def make_cfg() -> AppConfig:
    import yaml

    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    raw["universe"].update({"min_median_quote_volume": 0, "min_listing_days": 60})
    raw["data"]["history_days"] = 400
    raw["notify"]["daily_report"] = True
    return AppConfig.model_validate(raw)


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += timedelta(seconds=s)


@pytest.fixture
def env(tmp_path: Path):  # type: ignore[no-untyped-def]
    frames = market_frames()
    clock = Clock(datetime(RUN_DATE.year, RUN_DATE.month, RUN_DATE.day, 0, 10, tzinfo=UTC))
    acct = make_account(frames)
    deps = RunDeps(
        cfg=make_cfg(),
        assets=AssetsConfig(approved_assets=["BTC", *ALTS]),
        account=acct,
        market=FakeMarket(frames),
        store=MarketStore(tmp_path / "market.db"),
        repo=StateRepo.open(tmp_path / "state.db", clock=clock.now),
        notifier=TelegramNotifier(token="", chat_id=""),
        kill_file=tmp_path / "KILL",
        lock_file=tmp_path / "run.lock",
        now=clock.now,
        sleep=clock.sleep,
        data_retry_sleep_sec=0.0,
    )
    return deps, acct, clock


def test_first_run_builds_positions_then_is_idempotent(env) -> None:  # type: ignore[no-untyped-def]
    deps, acct, _ = env
    r = DailyRunner(deps).run_once()
    assert r.status == "DONE", r.messages
    buys = [o for o in acct.placed if o.side is Side.BUY]
    assert buys and any(o.symbol == "BTCUSDT" for o in buys)
    assert all(o.client_order_id.startswith("mom1-261102-") for o in acct.placed)
    managed = {p.asset for p in deps.repo.list_positions("spot-main", "mom1", managed_only=True)}
    assert "BTC" in managed
    kinds = [s.kind for s in deps.repo.list_nav_snapshots("mom1", "spot-main")]
    assert kinds == ["PRE_TRADE", "POST_TRADE"]
    assert deps.repo.get_daily_targets("mom1", T)
    n = len(acct.placed)
    r2 = DailyRunner(deps).run_once()  # 同一运行日再跑：只更新快照，不下单
    assert r2.status == "DONE" and len(acct.placed) == n
    assert any("今天已完成调仓" in m for m in r2.messages)


def test_dry_run_places_no_orders(env) -> None:  # type: ignore[no-untyped-def]
    deps, acct, _ = env
    r = DailyRunner(deps).run_once(dry_run=True)
    assert r.status == "DRY_RUN"
    assert acct.placed == []
    assert any("[dry-run]" in m for m in r.messages)


def test_kill_file_liquidates_and_stops(env) -> None:  # type: ignore[no-untyped-def]
    deps, acct, clock = env
    DailyRunner(deps).run_once()
    held_before = {a for a, b in acct.get_balances().items() if a != "USDT" and b.total > 0}
    assert held_before
    deps.kill_file.write_text("x")
    clock.t += timedelta(days=1)
    r = DailyRunner(deps).run_once()
    assert r.status == "KILLED"
    assert not deps.kill_file.exists()
    st = deps.repo.get_system_state("mom1")
    assert st is not None and st.state == SystemState.KILLED.value
    sells = [o for o in acct.placed if o.side is Side.SELL and "-K" in o.client_order_id]
    assert {o.symbol.removesuffix("USDT") for o in sells} == held_before
    n = len(acct.placed)
    clock.t += timedelta(days=1)
    r3 = DailyRunner(deps).run_once()  # KILLED 之后不再交易
    assert len(acct.placed) == n and r3.status == "SKIPPED"


def test_data_not_ready_retries_then_skips(env) -> None:  # type: ignore[no-untyped-def]
    deps, acct, clock = env
    deps.market = FakeMarket(market_frames(), withhold_last={"BTCUSDT"})
    r = DailyRunner(deps).run_once()
    assert r.status == "SKIPPED"
    assert len(clock.sleeps) == 6  # spec §3.2：最多重试 6 次
    assert acct.placed == []
    assert any(e.type == "DATA_NOT_READY" for e in deps.repo.list_events("mom1"))


def test_unknown_status_is_reconciled_without_duplicate(env) -> None:  # type: ignore[no-untyped-def]
    deps, acct, clock = env
    acct.inject_place_fault("timeout", executed=True, symbol="BTCUSDT")  # 已成交但响应丢失
    r = DailyRunner(deps).run_once()
    assert r.status == "DONE"
    btc_orders = [o for o in acct.placed if o.symbol == "BTCUSDT"]
    assert len(btc_orders) == 1  # 状态未知后不重下
    clock.t += timedelta(days=1)
    DailyRunner(deps).run_once()
    cids = [o.client_order_id for o in acct.placed]
    assert len(cids) == len(set(cids))  # 全局唯一
    btc_pos = deps.repo.get_position("spot-main", "mom1", "BTC")
    assert btc_pos is not None and btc_pos.qty > 0


def test_locked_run_exits(env) -> None:  # type: ignore[no-untyped-def]
    from filelock import FileLock

    deps, acct, _ = env
    with FileLock(str(deps.lock_file)):
        assert DailyRunner(deps).run_once().status == "LOCKED"
    assert acct.placed == []


def test_daemon_runs_daily_and_stops_on_kill(env) -> None:  # type: ignore[no-untyped-def]
    deps, acct, clock = env
    clock.t = datetime(2026, 11, 2, 0, 5, tzinfo=UTC)
    assert next_run_at(clock.t, "00:10") == datetime(2026, 11, 2, 0, 10, tzinfo=UTC)
    run_daemon(DailyRunner(deps), "00:10", now=clock.now, sleep=clock.sleep, max_cycles=1)
    assert acct.placed  # 到点后执行了一次
    deps.kill_file.write_text("x")
    assert run_daemon(DailyRunner(deps), "00:10", now=clock.now, sleep=clock.sleep) == 0
    assert deps.repo.get_system_state("mom1").state == "KILLED"  # type: ignore[union-attr]
