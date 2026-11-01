"""CTREND 式趋势信号（研究 R3，预注册见 doc/research/log.md "R3 预注册"）。

受 Fieberg et al. 2025（JFQA）启发的简化版本，**不是论文复现**：
- 信号：价格 P_t / SMA_L − 1（L ∈ PRICE_LOOKBACKS）；成交量 ln(QVMA_L / QVMA_50)（L ∈ VOLUME_LOOKBACKS）；
- 每天在参与比较的币之间做 z 分数，截断到 ±Z_CLIP；
- 每 h 天一个锚点 s：在 s 日的标的池内，用 s 日的标准化信号对 s → s+h 的收益做横截面 OLS（含截距），得 b_s；
- t 日只用 s + h ≤ t 的最近 W 个 b_s 的平均（不使用尚未实现的收益）；不足 W/2 个时没有得分（当天不选币）。
信号周期在预注册中固定，作为常量放在这里（与 sleeves.BLEND_LOOKBACKS 相同的做法）；W 与 h 在配置中。
"""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np
import pandas as pd

from bot.config import XsMomentumSleeveConfig
from bot.strategy.indicators import MarketPanel

PRICE_LOOKBACKS = (3, 5, 10, 20, 50)
VOLUME_LOOKBACKS = (3, 5, 10, 20)
VOLUME_BASE = 50
Z_CLIP = 3.0
WINDOW_DAYS = max(*PRICE_LOOKBACKS, VOLUME_BASE)  # 计算全部信号所需的历史天数


def signal_panels(panel: MarketPanel, family: str) -> dict[str, pd.DataFrame]:
    """信号名 → 面板（行 = 日期，列 = 交易对）。family 为 price_volume 或 price。"""
    out: dict[str, pd.DataFrame] = {}
    for n in PRICE_LOOKBACKS:
        out[f"p{n}"] = panel._get(("ctrend_p", n), lambda n=n: panel.close / panel.sma(n) - 1)
    if family == "price_volume":
        qv = panel.quote_volume.where(panel.quote_volume > 0)

        def qma(n: int) -> pd.DataFrame:
            return panel._get(("ctrend_qma", n), lambda: qv.rolling(n, min_periods=n).mean())

        for n in VOLUME_LOOKBACKS:
            out[f"v{n}"] = panel._get(("ctrend_v", n), lambda n=n: np.log(qma(n) / qma(VOLUME_BASE)))
    return out


def zscore(x: pd.DataFrame) -> pd.DataFrame:
    """按列在行之间标准化（行 = 币，列 = 信号），截断到 ±Z_CLIP；某列没有离散度时置 0。"""
    sd = x.std(ddof=0)
    z = (x - x.mean()) / sd.where(sd > 0)
    return z.fillna(0.0).clip(-Z_CLIP, Z_CLIP)


class CtrendModel:
    """滚动横截面回归学习信号权重，并给出 t 日的得分。系数按锚点缓存。"""

    def __init__(
        self,
        panel: MarketPanel,
        cfg: XsMomentumSleeveConfig,
        universe: Callable[[pd.Timestamp], list[str]],
    ) -> None:
        self.panel = panel
        self.signals = signal_panels(panel, cfg.ctrend_signals)
        self.names = list(self.signals)
        self.h = cfg.ctrend_horizon_days
        self.window = cfg.ctrend_window_weeks
        self.min_coefs = math.ceil(self.window / 2)
        self.universe = universe
        self.anchors = panel.dates[:: self.h]
        self._coefs: dict[pd.Timestamp, np.ndarray | None] = {}

    def matrix(self, t: pd.Timestamp, cols: list[str]) -> pd.DataFrame:
        """t 日的原始信号（行 = 币，列 = 信号），去掉任一信号缺失的币。"""
        x = pd.DataFrame({k: v.loc[t, cols] for k, v in self.signals.items()}, index=cols, columns=self.names)
        return x.replace([np.inf, -np.inf], np.nan).dropna()

    def coef(self, s: pd.Timestamp) -> np.ndarray | None:
        """锚点 s 的横截面回归系数（不含截距）；样本不足时为 None。"""
        if s not in self._coefs:
            self._coefs[s] = self._fit(s)
        return self._coefs[s]

    def _fit(self, s: pd.Timestamp) -> np.ndarray | None:
        end = s + pd.Timedelta(days=self.h)
        if end not in self.panel.close.index:
            return None
        x = self.matrix(s, self.universe(s))
        fwd = (self.panel.close.loc[end, x.index] / self.panel.close.loc[s, x.index] - 1).dropna()
        x = x.loc[fwd.index]
        k = len(self.names)
        if len(x) < k + 2:
            return None
        a = np.column_stack([np.ones(len(x)), zscore(x).to_numpy()])
        beta, *_ = np.linalg.lstsq(a, fwd.to_numpy(), rcond=None)
        return beta[1:]

    def weights(self, t: pd.Timestamp) -> np.ndarray | None:
        """t 日可用的平均系数：只用 s + h ≤ t 的最近 W 个锚点。"""
        usable = self.anchors[self.anchors <= t - pd.Timedelta(days=self.h)][-self.window :]
        coefs = [c for s in usable if (c := self.coef(s)) is not None]
        if len(coefs) < self.min_coefs:
            return None
        return np.mean(coefs, axis=0)

    def score(self, t: pd.Timestamp, cols: list[str]) -> pd.Series:
        """预期收益得分；没有可用系数或信号缺失的币为 NaN。"""
        out = pd.Series(np.nan, index=cols, dtype="float64")
        w = self.weights(t)
        if w is None or not cols:
            return out
        x = self.matrix(t, cols)
        if x.empty:
            return out
        out.loc[x.index] = zscore(x).to_numpy() @ w
        return out
