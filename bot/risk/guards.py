"""运行前风控判定（spec §5.1、§5.3、§5.4、§5.6）。纯函数：只做判断，不执行动作。

执行顺序：KILL 检查 → 系统状态检查 → 数据质量 → 无法解释的余额变化 → 回撤熔断 → 单日异常 → 生成订单。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from bot.domain.types import SystemState

ANOMALY_MAX_GAP_HOURS = 36


class Action(StrEnum):
    PROCEED = "PROCEED"  # 正常交易
    NO_BUYS = "NO_BUYS"  # 只允许卖出
    SKIP = "SKIP"  # 本次不交易（只做快照与报告）
    PAUSE = "PAUSE"  # 进入 PAUSED，不清仓
    HALT = "HALT"  # 回撤熔断：清仓并进入 HALTED
    KILL = "KILL"  # 紧急停止：清仓并进入 KILLED


@dataclass(frozen=True)
class GuardInput:
    kill_requested: bool
    system_state: SystemState
    data_ok: bool  # 数据质量检查（spec §3.3）
    unexplained: Mapping[str, Decimal]  # ledger.unexplained_changes 的结果（未确认的部分）
    navu: Decimal
    navu_peak: Decimal
    navu_prev: Decimal | None  # 上一次 PRE_TRADE 快照
    hours_since_prev: float | None
    price_deviation: Mapping[str, float]  # 资产 → |执行环境价格 / live 价格 − 1|；testnet 传空
    balances_ok: bool  # 余额接口返回正常、价格非零
    max_drawdown_halt: float
    daily_anomaly_limit: float
    data_sanity_max_deviation: float


@dataclass(frozen=True)
class GuardResult:
    action: Action
    reasons: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)  # 需要写入 events / 发送告警的类型

    @property
    def allow_buys(self) -> bool:
        return self.action is Action.PROCEED

    @property
    def allow_trading(self) -> bool:
        return self.action in (Action.PROCEED, Action.NO_BUYS)


def evaluate(g: GuardInput) -> GuardResult:
    if g.kill_requested:
        return GuardResult(Action.KILL, ["检测到 KILL 文件"], ["KILLED"])
    if g.system_state is not SystemState.RUNNING:
        return GuardResult(Action.SKIP, [f"系统状态为 {g.system_state.value}，本次不交易"])
    if not g.data_ok:
        return GuardResult(Action.SKIP, ["数据质量检查未通过"], ["DATA_NOT_READY"])

    events: list[str] = []
    reasons: list[str] = []
    no_buys = False
    if g.unexplained:
        no_buys = True
        events.append("UNEXPLAINED_BALANCE_CHANGE")
        reasons.append(f"无法解释的余额变化：{dict(g.unexplained)}，当天禁止买入")

    dd = g.navu / g.navu_peak - 1 if g.navu_peak > 0 else Decimal(0)
    if dd <= -Decimal(str(g.max_drawdown_halt)):
        bad = {a: d for a, d in g.price_deviation.items() if d > g.data_sanity_max_deviation}
        if bad or not g.balances_ok:
            return GuardResult(
                Action.PAUSE,
                [
                    f"回撤 {dd:.2%} 触发熔断阈值，但数据健全性检查失败（{bad or '余额/价格异常'}），只暂停不清仓"
                ],
                ["DATA_ANOMALY"],
            )
        return GuardResult(
            Action.HALT, [f"回撤 {dd:.2%} ≤ −{g.max_drawdown_halt:.0%}，熔断清仓"], ["DRAWDOWN_HALT"]
        )

    if g.navu_prev is not None and g.navu_prev > 0:
        if g.hours_since_prev is not None and g.hours_since_prev > ANOMALY_MAX_GAP_HOURS:
            reasons.append(f"距上次快照 {g.hours_since_prev:.0f} 小时，跳过单日异常检查")
        elif g.navu / g.navu_prev - 1 <= -Decimal(str(g.daily_anomaly_limit)):
            no_buys = True
            events.append("DAILY_ANOMALY")
            reasons.append(f"单位净值单日变化 {g.navu / g.navu_prev - 1:.2%}，当天禁止买入")

    return GuardResult(Action.NO_BUYS if no_buys else Action.PROCEED, reasons, events)
