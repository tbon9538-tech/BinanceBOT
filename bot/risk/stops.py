"""ATR 追踪止损（spec §5.7，默认关闭）。纯函数，研究引擎与实盘共用。"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from bot.strategy.smoothing import StopWindow


@dataclass(frozen=True)
class StopState:
    entry_date: pd.Timestamp
    highest_close: float


def update_stop(
    state: StopState | None, t: pd.Timestamp, close: float, atr: float, multiplier: float, cooldown_days: int
) -> tuple[StopState | None, StopWindow | None]:
    """持仓存续期间每天调用一次（t 日收盘后）。

    返回 (新的状态, 触发时的剔除窗口)。触发条件：c_t < highest_close − multiplier × ATR_t。
    state 为 None 表示今天是入场日，从 c_t 开始记录。
    """
    if not math.isfinite(close):
        return state, None
    st = (
        StopState(t, close) if state is None else StopState(state.entry_date, max(state.highest_close, close))
    )
    if math.isfinite(atr) and close < st.highest_close - multiplier * atr:
        return None, StopWindow(t, t + pd.Timedelta(days=cooldown_days))
    return st, None
