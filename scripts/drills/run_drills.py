"""故障演练（dev_plan M6-7，spec §14 第 4 条），全部在离线模拟环境中运行，不访问网络。

用法（项目根目录）：  uv run python scripts/drills/run_drills.py
testnet 上的对应步骤见 README「故障演练」一节。
"""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bot.app.run_once import DailyRunner
from bot.backtest.sim_env import build_sim_deps, sim_config
from bot.domain.types import Side

RUN_DATE = date(2026, 11, 2)
CFG = sim_config(yaml.safe_load(Path("config/config.yaml").read_text(encoding="utf-8")))


def drill_crash_then_restart(work: Path) -> str:
    deps, acct, _, _ = build_sim_deps(CFG, work, RUN_DATE)
    acct.inject_place_fault("crash", executed=True, symbol="BTCUSDT")  # 卖买之间进程崩溃（订单已到达交易所）
    r1 = DailyRunner(deps).run_once()
    assert r1.status == "FAILED", r1
    r2 = DailyRunner(deps).run_once()
    btc = [o for o in acct.placed if o.symbol == "BTCUSDT" and o.side is Side.BUY]
    assert r2.status == "DONE" and len(btc) == 1, (r2.status, len(btc))
    return "崩溃后重启：遗留意图被对账确认，BTC 没有重复下单"


def drill_timeout(work: Path) -> str:
    deps, acct, _, clock = build_sim_deps(CFG, work, RUN_DATE)
    acct.inject_place_fault("timeout", executed=True, symbol="BTCUSDT")
    DailyRunner(deps).run_once()
    clock.t += timedelta(days=1)
    DailyRunner(deps).run_once()
    cids = [o.client_order_id for o in acct.placed]
    assert len(cids) == len(set(cids))
    assert sum(1 for o in acct.placed if o.symbol == "BTCUSDT" and o.side is Side.BUY) <= 2
    return "下单超时：状态未知的意图按 spec §6.6 查询，次日对账，没有用新 ID 重下同一意图"


def drill_kill_file(work: Path) -> str:
    deps, acct, _, clock = build_sim_deps(CFG, work, RUN_DATE)
    DailyRunner(deps).run_once()
    deps.kill_file.write_text("x")
    clock.t += timedelta(days=1)
    r = DailyRunner(deps).run_once()
    left = {a: b.total for a, b in acct.get_balances().items() if a != "USDT" and b.total > 0}
    st = deps.repo.get_system_state("mom1")
    assert r.status == "KILLED" and not deps.kill_file.exists() and st is not None and st.state == "KILLED"
    clock.t += timedelta(days=1)
    n = len(acct.placed)
    DailyRunner(deps).run_once()
    assert len(acct.placed) == n
    return f"KILL 文件：撤单、清仓（剩余灰尘 {left or '无'}）、进入 KILLED，重启后不再交易"


def drill_network_down(work: Path) -> str:
    deps, acct, market, clock = build_sim_deps(CFG, work, RUN_DATE)
    market.offline = True
    r1 = DailyRunner(deps).run_once()
    events = [e.type for e in deps.repo.list_events("mom1")]
    assert r1.status == "FAILED" and "UNHANDLED_EXCEPTION" in events and acct.placed == []
    market.offline = False
    r2 = DailyRunner(deps).run_once()
    assert r2.status == "DONE"
    return "断网：运行失败并告警（UNHANDLED_EXCEPTION），恢复网络后下一次运行正常"


def drill_balance_reset(work: Path) -> str:
    deps, acct, _, clock = build_sim_deps(CFG, work, RUN_DATE)
    DailyRunner(deps).run_once()
    for a, b in list(acct.get_balances().items()):  # 模拟 testnet 月度重置：余额被外部改写
        acct.adjust_balance(a, -b.total)
    acct.adjust_balance("USDT", Decimal("10000"))
    clock.t += timedelta(hours=1)  # 同一运行日（合成行情没有下一天的数据），强制调仓以检验是否禁止买入
    n = len(acct.placed)
    r = DailyRunner(deps).run_once(force_rebalance=True)
    assert any(e.type == "UNEXPLAINED_BALANCE_CHANGE" for e in deps.repo.list_events("mom1"))
    assert not [o for o in acct.placed[n:] if o.side is Side.BUY]
    assert any("禁止买入" in m for m in r.messages)
    return "余额被外部重置：识别为无法解释的余额变化，当天禁止买入；人工 `capital --acknowledge` 后恢复"


DRILLS: list[tuple[str, Callable[[Path], str]]] = [
    ("1 下单途中进程崩溃后重启", drill_crash_then_restart),
    ("2 下单超时（状态未知）", drill_timeout),
    ("3 KILL 文件", drill_kill_file),
    ("4 断网", drill_network_down),
    ("5 testnet 余额重置", drill_balance_reset),
]


def main() -> int:
    failed = 0
    for name, fn in DRILLS:
        with tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        ) as d:  # Windows 上 SQLite 文件可能仍被占用
            try:
                print(f"[PASS] {name}：{fn(Path(d))}")
            except AssertionError as e:
                failed += 1
                print(f"[FAIL] {name}：{e!r}")
    print("全部通过" if not failed else f"{failed} 项失败")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
