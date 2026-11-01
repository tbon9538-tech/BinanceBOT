"""研究引擎（spec §9.1、§9.3）：逐日循环，直接调用策略纯函数生成 W_t，按权重差额成交并计成本。

规则：
- t 日收盘计算目标，t+1 日开盘成交；止损同样 t 日收盘判断、t+1 开盘成交；
- 是否调仓由 bot.strategy.rebalance.should_skip 判断（与实盘订单计划共用）；
- 持仓列的交易段结束（之后再无日线、且早于面板末日）时，按最后收盘价 × (1 − delist_haircut) 退出；
- NAV 每天按收盘价估值（缺失时用最近的收盘价）。
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from bot.config import AppConfig, RebalanceConfig
from bot.risk.stops import StopState, update_stop
from bot.strategy.indicators import MarketPanel, column_symbol
from bot.strategy.momentum_v1 import MomentumV1
from bot.strategy.rebalance import SkipReason, should_skip
from bot.strategy.smoothing import Overrides, StopWindow

BACKTEST_MIN_NOTIONAL = 5.0  # 历史过滤器无法获得，统一近似为 5 USDT（spec §6.3）


@dataclass(frozen=True)
class EngineOptions:
    initial_capital: float = 1000.0
    fee: float = 0.001
    slippage: float = 0.0005
    delist_haircut: float = 0.5
    ideal: bool = False  # True：不设容忍带、最小下单额与 min_trade（E9 的"理想情况"）

    @property
    def cost(self) -> float:
        return self.fee + self.slippage

    @classmethod
    def from_config(cls, cfg: AppConfig, **kw: object) -> EngineOptions:
        b = cfg.backtest
        base = {
            "initial_capital": float(b.initial_capital),
            "fee": b.fee,
            "slippage": b.slippage,
            "delist_haircut": b.delist_haircut,
        }
        base.update(kw)
        return cls(**base)


@dataclass
class BacktestResult:
    daily: pd.DataFrame  # nav, navu, gross, gross_<袖子>, turnover, cost, regime, n_pos, weights(json)
    trades: pd.DataFrame  # date, col, symbol, sleeve, side, notional, cost
    skips: Counter[tuple[str, str]] = field(default_factory=Counter)  # (袖子, 原因) → 次数
    delist_exits: list[tuple[str, str]] = field(default_factory=list)
    stop_events: list[tuple[str, str]] = field(default_factory=list)
    regime: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def nav(self) -> pd.Series:
        return self.daily["navu"]


def run_backtest(
    cfg: AppConfig,
    panel: MarketPanel,
    start: date,
    end: date,
    *,
    opts: EngineOptions | None = None,
    strategy: MomentumV1 | None = None,
) -> BacktestResult:
    opts = opts or EngineOptions.from_config(cfg)
    strat = strategy or MomentumV1(cfg, panel)
    rcfg: RebalanceConfig = cfg.rebalance
    if opts.ideal:
        rcfg = rcfg.model_copy(update={"min_trade_usdt": 0})
    min_notional = 0.0 if opts.ideal else BACKTEST_MIN_NOTIONAL
    stop_cfg = cfg.risk.trailing_stop

    dates = panel.dates[(panel.dates >= pd.Timestamp(start)) & (panel.dates <= pd.Timestamp(end))]
    if len(dates) == 0:
        raise ValueError("回测区间内没有数据")
    open_, close_ff = panel.open, panel.close.ffill()
    last_valid = panel.last_valid_date()
    panel_end = panel.dates[-1]
    atr = panel.atr(stop_cfg.atr_period) if stop_cfg.enabled else None

    cash = opts.initial_capital
    units: dict[str, float] = {}
    pending: dict[str, float] | None = None
    stop_states: dict[str, StopState] = {}
    stop_windows: dict[str, StopWindow] = {}
    rows: list[dict[str, object]] = []
    trades: list[dict[str, object]] = []
    skips: Counter[tuple[str, str]] = Counter()
    delist_exits: list[tuple[str, str]] = []
    stop_events: list[tuple[str, str]] = []

    for t in dates:
        traded = cost_today = 0.0
        # 1) 下架退出：交易段在 t 之前结束、且不是面板末尾
        for col in list(units):
            lv = last_valid[col]
            if lv is not None and lv < t and lv < panel_end:
                px = float(close_ff.at[lv, col])
                value = units.pop(col) * px
                proceeds = value * (1 - opts.cost) * (1 - opts.delist_haircut)
                cash += proceeds
                cost_today += value - proceeds
                stop_states.pop(col, None)
                delist_exits.append((t.date().isoformat(), col))

        # 2) t 日开盘执行上一交易日收盘生成的目标
        if pending is not None:
            px_open = {c: float(open_.at[t, c]) for c in set(units) | set(pending)}
            ref = {
                c: (p if math.isfinite(p) else float(close_ff.at[t - pd.Timedelta(days=1), c]))
                for c, p in px_open.items()
            }
            equity = cash + sum(u * ref[c] for c, u in units.items())
            sells: list[tuple[str, float]] = []
            buys: list[tuple[str, float]] = []
            for col in sorted(set(units) | set(pending)):
                px = px_open[col]
                if not math.isfinite(px) or equity <= 0:
                    continue
                pos = units.get(col, 0.0) * px
                tw = pending.get(col, 0.0)
                cw = pos / equity
                delta = tw * equity - pos
                reason = should_skip(tw, cw, delta, pos, min_notional, rcfg, ignore_band=opts.ideal)
                if reason is not None:
                    if reason is not SkipReason.BELOW_MIN_TRADE or tw > 0 or pos > 0:
                        skips[(strat.sleeve_of(col), reason.value)] += 1
                    continue
                if tw <= 0:
                    sells.append((col, -pos))
                elif delta < 0:
                    sells.append((col, delta))
                else:
                    buys.append((col, delta))
            for col, d in sells:
                px = px_open[col]
                qty = min(units.get(col, 0.0), -d / px)
                if pending.get(col, 0.0) <= 0:
                    qty = units.get(col, 0.0)
                notional = qty * px
                cash += notional * (1 - opts.cost)
                cost_today += notional * opts.cost
                traded += notional
                units[col] = units.get(col, 0.0) - qty
                if units[col] * px < 1e-9:
                    units.pop(col)
                    stop_states.pop(col, None)
                trades.append(_trade(t, col, strat, "SELL", notional, notional * opts.cost))
            total_buy = sum(d for _, d in buys)
            scale = min(1.0, (cash / (1 + opts.cost)) / total_buy) if total_buy > 0 else 0.0
            for col, d in buys:
                notional = d * scale
                if notional <= 0:
                    continue
                px = px_open[col]
                cash -= notional * (1 + opts.cost)
                cost_today += notional * opts.cost
                traded += notional
                units[col] = units.get(col, 0.0) + notional / px
                trades.append(_trade(t, col, strat, "BUY", notional, notional * opts.cost))
            turnover = traded / equity if equity > 0 else 0.0
        else:
            turnover = 0.0

        # 3) t 日收盘估值
        values = {c: u * float(close_ff.at[t, c]) for c, u in units.items()}
        nav = cash + sum(values.values())

        # 4) 止损（t 日收盘判断，影响 t 日起计算的目标）
        if stop_cfg.enabled and atr is not None:
            for col in list(units):
                c_t = float(panel.close.at[t, col])
                st, window = update_stop(
                    stop_states.get(col),
                    t,
                    c_t,
                    float(atr.at[t, col]),
                    stop_cfg.atr_multiplier,
                    stop_cfg.cooldown_days,
                )
                if window is not None:
                    stop_windows[col] = window
                    stop_states.pop(col, None)
                    stop_events.append((t.date().isoformat(), col))
                elif st is not None:
                    stop_states[col] = st

        # 5) t 日收盘生成目标
        ov = Overrides(
            stops={c: w for c, w in stop_windows.items() if w.cooldown_until >= t - pd.Timedelta(days=60)}
        )
        tb = strat.target(t, ov)
        pending = tb.weights

        gross_by: dict[str, float] = {}
        for c, v in values.items():
            gross_by[strat.sleeve_of(c)] = gross_by.get(strat.sleeve_of(c), 0.0) + v / nav if nav > 0 else 0.0
        rows.append(
            {
                "date": t,
                "nav": nav,
                "gross": sum(values.values()) / nav if nav > 0 else 0.0,
                **{f"gross_{k}": v for k, v in gross_by.items()},
                "turnover": turnover,
                "cost": cost_today,
                "regime": tb.regime.value,
                "n_pos": len(units),
                "weights": json.dumps({k: round(v, 5) for k, v in sorted(tb.weights.items())}),
            }
        )

    daily = pd.DataFrame(rows).set_index("date")
    for name in cfg.enabled_sleeves():
        daily[f"gross_{name}"] = daily[f"gross_{name}"].fillna(0.0) if f"gross_{name}" in daily else 0.0
    daily["navu"] = daily["nav"] / opts.initial_capital
    trades_df = pd.DataFrame(trades, columns=["date", "col", "symbol", "sleeve", "side", "notional", "cost"])
    return BacktestResult(
        daily=daily,
        trades=trades_df,
        skips=skips,
        delist_exits=delist_exits,
        stop_events=stop_events,
        regime=strat.regime.loc[dates],
    )


def _trade(
    t: pd.Timestamp, col: str, strat: MomentumV1, side: str, notional: float, cost: float
) -> dict[str, object]:
    return {
        "date": t,
        "col": col,
        "symbol": column_symbol(col),
        "sleeve": strat.sleeve_of(col),
        "side": side,
        "notional": notional,
        "cost": cost,
    }


def rebalance_stats(result: BacktestResult) -> pd.DataFrame:
    """按袖子的实际调仓频率（spec §9.4）：每月平均交易笔数、平均每笔金额、跳过次数与原因。"""
    tr = result.trades
    months = max(len(result.daily) / 30.44, 1e-9)
    rows = []
    sleeves = sorted({s for s, _ in result.skips} | set(tr["sleeve"].unique() if len(tr) else []))
    for s in sleeves:
        t = tr[tr["sleeve"] == s] if len(tr) else tr
        skipped = {r: n for (sl, r), n in result.skips.items() if sl == s}
        rows.append(
            {
                "sleeve": s,
                "trades_per_month": len(t) / months,
                "avg_trade_usdt": float(t["notional"].mean()) if len(t) else float("nan"),
                "skipped_tolerance_band": skipped.get(SkipReason.TOLERANCE_BAND.value, 0),
                "skipped_below_min_trade": skipped.get(SkipReason.BELOW_MIN_TRADE.value, 0),
                "skipped_dust": skipped.get(SkipReason.DUST.value, 0),
            }
        )
    return pd.DataFrame(rows)
