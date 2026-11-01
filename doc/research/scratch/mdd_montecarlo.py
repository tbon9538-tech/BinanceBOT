"""一次性脚本：给定年化波动和夏普，模拟 N 年内最大回撤的分布（正态日收益，偏乐观——加密收益厚尾）。
用法：py -3.13 -I doc/research/scratch/mdd_montecarlo.py
"""
import numpy as np

rng = np.random.default_rng(7)
years, paths = 5, 4000
n = 365 * years
print(f"{'vol':>5} {'SR':>4} {'CAGR中位':>8} {'MDD中位':>8} {'MDD P10(差)':>11} {'P(MDD<=30%)':>12}")
for vol in (0.20, 0.25, 0.30, 0.40, 0.60):
    for sr in (0.8, 1.0, 1.3):
        mu_d = sr * vol / 365
        sd_d = vol / np.sqrt(365)
        r = rng.standard_t(df=4, size=(paths, n)) / np.sqrt(2.0) * sd_d + mu_d  # t(4) 厚尾，方差归一
        eq = np.cumprod(1 + np.clip(r, -0.9, None), axis=1)
        mdd = (eq / np.maximum.accumulate(eq, axis=1) - 1).min(axis=1)
        cagr = eq[:, -1] ** (1 / years) - 1
        print(f"{vol:5.2f} {sr:4.1f} {np.median(cagr):8.1%} {np.median(mdd):8.1%} {np.percentile(mdd,10):11.1%} {(mdd>=-0.30).mean():12.0%}")
