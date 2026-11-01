"""基准（spec §1.3）：BTC 持有、BTC 定投、BTC 趋势、成交额前 10 等权月度再平衡。"""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np
import pandas as pd

from bot.config import UniverseConfig
from bot.strategy.indicators import MarketPanel
from bot.strategy.sleeves import rank_universe


def btc_hold(panel: MarketPanel, dates: pd.DatetimeIndex, symbol: str = "BTCUSDT") -> pd.Series:
    c = panel.close[symbol].ffill().loc[dates]
    return c / c.iloc[0]


def btc_trend(
    panel: MarketPanel, regime: pd.DataFrame, dates: pd.DatetimeIndex, cost: float, symbol: str = "BTCUSDT"
) -> pd.Series:
    """同一大盘过滤：t 日收盘判断，t+1 日开盘满仓 BTC 或全部 USDT。"""
    o, c = panel.open[symbol], panel.close[symbol].ffill()
    cash, units, want = 1.0, 0.0, False
    nav = []
    for t in dates:
        px = o.get(t)
        if px is not None and np.isfinite(px):
            if want and units == 0:
                units, cash = cash * (1 - cost) / px, 0.0
            elif not want and units > 0:
                cash, units = units * px * (1 - cost), 0.0
        nav.append(cash + units * float(c.loc[t]))
        want = regime.at[t, "state"] == "RISK_ON"
    return pd.Series(nav, index=dates)


def ew_top10_monthly(
    panel: MarketPanel,
    dates: pd.DatetimeIndex,
    ucfg: UniverseConfig,
    cost: float,
    asset_ok: Callable[[str], bool],
) -> pd.Series:
    """每月第一天开盘按前一天成交额排名取前 10，等权持有（成本按约一半仓位换手近似）。"""
    top = ucfg.model_copy(update={"universe_size": 10, "min_median_quote_volume": 0.0})
    close_ff = panel.close.ffill()
    cash, units = 1.0, {}
    nav = []
    for i, t in enumerate(dates):
        if i == 0 or t.month != dates[i - 1].month:
            prev = t - pd.Timedelta(days=1)
            members = rank_universe(panel, prev, top, asset_ok=asset_ok) if prev in panel.dates else []
            px = {c: panel.open.at[t, c] for c in members}
            members = [c for c in members if np.isfinite(px[c])]
            value = cash + sum(u * float(close_ff.at[prev, c]) for c, u in units.items()) if i else cash
            if members:
                value *= 1 - cost * 0.5
                units = {c: value / len(members) / px[c] for c in members}
                cash = 0.0
        nav.append(cash + sum(u * float(close_ff.at[t, c]) for c, u in units.items()))
    return pd.Series(nav, index=dates)


def dca(nav: pd.Series) -> dict[str, float]:
    """每月第一天投入 1 单位，返回终值 / 总投入与资金加权年化收益（IRR）。"""
    r = nav.pct_change().fillna(0.0)
    value, invested = 0.0, 0.0
    flows: list[tuple[pd.Timestamp, float]] = []
    prev_month = None
    for t, x in r.items():
        ts = pd.Timestamp(str(t))
        value *= 1 + x
        if ts.month != prev_month:
            value += 1.0
            invested += 1.0
            flows.append((ts, 1.0))
            prev_month = ts.month
    end = pd.Timestamp(str(nav.index[-1]))

    def fv(rate: float) -> float:
        return sum(a * (1 + rate) ** ((end - d).days / 365) for d, a in flows) - value

    lo, hi = -0.99, 10.0
    irr = float("nan")
    if fv(lo) * fv(hi) < 0:
        for _ in range(200):
            mid = (lo + hi) / 2
            if fv(lo) * fv(mid) <= 0:
                hi = mid
            else:
                lo = mid
        irr = (lo + hi) / 2
    return {"multiple": value / invested if invested else math.nan, "irr": irr}
