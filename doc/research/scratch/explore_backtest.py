"""一次性探索脚本：spec v2 策略的简化日频回测 + 若干变体，用于判断可行性与参数敏感性。

重要说明（写进报告）：
- 这是初步验证，不是正式回测：未模拟盘口/部分成交，下架币按最后收盘价退出（偏乐观），
  无融资利率/理财收益，参数网格为事后检视，存在多重检验问题。
- 数据只到 2025-06-30（2025-07-01 起为保留集，研究期间不得使用）。

用法（项目根目录）：
    py -3.13 -I doc/research/scratch/explore_backtest.py doc/research/scratch/data doc/research/scratch/out
"""
import json
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
import pandas as pd

HOLDOUT_START = "2025-07-01"

STABLE_FIAT = {
    "USDC", "BUSD", "TUSD", "USDP", "PAX", "DAI", "FDUSD", "USDS", "USDSB", "SUSD", "UST", "USTC",
    "EUR", "GBP", "AUD", "TRY", "BRL", "RUB", "BIDR", "IDRT", "UAH", "NGN", "ZAR", "PLN", "RON", "ARS",
    "JPY", "MXN", "COP", "CZK", "AEUR", "EURI", "USD1", "XUSD", "BFUSD", "RLUSD", "USDE", "PYUSD",
    "PAXG", "XAUT", "WBTC", "WBETH", "BETH", "BKRW", "VAI", "USDJ", "FRAX", "LUSD", "GUSD",
}
LEVERAGED = re.compile(r"^[A-Z0-9]{2,}(UP|DOWN|BULL|BEAR)$")
LEVERAGED_FALSE_POSITIVE = {"JUP", "SUPER", "PUP", "SYRUP", "CUP"}


@dataclass(frozen=True)
class P:
    name: str = "v2_default"
    start: str = "2020-01-01"
    end: str = "2025-06-30"
    universe_size: int = 40
    min_median_qv: float = 20e6
    min_listing_days: int = 90
    regime: str = "sma"           # sma | none
    regime_sma: int = 100
    regime_buffer: float = 0.02
    lookback: int = 28
    skip: int = 5
    score: str = "ret_over_vol"   # ret_over_vol | ret
    max_lb_ret: float = 1.5       # None = 不限
    coin_sma: int = 50            # 0 = 不过滤
    top_n: int = 5
    weighting: str = "equal"      # equal | inverse_vol
    max_w: float = 0.25
    max_gross: float = 0.95
    vol_target: bool = True
    target_vol: float = 0.40
    vol_lb: int = 30
    period: int = 7               # 每个 tranche 的调仓周期（天）
    tranches: int = 7
    offset: int = 0               # 调仓日偏移（tranches=1 时用于测试不同星期几）
    threshold: float = 0.02
    stop: bool = True
    atr_n: int = 14
    atr_mult: float = 2.5
    cooldown: int = 7
    cost: float = 0.0015          # 单边 fee 0.001 + slippage 0.0005
    survivors_only: bool = False  # True = 只用当前仍在交易的币（制造幸存者偏差，用于对比）
    universe_mode: str = "abs"    # abs = 绝对成交额门槛 | rank = 只按成交额排名
    ramp_on: bool = False         # True = Risk-On 切换时不全量重算，由各 tranche 逐日建仓
    delist_haircut: float = 0.0   # 数据中断（下架）时按最后收盘价再打折退出
    rank_floor: float = 0.0       # rank 模式下的成交额绝对下限
    exclude: tuple = ()           # 从标的池排除的交易对（如核心袖子资产）


def load_panel(data_dir):
    meta = pd.read_csv(data_dir / "symbols_usdt.csv")
    status = dict(zip(meta.symbol, meta.status_now))
    frames = {}
    for f in sorted((data_dir / "klines_1d").glob("*.csv")):
        sym = f.stem
        base = sym[:-4]
        if base in STABLE_FIAT or (LEVERAGED.match(base) and base not in LEVERAGED_FALSE_POSITIVE):
            continue
        df = pd.read_csv(f, index_col="date")
        df = df[df.index < HOLDOUT_START]
        if len(df) < 30:
            continue
        frames[sym] = df
    idx = pd.Index(sorted(set().union(*[d.index for d in frames.values()])), name="date")
    panel = {k: pd.DataFrame({s: d[k] for s, d in frames.items()}).reindex(idx).astype(float)
             for k in ["open", "high", "low", "close", "quote_volume"]}
    panel["status"] = status
    return panel


