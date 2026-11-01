"""资金费率套利袖子的研究回测（研究 R10，预注册见 doc/research/log.md "R10 预注册"）。只做研究，不涉及交易代码。

每个标的一个独立子账户（初始资金均分）：
- d 日是否持有对冲（现货多 + 永续空，名义相等），由 d−1 日收盘时过去 trailing_days 天的平均每日资金费 × 365
  是否高于门槛决定；门槛为 None 时一直持有。只用 d−1 日及以前的数据，没有前视；
- 持有时现货名义 n = c / (1 + 1/L)，其余 n/L 为永续保证金；当日盈亏 n ×（当日资金费 + 现货日收益 − 永续日收益）；
- 开仓、平仓时两条腿都计成本 n × (spot_cost + perp_cost)；对冲比例的日常再平衡忽略（预注册的简化）；
- 永续当日最高价相对前一日收盘上涨超过 liquidation_move 记一次爆仓：损失全部保证金 n/L，现货腿当日照常计盈亏，然后平仓；
- 任一价格缺失的日子盈亏记 0。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date

import pandas as pd


@dataclass(frozen=True)
class CarryParams:
    leverage: float = 2.0
    hurdle_annual: float | None = 0.05  # None：一直持有
    trailing_days: int = 7
    spot_cost: float = 0.0015  # 现货手续费 0.1% + 滑点 0.05%
    perp_cost: float = 0.001  # 永续手续费 0.05% + 滑点 0.05%
    liquidation_move: float = 0.45

    def with_costs_scaled(self, k: float) -> CarryParams:
        return replace(self, spot_cost=self.spot_cost * k, perp_cost=self.perp_cost * k)


@dataclass
class CarryResult:
    nav: pd.Series  # 袖子净值，起点 1
    holding: pd.DataFrame  # 每个标的每天是否持有对冲
    deployed: pd.Series  # 现货名义 / 当天开始时的袖子净值
    cost: pd.Series  # 每天的开平仓成本（占起始资金的比例）
    funding: pd.Series  # 每天收到的资金费（占起始资金的比例）
    events: list[tuple[str, str, str]] = field(default_factory=list)  # (日期, 标的, 事件)


def _num(s: pd.Series | None, d: pd.Timestamp) -> float:
    if s is None or d not in s.index:
        return math.nan
    v = s.at[d]
    return float(v) if pd.notna(v) else math.nan


def run_carry(
    spot_close: Mapping[str, pd.Series],
    perp: Mapping[str, pd.DataFrame],
    funding_daily: Mapping[str, pd.Series],
    start: date,
    end: date,
    params: CarryParams,
) -> CarryResult:
    dates = pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="D")
    symbols = list(spot_close)
    k = 1 + 1 / params.leverage
    one = pd.Timedelta(days=1)
    cap = dict.fromkeys(symbols, 1.0 / len(symbols))
    held = dict.fromkeys(symbols, False)
    trailing = {
        s: funding_daily[s].rolling(params.trailing_days, min_periods=params.trailing_days).mean() * 365
        for s in symbols
    }
    rows: list[dict[str, float]] = []
    holds: list[dict[str, bool]] = []
    events: list[tuple[str, str, str]] = []
    for d in dates:
        nav_start = sum(cap.values())
        cost_today = funding_today = notional_today = 0.0
        today: dict[str, bool] = {}
        for s in symbols:
            sp, pp = spot_close[s], perp[s]
            p_close, p_high = pp.get("close"), pp.get("high")
            ready = not (math.isnan(_num(sp, d - one)) or math.isnan(_num(p_close, d - one)))
            if params.hurdle_annual is None:
                want = ready
            else:
                fa = _num(trailing[s], d - one)
                want = ready and not math.isnan(fa) and fa > params.hurdle_annual
            if want != held[s]:  # 开仓或平仓：两条腿的成本
                c = cap[s] / k * (params.spot_cost + params.perp_cost)
                cap[s] -= c
                cost_today += c
            held[s] = want
            if want:
                n = cap[s] / k
                notional_today += n
                s0, s1 = _num(sp, d - one), _num(sp, d)
                p0, p1, hi = _num(p_close, d - one), _num(p_close, d), _num(p_high, d)
                if not any(math.isnan(x) for x in (s0, s1, p0, p1, hi)):
                    if hi / p0 - 1 > params.liquidation_move:
                        cap[s] += n * (s1 / s0 - 1) - n / params.leverage
                        held[s] = False
                        events.append((d.date().isoformat(), s, "LIQUIDATION"))
                    else:
                        f = _num(funding_daily[s], d)
                        f = 0.0 if math.isnan(f) else f
                        cap[s] += n * (f + s1 / s0 - p1 / p0)
                        funding_today += n * f
            today[s] = held[s]
        nav = sum(cap.values())
        rows.append(
            {"nav": nav, "cost": cost_today, "funding": funding_today, "deployed": notional_today / nav_start}
        )
        holds.append(today)
    df = pd.DataFrame(rows, index=dates)
    return CarryResult(
        nav=df["nav"],
        holding=pd.DataFrame(holds, index=dates),
        deployed=df["deployed"],
        cost=df["cost"],
        funding=df["funding"],
        events=events,
    )


def combine(trend_nav: pd.Series, carry_nav: pd.Series, w: float) -> pd.Series:
    """组合净值：每天按 (1 − w)、w 再平衡（忽略再平衡成本，预注册的简化）。只取两者都有的日期，起点 1。"""
    idx = trend_nav.index.intersection(carry_nav.index)
    r = (1 - w) * trend_nav.loc[idx].pct_change() + w * carry_nav.loc[idx].pct_change()
    return (1 + r.fillna(0.0)).cumprod()
