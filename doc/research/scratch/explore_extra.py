"""一次性脚本：基于 explore_backtest.py 的输出做补充分析（回撤区间、前后半段、BTC 趋势同风险对照、组合）。
用法（项目根目录）：
    py -3.13 -I doc/research/scratch/explore_extra.py doc/research/scratch/data doc/research/scratch/out
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

COST = 0.0015


def stats(r):
    eq = (1 + r).cumprod()
    yrs = len(r) / 365
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    sr = r.mean() / r.std() * np.sqrt(365)
    mdd = (eq / eq.cummax() - 1).min()
    return dict(CAGR=cagr, Vol=r.std() * np.sqrt(365), Sharpe=sr, MDD=mdd, Calmar=cagr / -mdd)


def dd_episodes(eq, n=3):
    dd = eq / eq.cummax() - 1
    out, used = [], pd.Series(False, index=eq.index)
    for _ in range(n):
        d = dd[~used]
        if d.min() > -0.05:
            break
        trough = d.idxmin()
        peak = eq.loc[:trough].idxmax()
        rec = eq.loc[trough:][eq.loc[trough:] >= eq.loc[peak]]
        end = rec.index[0] if len(rec) else eq.index[-1]
        out.append((peak.date(), trough.date(), end.date() if len(rec) else "未恢复", round(dd[trough], 3)))
        used.loc[peak:end] = True
    return out


def btc_trend_vt(data_dir, start, end, target_vol, sma=100, buf=0.02, vol_lb=30, max_gross=0.95):
    df = pd.read_csv(data_dir / "klines_1d" / "BTCUSDT.csv", index_col="date", parse_dates=True)
    c, o = df["close"], df["open"]
    m = c.rolling(sma).mean()
    vol = c.pct_change().rolling(vol_lb).std() * np.sqrt(365)
    on, w_sig = False, pd.Series(0.0, index=c.index)
    for t in c.index:
        if not np.isnan(m[t]):
            if not on and c[t] > m[t] * (1 + buf):
                on = True
            elif on and c[t] < m[t] * (1 - buf):
                on = False
        w_sig[t] = min(max_gross, target_vol / vol[t]) if on and vol[t] > 0 else 0.0
    w = w_sig.shift(1).fillna(0)  # t 收盘信号，t+1 开盘执行（近似：用 open→close + close→open 拆分）
    # 近似：当日收益 = w_{t} * (close_t/close_{t-1}-1)，换仓成本按 |Δw| 计
    r = w * c.pct_change() - (w.diff().abs().fillna(0) * COST)
    r = r.loc[start:end]
    return r


def main():
    data_dir, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
    curves = pd.read_csv(out_dir / "explore_curves.csv", index_col=0, parse_dates=True)
    rets = curves.pct_change().dropna(how="all")
    lines = []

    lines.append("== 最大的 3 段回撤（峰值, 谷底, 恢复日, 回撤）==")
    for k in ["v2_default", "no_stop", "cand_v3", "BTC_sma100_trend", "BTC_hold"]:
        lines.append(f"{k}: {dd_episodes(curves[k])}")

    lines.append("\n== 前后半段 Sharpe / CAGR / MDD（2020-01~2022-09 | 2022-10~2025-06）==")
    for k in ["v2_default", "no_stop", "lb14_skip0", "lb28_skip0", "score_blend", "ts_basket_top10",
              "universe_rank40", "cand_v3", "BTC_sma100_trend", "BTC_hold"]:
        a = stats(rets[k].loc[:"2022-09-30"].dropna())
        b = stats(rets[k].loc["2022-10-01":].dropna())
        lines.append(f"{k:18s} H1 SR={a['Sharpe']:.2f} CAGR={a['CAGR']:.1%} MDD={a['MDD']:.1%} | "
                     f"H2 SR={b['Sharpe']:.2f} CAGR={b['CAGR']:.1%} MDD={b['MDD']:.1%}")

    lines.append("\n== BTC 趋势 + 波动率目标（同风险对照）==")
    start, end = rets.index[0], rets.index[-1]
    bt = {}
    for tv in (0.25, 0.30, 0.40):
        r = btc_trend_vt(data_dir, start, end, tv)
        bt[tv] = r
        s = stats(r)
        lines.append(f"BTC_trend_vt{tv:.2f}: " + " ".join(f"{k}={v:.3f}" for k, v in s.items()))

    lines.append("\n== 组合：50% BTC 趋势(vt0.30) + 50% 动量袖子（每日再平衡近似）==")
    for k in ["v2_default", "no_stop", "cand_v3"]:
        r = 0.5 * bt[0.30].reindex(rets.index).fillna(0) + 0.5 * rets[k].fillna(0)
        s = stats(r)
        lines.append(f"blend_BTCvt30+{k}: " + " ".join(f"{kk}={v:.3f}" for kk, v in s.items()))
    corr = pd.concat([bt[0.30].reindex(rets.index), rets["v2_default"]], axis=1).corr().iloc[0, 1]
    lines.append(f"BTC_trend_vt0.30 与 v2_default 日收益相关系数: {corr:.2f}")

    lines.append("\n== Sharpe 估计的抽样误差（年化 SR 的近似标准误，Lo 2002 iid 近似）==")
    T = len(rets) / 365
    for sr in (0.8, 1.2, 1.5):
        lines.append(f"SR={sr}: SE≈{np.sqrt((1 + 0.5 * sr ** 2) / T):.2f}（样本 {T:.1f} 年）")

    text = "\n".join(lines)
    print(text)
    (out_dir / "explore_extra.txt").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
