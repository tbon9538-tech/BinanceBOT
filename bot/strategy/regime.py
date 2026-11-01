"""大盘状态机（spec §4.2）。

从可得的最早数据开始以 RISK_OFF 重放，得到确定的当前状态；回测与实盘使用同一个函数。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from bot.domain.types import Regime


def regime_series(
    close: pd.Series, sma_period: int, buffer: float, *, mode: str = "sma", tsmom_lookback: int = 90
) -> pd.DataFrame:
    """返回以日期为索引的 DataFrame：state（Regime 值）、switched_on、switched_off、sma。

    - RISK_OFF 且 c_t > SMA × (1 + buffer) → RISK_ON；
    - RISK_ON 且 c_t < SMA × (1 − buffer) → RISK_OFF；
    - 其他情况（包括 SMA 不可用、当天收盘缺失）保持原状态。
    """
    sma = close.rolling(sma_period, min_periods=sma_period).mean()
    if mode == "tsmom":
        # 研究对照 E2b：把"价格相对 SMA"换成"价格相对 tsmom_lookback 天前的收盘价"，其余规则相同
        sma = close.shift(tsmom_lookback)
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


def regime_with_exposure(
    close: pd.Series,
    sma_period: int,
    buffer: float,
    *,
    mode: str = "sma",
    tsmom_lookback: int = 90,
    ensemble_periods: tuple[int, ...] = (50, 100, 150, 200),
) -> pd.DataFrame:
    """在 regime_series 的基础上增加 exposure 列（总仓位乘数）。

    - sma / tsmom：Risk-On 时 1.0，Risk-Off 时 0.0（与原规则完全一致）；
    - ensemble（研究 R2）：对每条均线分别运行同一带缓冲的状态机，exposure = 处于 Risk-On 的比例；
      exposure > 0 视为 RISK_ON，切换标记按 exposure 是否从 0 变为正数（或反之）计算。
    """
    if mode != "ensemble":
        df = regime_series(close, sma_period, buffer, mode=mode, tsmom_lookback=tsmom_lookback)
        df["exposure"] = (df["state"] == Regime.RISK_ON.value).astype("float64")
        return df
    parts = [regime_series(close, p, buffer)["state"] == Regime.RISK_ON.value for p in ensemble_periods]
    exposure = pd.concat(parts, axis=1).mean(axis=1).astype("float64")
    on = exposure > 0
    prev = on.shift(1, fill_value=False)
    return pd.DataFrame(
        {
            "state": np.where(on, Regime.RISK_ON.value, Regime.RISK_OFF.value),
            "switched_on": (on & ~prev).to_numpy(),
            "switched_off": (~on & prev).to_numpy(),
            "sma": np.nan,
            "close": close.to_numpy(dtype="float64"),
            "exposure": exposure.to_numpy(),
        },
        index=close.index,
    )
