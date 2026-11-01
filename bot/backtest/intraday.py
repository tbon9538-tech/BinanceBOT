"""短线策略模拟（研究 R11，预注册见 doc/research/log.md "R11 预注册"）。

5 分钟 K 线，只做多现货、不加杠杆，每个策略、每个币一个独立账户。
- 吃单（S2、S3）：第 i 根收盘算出信号，第 i+1 根开盘成交；每边手续费 taker_fee，成交价偏离开盘价 taker_slippage；
- 挂单（S1 网格）：限价成交，每边手续费 maker_fee、无滑点；价格必须穿过挂单价 maker_trade_through 才算成交；
  同一根 K 线里先处理此前买入的份额的卖单，再处理买单；本根卖出的档位不在本根重新买入（保守）。
- 网格锚点：持仓为空时，锚点在每根收盘时跟随当前价格（对预注册"全部卖完后锚点更新为当时价格"的解释，运行前写明）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Costs:
    taker_fee: float = 0.001
    taker_slippage: float = 0.0005
    maker_fee: float = 0.001
    maker_trade_through: float = 0.0005  # 成交保守性要求，不属于成本，压力测试时不放大

    def scaled(self, k: float) -> Costs:
        return Costs(
            self.taker_fee * k, self.taker_slippage * k, self.maker_fee * k, self.maker_trade_through
        )


@dataclass
class SimResult:
    equity: pd.Series  # 每根 K 线收盘时的权益（USDT）
    trades: int
    fees: float  # 累计手续费（USDT）
    slippage: float  # 累计滑点成本（USDT）
    bars_in_market: int
    capital: float

    @property
    def daily(self) -> pd.Series:
        """每个 UTC 日最后一根 K 线的权益，换算成单位净值（起点 1）。"""
        d = self.equity.groupby(self.equity.index.normalize()).last()
        return d / self.capital

    @property
    def gross_pnl(self) -> float:
        """不含手续费与滑点的盈亏。"""
        return float(self.equity.iloc[-1]) - self.capital + self.fees + self.slippage


def run_signals(
    bars: pd.DataFrame,
    enter: np.ndarray,
    exit_: np.ndarray,
    *,
    capital: float,
    costs: Costs,
    max_hold: int | None = None,
) -> SimResult:
    """全仓进出的吃单策略。enter / exit_ 为第 i 根收盘时的信号；持有满 max_hold 根也卖出。"""
    o = bars["open"].to_numpy(np.float64)
    c = bars["close"].to_numpy(np.float64)
    n = len(c)
    equity = np.empty(n)
    cash, qty, held = capital, 0.0, 0
    fees = slip = 0.0
    trades = in_mkt = 0
    pending = 0  # +1：本根开盘买入；−1：本根开盘卖出
    for i in range(n):
        if pending == 1 and qty == 0.0 and math.isfinite(o[i]):
            fee = cash * costs.taker_fee
            px = o[i] * (1 + costs.taker_slippage)
            qty = (cash - fee) / px
            slip += qty * (px - o[i])
            fees += fee
            cash, held, trades = 0.0, 0, trades + 1
        elif pending == -1 and qty > 0.0 and math.isfinite(o[i]):
            px = o[i] * (1 - costs.taker_slippage)
            proceeds = qty * px
            fee = proceeds * costs.taker_fee
            slip += qty * (o[i] - px)
            fees += fee
            cash, qty, trades = proceeds - fee, 0.0, trades + 1
        pending = 0
        equity[i] = cash + qty * c[i]
        if qty > 0.0:
            held += 1
            in_mkt += 1
            if exit_[i] or (max_hold is not None and held >= max_hold):
                pending = -1
        elif enter[i]:
            pending = 1
    return SimResult(pd.Series(equity, index=bars.index), trades, fees, slip, in_mkt, capital)


def dip_signals(bars: pd.DataFrame, z_entry: float, window: int) -> tuple[np.ndarray, np.ndarray]:
    """S2：收盘价相对前 window 根（不含当前根）均值的 z 分数 < −z_entry 时买入，回到 0 以上时卖出。"""
    c = bars["close"]
    prev = c.shift(1).rolling(window, min_periods=window)
    z = (c - prev.mean()) / prev.std(ddof=1)
    return (z < -z_entry).fillna(False).to_numpy(), (z > 0).fillna(False).to_numpy()


def breakout_signals(bars: pd.DataFrame, lookback_bars: int) -> tuple[np.ndarray, np.ndarray]:
    """S3：收盘价高于前 lookback 根最高价时买入；低于前 lookback/2 根最低价时卖出。"""
    hi = bars["high"].shift(1).rolling(lookback_bars, min_periods=lookback_bars).max()
    lo = bars["low"].shift(1).rolling(lookback_bars // 2, min_periods=lookback_bars // 2).min()
    c = bars["close"]
    return (c > hi).fillna(False).to_numpy(), (c < lo).fillna(False).to_numpy()


def run_grid(
    bars: pd.DataFrame,
    *,
    step: float,
    capital: float,
    costs: Costs,
    unit_frac: float = 0.1,
    max_units: int = 10,
) -> SimResult:
    """S1 网格：锚点下方每隔 step 一档挂买单（每份 = 锚点设定时现金的 unit_frac），每份在买入价上涨 step 处挂卖单。"""
    o = bars["open"].to_numpy(np.float64)
    h = bars["high"].to_numpy(np.float64)
    lo = bars["low"].to_numpy(np.float64)
    c = bars["close"].to_numpy(np.float64)
    n = len(c)
    equity = np.empty(n)
    tt = costs.maker_trade_through
    cash = capital
    anchor = o[0]
    unit_usdt = capital * unit_frac
    held: dict[int, tuple[float, float, int]] = {}  # 档位 → (买入价, 数量, 买入的 K 线序号)
    fees = 0.0
    trades = in_mkt = 0
    for i in range(n):
        freed: set[int] = set()
        for k, (buy_px, q, at) in list(held.items()):
            sell_px = buy_px * (1 + step)
            if at < i and h[i] >= sell_px * (1 + tt):
                proceeds = q * sell_px
                fee = proceeds * costs.maker_fee
                cash += proceeds - fee
                fees += fee
                trades += 1
                del held[k]
                freed.add(k)
        for k in range(1, max_units + 1):
            if k in held or k in freed:
                continue
            level = anchor * (1 - step) ** k
            if lo[i] <= level * (1 - tt) and cash >= unit_usdt > 0:
                fee = unit_usdt * costs.maker_fee
                held[k] = (level, (unit_usdt - fee) / level, i)
                cash -= unit_usdt
                fees += fee
                trades += 1
        equity[i] = cash + sum(q for _, q, _ in held.values()) * c[i]
        if held:
            in_mkt += 1
        else:
            anchor = c[i]
            unit_usdt = cash * unit_frac
    return SimResult(pd.Series(equity, index=bars.index), trades, fees, 0.0, in_mkt, capital)


def win_day_ratio(daily_navu: pd.Series) -> float:
    """盈利天数占比：日收益 > 0 的天数 / 全部天数（没有持仓、收益为 0 的日子不算盈利）。"""
    r = daily_navu.pct_change().dropna()
    return float((r > 0).mean()) if len(r) else float("nan")
