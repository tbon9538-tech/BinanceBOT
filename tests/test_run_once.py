"""run_once 端到端测试：SimSpotAccount + 假行情源 + 临时数据库（不访问网络）。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from bot.app.daemon import alive_path, next_run_at, run_daemon
from bot.app.run_once import DailyRunner, managed_assets
from bot.backtest.sim_env import build_sim_deps, sim_config
from bot.config import BackupConfig
from bot.domain.types import Side, SystemState
from bot.persistence.backup import list_backups
from bot.persistence.repo import StateRepo

D = Decimal
RUN_DATE = date(2026, 11, 2)
T = RUN_DATE - timedelta(days=1)


@pytest.fixture
def env(tmp_path: Path, base_cfg: dict[str, Any]):  # type: ignore[no-untyped-def]
    """合成行情 + SimSpotAccount + 临时数据库；与故障演练共用 bot.backtest.sim_env。"""
    deps, acct, _, clock = build_sim_deps(sim_config(base_cfg), tmp_path, RUN_DATE)
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
    # 时钟静止时（成交记录时间晚于快照时间若干微秒），余额核对也不能误报（D-063）
    assert not deps.repo.list_events("mom1", type_="UNEXPLAINED_BALANCE_CHANGE")


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
    deps.market.withhold_last = {"BTCUSDT"}  # type: ignore[attr-defined]
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
    alive = json.loads(alive_path(deps.lock_file).read_text(encoding="utf-8"))
    assert alive["state"] == "running" and alive["next_run"].startswith(
        "2026-11-02T00:10"
    )  # 管理界面用（D-080）
    deps.kill_file.write_text("x")
    assert run_daemon(DailyRunner(deps), "00:10", now=clock.now, sleep=clock.sleep) == 0
    assert deps.repo.get_system_state("mom1").state == "KILLED"  # type: ignore[union-attr]


def test_preexisting_balances_are_external(env) -> None:  # type: ignore[no-untyped-def]
    """账户里原有的 BTC / A1 不被当成策略持仓（D-062；testnet 联调发现的问题）。"""
    deps, acct, clock = env
    acct.adjust_balance("BTC", D("1"))
    acct.adjust_balance("A1", D("50"))
    acct.adjust_balance("XYZ", D("7"))
    r = DailyRunner(deps).run_once()
    assert r.status == "DONE"
    bought = sum(o.quantity for o in acct.placed if o.symbol == "BTCUSDT" and o.side is Side.BUY)
    pos = deps.repo.get_position("spot-main", "mom1", "BTC")
    assert pos is not None and pos.qty <= bought  # 只算策略买入的部分（扣手续费）
    assert not [e for e in deps.repo.list_events("mom1", type_="RECONCILE_DIFF") if e.level != "INFO"]
    post = deps.repo.latest_nav_snapshot("mom1", "spot-main", kind="POST_TRADE")
    assert post is not None and post.nav_usdt < D("1100")  # 1000 USDT 起步，不含原有 1 BTC
    assert deps.repo.get_external_balances("spot-main", "mom1")["BTC"] == D("1")
    clock.t += timedelta(days=1)
    DailyRunner(deps).run_once()  # 次日：基线保持，没有"无法解释的余额变化"
    assert not deps.repo.list_events("mom1", type_="UNEXPLAINED_BALANCE_CHANGE")


def test_successful_run_pings_heartbeat_and_backs_up_state(env, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    deps, _, _ = env
    hits: list[str] = []

    def ok(req: httpx.Request) -> httpx.Response:
        hits.append(str(req.url))
        return httpx.Response(200)

    bdir = tmp_path / "backup"
    deps = replace(
        deps,
        cfg=deps.cfg.model_copy(update={"backup": BackupConfig(dir=bdir, keep=30)}),
        heartbeat_url="https://hc-ping.com/test-uuid",
        heartbeat_transport=httpx.MockTransport(ok),
        state_db=tmp_path / "state.db",
    )
    assert DailyRunner(deps).run_once().status == "DONE"
    assert hits == ["https://hc-ping.com/test-uuid"]
    [b] = list_backups(bdir, "state")
    copy = StateRepo.open(b)
    assert copy.last_run("mom1").status == "DONE"  # type: ignore[union-attr]
    assert {p.asset for p in copy.list_positions("spot-main", "mom1", managed_only=True)} == managed_assets(
        deps.repo, deps.cfg
    )
    copy.close()


def test_backup_failure_only_warns(env, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    deps, _, _ = env
    deps = replace(
        deps,
        cfg=deps.cfg.model_copy(update={"backup": BackupConfig(dir=tmp_path / "backup", keep=30)}),
        state_db=tmp_path / "missing.db",
    )
    assert DailyRunner(deps).run_once().status == "DONE"
    [ev] = deps.repo.list_events("mom1", type_="BACKUP_FAILED")
    assert ev.level == "WARN"


def test_max_position_counts_external_holdings(env) -> None:  # type: ignore[no-untyped-def]
    """MAX_POSITION 按整个账户计算：账户原有的 1 BTC（外部基线）也占额度（D-073）。"""
    deps, acct, _ = env
    acct.adjust_balance("BTC", D("1"))
    acct.rules["BTCUSDT"] = replace(acct.rules["BTCUSDT"], max_position=D("1.0001"))
    r = DailyRunner(deps).run_once()
    assert r.status == "DONE", r.messages
    assert not [o for o in acct.placed if o.symbol == "BTCUSDT"]  # 下单前就放弃，没有被交易所拒单
    assert [o for o in acct.placed if o.side is Side.BUY]  # 其他币照常买入
    [ev] = deps.repo.list_events("mom1", type_="FILTER_REJECTED")
    assert ev.level == "WARN" and "MAX_POSITION" in ev.payload_json


class _Inbox:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str, level: str = "INFO") -> bool:
        self.sent.append(text)
        return False


@pytest.mark.parametrize("mode", ["sma", "ensemble"])
def test_daily_report_shows_ensemble_multiplier(env, mode: str) -> None:  # type: ignore[no-untyped-def]
    deps, _, _ = env
    regime = deps.cfg.regime.model_copy(update={"mode": mode})
    inbox = _Inbox()
    deps = replace(deps, cfg=deps.cfg.model_copy(update={"regime": regime}), notifier=inbox)
    assert DailyRunner(deps).run_once().status == "DONE"
    [report] = [m for m in inbox.sent if "日报" in m]
    assert ("集成仓位乘数" in report) is (mode == "ensemble")
