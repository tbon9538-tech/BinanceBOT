"""运维操作的核心逻辑：命令行（ops_commands）与管理界面（dashboard）共用（D-075）。

每个函数返回给人看的结果说明；状态不允许时抛 OpRefused，信息可以直接展示给用户。
KILL 文件与进程锁都取自 RunDeps，与常驻进程使用同一组路径。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from filelock import FileLock, Timeout

from bot.app.run_once import DailyRunner, RunDeps, RunResult, _prices_for, managed_assets
from bot.config import AppConfig
from bot.domain.types import Env, SystemState
from bot.persistence.repo import StateRepo
from bot.risk import ledger

KILL_LOCK_WAIT_SEC = 120.0


class OpRefused(Exception):
    """操作被拒绝（状态或参数不允许），没有做任何改动——kill 拿不到锁时除外，见 kill()。"""


def pause(cfg: AppConfig, repo: StateRepo, *, source: str) -> str:
    """暂停交易（不需要交易所 Key）：之后的运行只做快照和报告。"""
    repo.set_system_state(cfg.strategy_id, SystemState.PAUSED.value, reason=f"pause（{source}）")
    return "已暂停：之后的运行只做快照和报告，不交易"


def resume(cfg: AppConfig, repo: StateRepo, kill_file: Path, *, after_kill: bool, source: str) -> str:
    st = repo.get_system_state(cfg.strategy_id)
    state = SystemState(st.state) if st else SystemState.RUNNING
    if state is SystemState.KILLED and not after_kill:
        raise OpRefused("系统处于 KILLED（已清仓），恢复必须另外确认")
    if kill_file.exists():
        raise OpRefused(f"KILL 文件 {kill_file} 仍然存在，请先删除")
    repo.set_system_state(
        cfg.strategy_id, SystemState.RUNNING.value, reason=f"resume（原状态 {state.value}，{source}）"
    )
    return f"已恢复：{state.value} → RUNNING"


def kill(deps: RunDeps, *, reason: str, wait_sec: float = KILL_LOCK_WAIT_SEC) -> str:
    """紧急停止（spec §5.5）：先写 KILL 文件；有运行在进行时等它在下一笔订单前停下，再撤单、清仓。

    拿不到锁时 KILL 文件保留（正在运行的进程或常驻进程会接着执行 kill），并抛 OpRefused 说明情况。
    """
    deps.kill_file.parent.mkdir(parents=True, exist_ok=True)
    deps.kill_file.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
    lock = FileLock(str(deps.lock_file))
    try:
        lock.acquire(timeout=wait_sec)
    except Timeout:
        raise OpRefused(
            f"{wait_sec:.0f} 秒内拿不到进程锁；KILL 文件已保留，正在运行的进程会在下一笔订单前停止"
        ) from None
    try:
        res = DailyRunner(deps).kill(reason=reason)
    finally:
        lock.release()
    return f"已紧急停止：{res.status}"


def acknowledge(deps: RunDeps, *, note: str | None) -> str:
    """确认当前余额（例如 testnet 月度重置后），作为下一次"无法解释的余额变化"核对的基准（spec §5.2）。"""
    cfg, repo = deps.cfg, deps.repo
    runner = DailyRunner(deps)
    acct = runner._view()
    rules = acct.get_all_rules()
    balances = acct.get_balances()
    managed = managed_assets(repo, cfg)
    nav = ledger.compute_nav(balances, _prices_for(acct, managed, rules), managed).nav
    details = {
        "balances": {a: str(b.total) for a, b in balances.items()},
        "note": note,
        "fills_through": runner._fills_through(),
    }
    units, navu = runner._units(nav)
    repo.append_nav_snapshot(strategy_id=cfg.strategy_id, account_id=cfg.account_id, kind="ACK", nav_usdt=nav,
                             units=units, nav_per_unit=navu, gross_exposure=Decimal(0), details=details)  # fmt: skip
    return "已确认当前余额，作为下一次核对的基准"


def run_now(deps: RunDeps) -> RunResult:
    """立即执行一次每日流程（与 `run --once` 相同）。live 环境不允许从这里启动（spec §13 要求交互确认）。"""
    if deps.cfg.env is Env.LIVE:
        raise OpRefused("live 环境只能在命令行用 run --once --live-confirm 启动")
    return DailyRunner(deps).run_once()