def segment_age(close):
    """上市天数；数据中断 > 7 天视为重新上市（如 LUNA 旧币下架后同名新币）。"""
    valid = close.notna()
    age = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    for s in close.columns:
        v = valid[s].values
        out = np.full(len(v), np.nan)
        cnt, gap = 0, 999
        for i, ok in enumerate(v):
            if ok:
                if gap > 7:
                    cnt = 0
                cnt += 1
                gap = 0
                out[i] = cnt
            else:
                gap += 1
        age[s] = out
    return age


class Pre:
    """与参数相关的预计算（缓存，避免每个变体重复计算）。"""

    def __init__(self, panel):
        self.panel = panel
        self.C, self.O = panel["close"], panel["open"]
        self.H, self.L = panel["high"], panel["low"]
        self.ret = self.C.pct_change(fill_method=None)
        self.age = segment_age(self.C)
        self.medqv = panel["quote_volume"].rolling(30, min_periods=25).median()
        prev = self.C.shift(1)
        tr = pd.concat([(self.H - self.L).stack(), (self.H - prev).abs().stack(), (self.L - prev).abs().stack()],
                       axis=1).max(axis=1).unstack()
        self._tr = tr.reindex_like(self.C)
        self._cache = {}
        self.alive = pd.Series({s: panel["status"].get(s) == "TRADING" for s in self.C.columns})

    def get(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def sma(self, n):
        return self.get(("sma", n), lambda: self.C.rolling(n, min_periods=n).mean())

    def mom(self, lb, skip):
        return self.get(("mom", lb, skip), lambda: self.C.shift(skip) / self.C.shift(lb) - 1)

    def vol(self, lb):
        return self.get(("vol", lb), lambda: self.ret.rolling(lb, min_periods=int(lb * 0.9)).std())

    def atr(self, n):
        return self.get(("atr", n), lambda: self._tr.ewm(alpha=1 / n, min_periods=n, adjust=False).mean())


def universe_at(pre, p, t):
    medqv = pre.medqv.loc[t]
    ok = pre.C.loc[t].notna() & (pre.age.loc[t] >= p.min_listing_days) & medqv.notna()
    if p.universe_mode == "abs":
        ok &= medqv >= p.min_median_qv
    else:
        ok &= medqv >= p.rank_floor
    if p.exclude:
        ok &= ~medqv.index.isin(list(p.exclude))
    if p.survivors_only:
        ok &= pre.alive
    return medqv[ok].sort_values(ascending=False).index[: p.universe_size]


def target_weights(pre, p, t, uni, blocked):
    if len(uni) == 0:
        return {}
    mom = pre.mom(p.lookback, p.skip).loc[t, uni]
    vol = pre.vol(p.lookback).loc[t, uni]
    if p.score == "ret_over_vol":
        score = mom / vol
    elif p.score == "blend":  # 多周期：14/28/56 天 ret/vol 的平均
        score = sum(pre.mom(L, 0).loc[t, uni] / pre.vol(L).loc[t, uni] for L in (14, 28, 56)) / 3
    else:
        score = mom
    ok = score.notna() & (score > 0)
    if p.coin_sma:
        ok &= pre.C.loc[t, uni] > pre.sma(p.coin_sma).loc[t, uni]
    if p.max_lb_ret is not None:
        lbr = pre.mom(p.lookback, 0).loc[t, uni]
        ok &= lbr <= p.max_lb_ret
    ok &= ~score.index.isin(list(blocked))
    sel = score[ok].sort_values(ascending=False).index[: p.top_n]
    if len(sel) == 0:
        return {}
    raw = pd.Series(1.0, index=sel) if p.weighting == "equal" else 1 / vol[sel]
    w = cap_weights(raw / raw.sum(), p.max_w)
    gross = p.max_gross
    if p.vol_target:
        loc = pre.ret.index.get_loc(t)
        r = pre.ret.iloc[loc - p.vol_lb + 1: loc + 1][w.index].fillna(0.0)
        pv = float((r @ w).std() * np.sqrt(365))
        if pv > 0:
            gross = min(p.max_gross, p.target_vol / pv)
    return (w * gross).to_dict()


def cap_weights(w, cap):
    w = w.copy()
    for _ in range(20):
        over = w > cap + 1e-12
        if not over.any():
            break
        excess = (w[over] - cap).sum()
        w[over] = cap
        free = ~over & (w < cap)
        if not free.any():
            break  # 剩余留 USDT
        w[free] += excess * w[free] / w[free].sum()
    return w


def run(pre, p):
    dates = pre.C.loc[p.start:p.end].index
    all_dates = pre.C.index
    C, O = pre.C, pre.O
    btc = C["BTCUSDT"]
    bsma = btc.rolling(p.regime_sma, min_periods=p.regime_sma).mean()
    atr = pre.atr(p.atr_n)

    cash, units = 1.0, {}
    tranche_t = [dict() for _ in range(p.tranches)]
    risk_on = False
    highest, cooldown_until = {}, {}
    equity, gross_hist, turnover = [], [], 0.0
    pending = None  # 上一日收盘生成、今日开盘执行的目标
    delist_exits = 0
    last_px = {}

    for i, t in enumerate(dates):
        # ---- t 日开盘：执行上一日收盘计算的目标 ----
        if pending is not None:
            px = {s: (O.at[t, s] if s in O.columns and not np.isnan(O.at[t, s]) else last_px.get(s)) for s in
                  set(units) | set(pending)}
            eq = cash + sum(u * px[s] for s, u in units.items())
            trades = {}
            for s in set(units) | set(pending):
                if px.get(s) is None or np.isnan(px[s]):
                    continue
                cur = units.get(s, 0.0) * px[s] / eq
                tgt = pending.get(s, 0.0)
                if tgt == 0 and cur > 0:
                    trades[s] = -cur
                elif tgt > 0 and abs(tgt - cur) >= p.threshold:
                    trades[s] = tgt - cur
            for s, dw in sorted(trades.items(), key=lambda kv: kv[1]):  # 先卖后买
                val = dw * eq
                if val > 0:
                    val = min(val, cash / (1 + p.cost))
                    if val <= 0:
                        continue
                cash -= val + abs(val) * p.cost
                turnover += abs(val) / eq
                units[s] = units.get(s, 0.0) + val / px[s]
                if units[s] * px[s] < 1e-9:
                    units.pop(s)
                    highest.pop(s, None)
            pending = None

        # ---- t 日收盘：估值 ----
        for s in list(units):
            c = C.at[t, s]
            if not np.isnan(c):
                last_px[s] = c
            elif s not in last_px:
                last_px[s] = O.at[t, s]
        eq = cash + sum(u * last_px[s] for s, u in units.items())
        equity.append(eq)
        gross_hist.append(1 - cash / eq)

        # 已下架（之后再无数据）的持仓：按最后收盘价强制退出（偏乐观）
        loc = all_dates.get_loc(t)
        for s in list(units):
            if C[s].iloc[loc + 1:].notna().sum() == 0 and t < dates[-1]:
                cash += units.pop(s) * last_px[s] * (1 - p.cost) * (1 - p.delist_haircut)
                delist_exits += 1
                highest.pop(s, None)

        # ---- t 日收盘：风控 + 信号 ----
        if p.regime == "none":
            new_on = True
        else:
            b, m = btc.at[t], bsma.at[t]
            new_on = risk_on
            if not np.isnan(m):
                if not risk_on and b > m * (1 + p.regime_buffer):
                    new_on = True
                elif risk_on and b < m * (1 - p.regime_buffer):
                    new_on = False
        switched_on, switched_off = new_on and not risk_on, risk_on and not new_on
        risk_on = new_on

        blocked = {s for s, d in cooldown_until.items() if t <= d}
        if p.stop:
            for s in list(units):
                c = C.at[t, s]
                if np.isnan(c):
                    continue
                highest[s] = max(highest.get(s, c), c)
                a = atr.at[t, s]
                if not np.isnan(a) and c < highest[s] - p.atr_mult * a:
                    cooldown_until[s] = all_dates[min(loc + p.cooldown, len(all_dates) - 1)]
                    blocked.add(s)
                    for tr in tranche_t:
                        tr.pop(s, None)
        else:
            for s in list(units):
                c = C.at[t, s]
                if not np.isnan(c):
                    highest[s] = max(highest.get(s, c), c)

        day_index = (pd.Timestamp(t) - pd.Timestamp("1970-01-01")).days
        due = [k for k in range(p.tranches) if (day_index - p.offset) % p.period == (k * p.period) // p.tranches]
        if switched_off or not risk_on:
            if switched_off:
                due = range(p.tranches)
            for k in due:
                tranche_t[k] = {}
        else:
            if switched_on and not p.ramp_on:
                due = range(p.tranches)
            if due:
                uni = universe_at(pre, p, t)
                tw = target_weights(pre, p, t, uni, blocked)
                for k in due:
                    tranche_t[k] = dict(tw)
        agg = {}
        for tr in tranche_t:
            for s, w in tr.items():
                if s not in blocked:
                    agg[s] = agg.get(s, 0.0) + w / p.tranches
        pending = agg

    eq = pd.Series(equity, index=pd.to_datetime(dates))
    run.delist_exits = delist_exits
    return eq, pd.Series(gross_hist, index=eq.index), turnover / (len(dates) / 365)


def stats(eq, gross=None, turnover=None):
    r = eq.pct_change().dropna()
    yrs = (eq.index[-1] - eq.index[0]).days / 365
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1
    vol = r.std() * np.sqrt(365)
    sharpe = r.mean() / r.std() * np.sqrt(365) if r.std() > 0 else np.nan
    dd = eq / eq.cummax() - 1
    mdd = dd.min()
    out = dict(CAGR=cagr, Vol=vol, Sharpe=sharpe, MDD=mdd, Calmar=cagr / -mdd if mdd < 0 else np.nan,
               Days_le_m10=int((r <= -0.10).sum()), Days_le_m15=int((r <= -0.15).sum()))
    if gross is not None:
        out["AvgGross"] = gross.mean()
        out["CashDays"] = (gross < 0.05).mean()
    if turnover is not None:
        out["Turnover/yr"] = turnover
    yearly = eq.resample("YE").last()
    yearly = pd.concat([eq.iloc[:1], yearly]).pct_change().dropna()
    for y, v in yearly.items():
        out[str(y.year)] = v
    return out


def bench_btc(pre, p):
    c = pre.C["BTCUSDT"].loc[p.start:p.end]
    return pd.Series(c.values / c.iloc[0], index=pd.to_datetime(c.index))


def bench_btc_trend(pre, p, sma=100, buf=0.02):
    """BTC 单币趋势：BTC > SMA(+缓冲) 时满仓 BTC，否则 USDT；t 收盘判断，t+1 开盘执行。"""
    C, O = pre.C["BTCUSDT"], pre.O["BTCUSDT"]
    m = C.rolling(sma, min_periods=sma).mean()
    dates = C.loc[p.start:p.end].index
    on, want, cash, u, eq = False, False, 1.0, 0.0, []
    for t in dates:
        if want != (u > 0):
            if want:
                u = cash * (1 - p.cost) / O[t]
                cash = 0.0
            else:
                cash = u * O[t] * (1 - p.cost)
                u = 0.0
        eq.append(cash + u * C[t])
        if not on and C[t] > m[t] * (1 + buf):
            on = True
        elif on and C[t] < m[t] * (1 - buf):
            on = False
        want = on
    return pd.Series(eq, index=pd.to_datetime(dates))


def bench_ew_top10(pre, p):
    """等权持有成交额前 10（每月初再平衡，用上月末数据选币）。"""
    dates = pre.C.loc[p.start:p.end].index
    cash, units, eq, last = 1.0, {}, [], {}
    q = replace(p, universe_size=10, universe_mode="rank", survivors_only=False)
    for i, t in enumerate(dates):
        if i == 0 or t[:7] != dates[i - 1][:7]:
            prev = dates[i - 1] if i else t
            uni = universe_at(pre, q, prev)
            px = {s: pre.O.at[t, s] for s in set(units) | set(uni)}
            px = {s: v if not np.isnan(v) else last.get(s, np.nan) for s, v in px.items()}
            e = cash + sum(units[s] * px[s] for s in units)
            cash = e * (1 - p.cost * 0.5)  # 简化：约一半仓位换手
            units = {}
            live = [s for s in uni if not np.isnan(px[s])]
            for s in live:
                units[s] = cash / len(live) / px[s]
            cash = 0.0 if live else cash
        for s in units:
            c = pre.C.at[t, s]
            if not np.isnan(c):
                last[s] = c
        eq.append(cash + sum(units[s] * last.get(s, 0) for s in units))
    return pd.Series(eq, index=pd.to_datetime(dates))


def dca_terminal(eq, monthly=1.0):
    """按月初等额投入，比较终值 / 总投入（同一现金流下比较策略与 BTC 定投）。"""
    r = eq.pct_change().fillna(0)
    w, inv = 0.0, 0.0
    prev_m = None
    for t, x in r.items():
        w *= 1 + x
        if t.month != prev_m:
            w += monthly
            inv += monthly
            prev_m = t.month
    return w / inv


def main():
    data_dir, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)
    panel = load_panel(data_dir)
    pre = Pre(panel)
    base = P()

    # 标的池规模随时间变化（绝对门槛 20M 的影响）
    sizes = {}
    for t in pre.C.loc["2018-06-01":base.end].index[::30]:
        sizes[t] = len(universe_at(pre, base, t))
    pd.Series(sizes).to_csv(out_dir / "universe_size_abs20m.csv")

    variants = [
        base,
        replace(base, name="no_regime", regime="none"),
        replace(base, name="no_voltarget", vol_target=False),
        replace(base, name="no_stop", stop=False),
        replace(base, name="no_stop_no_vt", stop=False, vol_target=False),
        replace(base, name="regime_sma50", regime_sma=50),
        replace(base, name="regime_sma150", regime_sma=150),
        replace(base, name="regime_sma200", regime_sma=200),
        replace(base, name="lb14_skip0", lookback=14, skip=0),
        replace(base, name="lb28_skip0", skip=0),
        replace(base, name="lb56_skip5", lookback=56),
        replace(base, name="lb90_skip5", lookback=90),
        replace(base, name="top3", top_n=3, max_w=0.34),
        replace(base, name="top10", top_n=10),
        replace(base, name="no_maxret_cap", max_lb_ret=None),
        replace(base, name="no_coin_sma", coin_sma=0),
        replace(base, name="inverse_vol", weighting="inverse_vol"),
        replace(base, name="tvol_0.25", target_vol=0.25),
        replace(base, name="tvol_0.60", target_vol=0.60),
        replace(base, name="tranches1_weekly", tranches=1),
        replace(base, name="universe_rank40", universe_mode="rank"),
        replace(base, name="universe_rank20", universe_mode="rank", universe_size=20),
        replace(base, name="survivors_only", survivors_only=True),
        replace(base, name="cost_x2", cost=0.003),
        replace(base, name="atr_3.0", atr_mult=3.0),
        replace(base, name="lb28_skip1", skip=1),
        replace(base, name="buffer_0", regime_buffer=0.0),
        replace(base, name="buffer_0.05", regime_buffer=0.05),
        replace(base, name="ramp_on", ramp_on=True),
        replace(base, name="score_ret_only", score="ret"),
        replace(base, name="score_blend", score="blend"),
        replace(base, name="ts_basket_top10", universe_size=10, top_n=10, max_w=0.25, universe_mode="rank"),
        replace(base, name="delist_haircut_0.5", delist_haircut=0.5),
        # 组合候选（研究文档"推荐第一版"的初步形态，仅作方向性参考）
        replace(base, name="cand_v3", universe_mode="rank", universe_size=30, skip=0, max_lb_ret=None,
                target_vol=0.30, stop=False),
        replace(base, name="cand_v3_stop", universe_mode="rank", universe_size=30, skip=0, max_lb_ret=None,
                target_vol=0.30),
        replace(base, name="cand_v3_tv25", universe_mode="rank", universe_size=30, skip=0, max_lb_ret=None,
                target_vol=0.25, stop=False),
    ]
    rows, curves = [], {}
    for v in variants:
        eq, g, to = run(pre, v)
        s = stats(eq, g, to)
        s["DCA_multiple"] = dca_terminal(eq)
        s["delist_exits"] = run.delist_exits
        rows.append({"variant": v.name, **s})
        curves[v.name] = eq
        print(v.name, {k: round(x, 3) for k, x in s.items() if isinstance(x, float)}, flush=True)

    for name, eq in [("BTC_hold", bench_btc(pre, base)), ("BTC_sma100_trend", bench_btc_trend(pre, base)),
                     ("EW_top10_monthly", bench_ew_top10(pre, base))]:
        s = stats(eq)
        s["DCA_multiple"] = dca_terminal(eq)
        rows.append({"variant": name, **s})
        curves[name] = eq
        print(name, {k: round(x, 3) for k, x in s.items() if isinstance(x, float)}, flush=True)

    # 调仓日敏感性：tranches=1 时周一~周日各跑一次
    for off in range(7):
        v = replace(base, name=f"weekly_offset{off}", tranches=1, offset=off)
        eq, g, to = run(pre, v)
        rows.append({"variant": v.name, **stats(eq, g, to)})

    # 2018-06 ~ 2019-12：覆盖 2018 熊市（绝对门槛下标的池过小，改用排名模式）
    early = replace(base, name="early_2018_rank40", start="2018-06-01", end="2019-12-31", universe_mode="rank")
    eq, g, to = run(pre, early)
    rows.append({"variant": early.name, **stats(eq, g, to)})
    e2 = replace(early, name="early_BTC_hold")
    rows.append({"variant": e2.name, **stats(bench_btc(pre, e2))})
    rows.append({"variant": "early_BTC_sma100", **stats(bench_btc_trend(pre, e2))})

    df = pd.DataFrame(rows)
    df.to_csv(out_dir / "explore_results.csv", index=False, float_format="%.4f")
    pd.DataFrame(curves).to_csv(out_dir / "explore_curves.csv", float_format="%.6f")
    print(df.to_string(float_format=lambda x: f"{x:.3f}"))


if __name__ == "__main__":
    main()
