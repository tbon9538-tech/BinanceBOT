"""大盘状态机（spec §4.2）。

从可得的最早数据开始以 RISK_OFF 重放，得到确定的当前状态；回测与实盘使用同一个函数。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from bot.domain.types import Regime


def regime_series(close: pd.Series, sma_period: int, buffer: float) -> pd.DataFrame:
    """返回以日期为索引的 DataFrame：state（Regime 值）、switched_on、switched_off、sma。

    - RISK_OFF 且 c_t > SMA × (1 + buffer) → RISK_ON；
    - RISK_ON 且 c_t < SMA × (1 − buffer) → RISK_OFF；
    - 其他情况（包括 SMA 不可用、当天收盘缺失）保持原状态。
    """
    sma = close.rolling(sma_period, min_periods=sma_period).mean()
    c = close.to_numpy(dtype="float64")
    m = sma.to_numpy(dtype="float64")
    states: list[str] = []
    on_flags = np.zeros(len(c), dtype=bool)
    off_flags = np.zeros(len(c), dtype=bool)
    on = False
    for i in range(len(c)):
        if not (np.isnan(c[i]) or np.isnan(m[i])):
            if not on and c[i] > m[i] * (1 + buffer):
                on = True
                on_flags[i] = True
            elif on and c[i] < m[i] * (1 - buffer):
                on = False
                off_flags[i] = True
        states.append(Regime.RISK_ON.value if on else Regime.RISK_OFF.value)
    return pd.DataFrame(
        {"state": states, "switched_on": on_flags, "switched_off": off_flags, "sma": m, "close": c},
        index=close.index,
    )
