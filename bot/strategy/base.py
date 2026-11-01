"""策略接口（spec §11.2）。compute_targets 必须是纯函数：只读 ctx，不做任何 I/O。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

import pandas as pd

from bot.domain.types import InstrumentKey, TargetPortfolio


@dataclass(frozen=True)
class StrategyContext:
    """策略计算所需的全部输入。M2 会按 spec §4 补充字段（止损事件、强制退出名单等）。"""

    as_of: date  # t：最近一根已收盘日线的日期
    daily_klines: Mapping[str, pd.DataFrame]  # symbol → 截至 t 的已收盘日线
    tradable_symbols: frozenset[str] = field(default_factory=frozenset)  # 执行环境中状态为 TRADING 的交易对


class Strategy(Protocol):
    strategy_id: str

    def required_symbols(self, ctx: StrategyContext) -> set[InstrumentKey]: ...

    def compute_targets(self, ctx: StrategyContext) -> TargetPortfolio: ...
