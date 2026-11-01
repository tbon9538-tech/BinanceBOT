"""平滑后的目标 S_t（spec §4.4，替代 v2 的 tranche 机制）。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import pandas as pd

DayTargets = Mapping[str, float]


@dataclass(frozen=True)
class StopWindow:
    """止损剔除窗口：stop_date 当天及之前、直到 cooldown_until 计算出的目标都不得包含该币。"""

    stop_date: pd.Timestamp
    cooldown_until: pd.Timestamp


@dataclass(frozen=True)
class Overrides:
    """F(·) 的剔除规则输入（spec §4.4）。键都是面板列名。"""

    always_excluded: frozenset[str] = frozenset()  # force_exit / 黑名单 / frozen
    stops: Mapping[str, StopWindow] = field(default_factory=dict)

    def blocked_at(self, d: pd.Timestamp) -> frozenset[str]:
        """d 日计算原始目标时处于冷却期的列（spec §4.3 候选条件）。"""
        return frozenset(c for c, w in self.stops.items() if w.stop_date <= d <= w.cooldown_until)

    def excluded(self, col: str, d: pd.Timestamp) -> bool:
        if col in self.always_excluded:
            return True
        w = self.stops.get(col)
        return w is not None and d <= w.cooldown_until


def _filter(d_targets: DayTargets, d: pd.Timestamp, ov: Overrides) -> dict[str, float]:
    return {c: w for c, w in d_targets.items() if not ov.excluded(c, d)}


def smoothed_target(
    raw: Callable[[pd.Timestamp], DayTargets],
    t: pd.Timestamp,
    *,
    risk_on: bool,
    mode: str,
    n: int,
    weekday: int,
    risk_on_mode: str,
    last_switch_on: pd.Timestamp | None,
    overrides: Overrides,
) -> dict[str, float]:
    """raw(d) 返回 d 日的原始目标 D_d。

    - Risk-Off 当天立即清零；
    - smoothed：最近 n 天 F(D) 的算术平均；weekly：最近一个 rebalance_weekday 的 F(D)；
    - risk_on_mode == "immediate"：窗口中早于最近一次切换日 s 的 D 用 D_s 替换（等价于切换当天全量重算）；
      "ramp"：不替换，Risk-Off 期间的空目标自然实现 n 天线性建仓。
    """
    if not risk_on:
        return {}

    def source_date(d: pd.Timestamp) -> pd.Timestamp:
        if risk_on_mode == "immediate" and last_switch_on is not None and d < last_switch_on:
            return last_switch_on
        return d

    if mode == "weekly":
        back = (t.day_of_week - weekday) % 7
        src = source_date(t - pd.Timedelta(days=back))
        return _filter(raw(src), src, overrides)

    acc: dict[str, float] = {}
    for j in range(n):
        src = source_date(t - pd.Timedelta(days=j))
        for c, w in _filter(raw(src), src, overrides).items():
            acc[c] = acc.get(c, 0.0) + w / n
    return {c: w for c, w in acc.items() if w > 0}
