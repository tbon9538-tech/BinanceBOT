"""统计指标（spec §1.2）、Deflated Sharpe Ratio、block bootstrap（spec §9.5）。"""

from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np
import pandas as pd

ANN_DAYS = 365
EULER_GAMMA = 0.5772156649015329
_N = NormalDist()


def daily_returns(nav: pd.Series) -> pd.Series:
    return nav.pct_change().dropna()


def cagr(nav: pd.Series) -> float:
    days = (nav.index[-1] - nav.index[0]).days
    if days <= 0 or nav.iloc[0] <= 0:
        return float("nan")
    return float((nav.iloc[-1] / nav.iloc[0]) ** (ANN_DAYS / days) - 1)


def ann_vol(r: pd.Series) -> float:
    return float(r.std(ddof=1) * math.sqrt(ANN_DAYS))


def sharpe(r: pd.Series, annual_rf: float = 0.0) -> float:
    """年化夏普（spec §1.2）。annual_rf > 0 时为扣除该年化收益（例如 USDT 理财）后的超额夏普。"""
    sd = r.std(ddof=1)
    rf = (1 + annual_rf) ** (1 / ANN_DAYS) - 1
    return float((r.mean() - rf) / sd * math.sqrt(ANN_DAYS)) if sd and sd > 0 else float("nan")


def drawdown(nav: pd.Series) -> pd.Series:
    return nav / nav.cummax() - 1


def max_drawdown(nav: pd.Series) -> float:
    return float(drawdown(nav).min())


def longest_drawdown_days(nav: pd.Series) -> int:
    peak_date = nav.index[0]
    peak = nav.iloc[0]
    longest = 0
    for d, v in nav.items():
        if v >= peak:
            peak, peak_date = v, d
        longest = max(longest, (pd.Timestamp(str(d)) - pd.Timestamp(str(peak_date))).days)
    return longest


def drawdown_episodes(nav: pd.Series, n: int = 5, min_depth: float = 0.03) -> list[dict[str, object]]:
    """最大的 n 段回撤：峰值日、谷底日、恢复日（未恢复为 None）、幅度。"""
    dd = drawdown(nav)
    used = pd.Series(False, index=nav.index)
    out: list[dict[str, object]] = []
    for _ in range(n):
        cand = dd[~used]
        if cand.empty or cand.min() > -min_depth:
            break
        trough = cand.idxmin()
        peak = nav.loc[:trough].idxmax()
        after = nav.loc[trough:]
        rec = after[after >= nav.loc[peak]]
        recovery = rec.index[0] if len(rec) else None
        out.append(
            {
                "peak": pd.Timestamp(peak).date().isoformat(),
                "trough": pd.Timestamp(trough).date().isoformat(),
                "recovery": pd.Timestamp(recovery).date().isoformat() if recovery is not None else None,
                "depth": float(dd.loc[trough]),
            }
        )
        used.loc[peak : recovery if recovery is not None else nav.index[-1]] = True
    return out


def yearly_returns(nav: pd.Series) -> dict[str, float]:
    ends = nav.groupby(nav.index.year).last()
    starts = pd.concat([pd.Series([nav.iloc[0]], index=[ends.index[0] - 1]), ends.iloc[:-1]])
    starts.index = ends.index
    return {str(y): float(ends[y] / starts[y] - 1) for y in ends.index}


def summarize(nav: pd.Series) -> dict[str, float]:
    r = daily_returns(nav)
    c = cagr(nav)
    m = max_drawdown(nav)
    return {
        "cagr": c,
        "vol": ann_vol(r),
        "sharpe": sharpe(r),
        "mdd": m,
        "calmar": c / -m if m < 0 else float("nan"),
        "longest_dd_days": float(longest_drawdown_days(nav)),
        "days_le_m10": float((r <= -0.10).sum()),
        "days_le_m15": float((r <= -0.15).sum()),
    }


def deflated_sharpe(
    sr_hat: float, n_trials: int, trials_sr_var: float, n_obs: int, skew: float, kurt: float
) -> float:
    """Bailey & López de Prado (2014)。所有 Sharpe 均为**每期**（日）值；kurt 为非超额峰度。"""
    if n_trials > 1 and trials_sr_var > 0:
        sr0 = math.sqrt(trials_sr_var) * (
            (1 - EULER_GAMMA) * _N.inv_cdf(1 - 1 / n_trials)
            + EULER_GAMMA * _N.inv_cdf(1 - 1 / (n_trials * math.e))
        )
    else:
        sr0 = 0.0
    denom = 1 - skew * sr_hat + (kurt - 1) / 4 * sr_hat**2
    if denom <= 0 or n_obs < 2:
        return float("nan")
    return float(_N.cdf((sr_hat - sr0) * math.sqrt(n_obs - 1) / math.sqrt(denom)))


def dsr_from_returns(r: pd.Series, trial_annual_sharpes: list[float]) -> float:
    """用策略日收益与全部试验的年化 Sharpe 计算 DSR。"""
    sr_hat = float(r.mean() / r.std(ddof=1))
    per_period = [s / math.sqrt(ANN_DAYS) for s in trial_annual_sharpes if math.isfinite(s)]
    var = float(np.var(per_period, ddof=1)) if len(per_period) > 1 else 0.0
    return deflated_sharpe(sr_hat, max(len(per_period), 1), var, len(r), float(r.skew()), float(r.kurt() + 3))


def block_bootstrap_mdd(r: pd.Series, *, block_days: int = 30, n: int = 1000, seed: int = 0) -> np.ndarray:
    """环形 block bootstrap：重采样日收益序列，返回 n 个最大回撤。"""
    rng = np.random.default_rng(seed)
    x = r.to_numpy(dtype="float64")
    T = len(x)
    out = np.empty(n)
    n_blocks = math.ceil(T / block_days)
    for i in range(n):
        starts = rng.integers(0, T, n_blocks)
        idx = (starts[:, None] + np.arange(block_days)[None, :]).ravel()[:T] % T
        eq = np.cumprod(1 + x[idx])
        out[i] = float((eq / np.maximum.accumulate(eq) - 1).min())
    return out
