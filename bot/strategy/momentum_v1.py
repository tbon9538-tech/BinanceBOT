"""策略 mom1：BTC 趋势核心 + 山寨动量卫星（spec §4）。

同一个类同时用于研究引擎（逐日调用 target）与实盘（只调用最新一天）。
所有计算只依赖面板中 ≤ t 的数据；原始目标 D_d 按 (袖子, 日期, 冷却集合) 缓存，因此可以随时重算。
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass

import pandas as pd

from bot.config import AppConfig, TrendSleeveConfig, XsMomentumSleeveConfig
from bot.domain.types import Regime
from bot.strategy.ctrend import CtrendModel
from bot.strategy.indicators import MarketPanel
from bot.strategy.portfolio import combine_sleeves
from bot.strategy.regime import regime_series, regime_with_exposure
from bot.strategy.sleeves import core_target, rank_universe, satellite_target
from bot.strategy.smoothing import Overrides, smoothed_target


@dataclass(frozen=True)
class TargetBreakdown:
    as_of: pd.Timestamp
    regime: Regime
    switched_on: bool
    switched_off: bool
    raw: dict[str, dict[str, float]]  # 袖子 → D_t
    smoothed: dict[str, dict[str, float]]  # 袖子 → S_t
    weights: dict[str, float]  # W_t：面板列 → 占策略资金的权重
    universe: list[str]  # 卫星袖子当天的标的池
    exposure: float = 1.0  # 大盘仓位乘数：sma / tsmom 为 0 或 1，ensemble 为处于 Risk-On 的均线比例


class MomentumV1:
    def __init__(
        self,
        cfg: AppConfig,
        panel: MarketPanel,
        *,
        asset_ok: Callable[[str], bool] = lambda col: True,
        exclude_symbols: Collection[str] = (),
    ) -> None:
        """exclude_symbols：在流动性排名阶段就排除的交易对（实盘传入 risk.force_exit_assets，spec §2.4）。"""
        self.cfg = cfg
        self.strategy_id = cfg.strategy_id
        self.panel = panel
        self.asset_ok = asset_ok
        self._exclude = frozenset(exclude_symbols)
        rc = cfg.regime
        if rc.symbol not in panel.columns:
            raise ValueError(f"面板中没有大盘过滤标的 {rc.symbol}")
        self.regime = regime_with_exposure(
            panel.close[rc.symbol],
            rc.sma_period,
            rc.buffer,
            mode=rc.mode,
            tsmom_lookback=rc.tsmom_lookback,
            ensemble_periods=tuple(rc.ensemble_periods),
        )
        self._switch_on_dates = pd.DatetimeIndex(self.regime.index[self.regime["switched_on"].to_numpy()])
        self._sleeves = cfg.enabled_sleeves()
        self._raw_cache: dict[tuple[str, pd.Timestamp, frozenset[str]], dict[str, float]] = {}
        self._universe_cache: dict[pd.Timestamp, list[str]] = {}
        # 单资产趋势过滤（R4）：袖子 → 资产 → 每天是否处于自身的 Risk-On
        self._asset_on: dict[str, dict[str, pd.Series]] = {
            name: {
                sym: regime_series(panel.close[sym], s.asset_sma_period, s.asset_buffer)["state"]
                == Regime.RISK_ON.value
                for sym in s.symbols
                if sym in panel.columns
            }
            for name, s in self._sleeves.items()
            if isinstance(s, TrendSleeveConfig) and s.asset_filter == "own_sma"
        }
        self._ctrend: dict[str, CtrendModel] = {
            name: CtrendModel(panel, s, lambda d, ex=tuple(s.exclude_symbols): self.universe(d, ex))
            for name, s in self._sleeves.items()
            if isinstance(s, XsMomentumSleeveConfig) and s.score == "ctrend"
        }

    def exposure(self, d: pd.Timestamp) -> float:
        return float(self.regime.at[d, "exposure"])

    def risk_on(self, d: pd.Timestamp) -> bool:
        return bool(self.regime.at[d, "state"] == Regime.RISK_ON.value)

    def last_switch_on(self, t: pd.Timestamp) -> pd.Timestamp | None:
        prior = self._switch_on_dates[self._switch_on_dates <= t]
        return prior[-1] if len(prior) else None

    def universe(self, d: pd.Timestamp, exclude_symbols: tuple[str, ...]) -> list[str]:
        if d not in self._universe_cache:
            self._universe_cache[d] = rank_universe(
                self.panel,
                d,
                self.cfg.universe,
                exclude_symbols=set(exclude_symbols) | self._exclude,
                asset_ok=self.asset_ok,
            )
        return self._universe_cache[d]

    def raw_target(
        self, sleeve: str, d: pd.Timestamp, blocked: frozenset[str] = frozenset()
    ) -> dict[str, float]:
        """D_d（spec §4.3）。d 早于面板起点时视为空目标。"""
        key = (sleeve, d, blocked)
        if key in self._raw_cache:
            return self._raw_cache[key]
        if d < self.panel.dates[0]:
            out: dict[str, float] = {}
        else:
            scfg = self._sleeves[sleeve]
            on = self.risk_on(d)
            if isinstance(scfg, TrendSleeveConfig):
                flt = self._asset_on.get(sleeve)
                asset_on = {sym: bool(s.at[d]) for sym, s in flt.items()} if flt is not None else None
                out = core_target(self.panel, scfg, d, on, asset_on)
            else:
                assert isinstance(scfg, XsMomentumSleeveConfig)
                uni = self.universe(d, tuple(scfg.exclude_symbols)) if on else []
                out = satellite_target(self.panel, scfg, d, on, uni, blocked, self._ctrend.get(sleeve))
            e = self.exposure(d)
            if e < 1.0:  # 集成模式：按处于 Risk-On 的均线比例缩放（sma / tsmom 模式下 e 只取 0 或 1）
                out = {k: w * e for k, w in out.items()}
        self._raw_cache[key] = out
        return out

    def target(self, t: pd.Timestamp, overrides: Overrides | None = None) -> TargetBreakdown:
        ov = overrides or Overrides()
        on = self.risk_on(t)
        last_on = self.last_switch_on(t)
        raw: dict[str, dict[str, float]] = {}
        smoothed: dict[str, dict[str, float]] = {}
        for name, scfg in self._sleeves.items():
            raw[name] = self.raw_target(name, t, ov.blocked_at(t))

            def raw_of(d: pd.Timestamp, sleeve: str = name) -> dict[str, float]:
                return self.raw_target(sleeve, d, ov.blocked_at(d))

            smoothed[name] = smoothed_target(
                raw_of,
                t,
                risk_on=on,
                mode=scfg.rebalance_mode,
                n=scfg.smoothing_days,
                weekday=scfg.rebalance_weekday,
                risk_on_mode=self.cfg.regime.risk_on_mode,
                last_switch_on=last_on,
                overrides=ov,
            )
        weights = combine_sleeves(smoothed, {k: v.capital_weight for k, v in self._sleeves.items()})
        sat_universe = next(
            (
                self._universe_cache.get(t, [])
                for s in self._sleeves.values()
                if isinstance(s, XsMomentumSleeveConfig)
            ),
            [],
        )
        return TargetBreakdown(
            as_of=t,
            regime=Regime(self.regime.at[t, "state"]),
            switched_on=bool(self.regime.at[t, "switched_on"]),
            switched_off=bool(self.regime.at[t, "switched_off"]),
            raw=raw,
            smoothed=smoothed,
            weights=weights,
            universe=sat_universe,
            exposure=self.exposure(t),
        )

    def sleeve_of(self, col: str) -> str:
        """归属袖子（用于按袖子统计）。核心资产归核心袖子，其余归卫星。"""
        for name, s in self._sleeves.items():
            if isinstance(s, TrendSleeveConfig) and col in s.symbols:
                return name
        for name, s in self._sleeves.items():
            if isinstance(s, XsMomentumSleeveConfig):
                return name
        return next(iter(self._sleeves))
