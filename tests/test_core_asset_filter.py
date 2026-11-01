"""核心袖子的单资产趋势过滤（研究 R4）：未通过的资产不持有、预算不挪用；BTC 单资产时与大盘过滤等价。"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from bot.backtest.sim_env import sim_config
from bot.config import TrendSleeveConfig
from bot.strategy.indicators import MarketPanel
from bot.strategy.momentum_v1 import MomentumV1
from bot.strategy.sleeves import ANN, core_target


def _frames(n: int = 400) -> dict[str, pd.DataFrame]:
    """BTC 持续上涨、ETH 持续下跌，另有 8 个山寨币供卫星使用。"""
    rng = np.random.default_rng(3)
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    drifts = {"BTCUSDT": 0.004, "ETHUSDT": -0.004} | {f"X{i}USDT": 0.003 - 0.0004 * i for i in range(8)}
    out = {}
    for k, (sym, d) in enumerate(drifts.items()):
        c = 100 * np.cumprod(1 + d + rng.normal(0, 0.02, n))
        qv = np.full(n, 1e9 / (k + 1))
        out[sym] = pd.DataFrame(
            {"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "quote_volume": qv}, index=idx
        )
    return out


def _cfg(base_cfg: dict[str, Any], **core: Any):  # type: ignore[no-untyped-def]
    base_cfg["sleeves"]["core"].update(core)
    base_cfg["sleeves"]["satellite"]["exclude_symbols"] = ["BTCUSDT", "ETHUSDT"]
    return sim_config(base_cfg)


def test_default_is_no_asset_filter(base_cfg) -> None:  # type: ignore[no-untyped-def]
    core = sim_config(base_cfg).sleeves["core"]
    assert isinstance(core, TrendSleeveConfig) and core.asset_filter == "none"


def test_filtered_asset_is_skipped_and_budget_not_reallocated() -> None:
    p = MarketPanel.from_frames(_frames())
    cfg = TrendSleeveConfig(
        type="trend", capital_weight=0.5, target_annual_vol=0.35, symbols=["BTCUSDT", "ETHUSDT"]
    )
    t = p.dates[-1]
    both = core_target(p, cfg, t, True)
    only_btc = core_target(p, cfg, t, True, {"BTCUSDT": True, "ETHUSDT": False})
    assert set(both) == {"BTCUSDT", "ETHUSDT"} and set(only_btc) == {"BTCUSDT"}
    sigma = float(p.vol(30).at[t, "BTCUSDT"]) * ANN
    assert only_btc["BTCUSDT"] == pytest.approx(min(0.95, 0.35 / sigma) / 2)  # 仍按 n = 2 分预算
    assert only_btc["BTCUSDT"] == both["BTCUSDT"]
    assert core_target(p, cfg, t, False, {"BTCUSDT": True, "ETHUSDT": True}) == {}  # 大盘 Risk-Off 优先


def test_strategy_drops_downtrending_eth_only_with_own_filter(base_cfg) -> None:  # type: ignore[no-untyped-def]
    p = MarketPanel.from_frames(_frames())
    t = p.dates[-1]
    syms = ["BTCUSDT", "ETHUSDT"]
    plain = MomentumV1(_cfg(base_cfg, symbols=syms), p).target(t)
    assert set(plain.raw["core"]) == {"BTCUSDT", "ETHUSDT"}  # 只跟随大盘：ETH 下跌也持有
    own = MomentumV1(_cfg(base_cfg, symbols=syms, asset_filter="own_sma", asset_sma_period=100), p).target(t)
    assert set(own.raw["core"]) == {"BTCUSDT"}
    assert own.raw["core"]["BTCUSDT"] == plain.raw["core"]["BTCUSDT"]
    assert "ETHUSDT" not in own.raw["satellite"]  # ETH 已移出卫星候选


def test_btc_only_own_filter_matches_regime_every_day(base_cfg) -> None:  # type: ignore[no-untyped-def]
    """BTC 单资产、N 与缓冲和大盘过滤相同时，单资产过滤与当前行为逐日一致（R4b own100 = 当前配置）。"""
    p = MarketPanel.from_frames(_frames())
    plain = MomentumV1(sim_config(base_cfg), p)
    own = MomentumV1(sim_config({**base_cfg, "sleeves": {**base_cfg["sleeves"], "core": {
        **base_cfg["sleeves"]["core"], "asset_filter": "own_sma", "asset_sma_period": 100, "asset_buffer": 0.02,
    }}}), p)  # fmt: skip
    for d in p.dates[150:]:
        assert own.raw_target("core", d) == plain.raw_target("core", d)
