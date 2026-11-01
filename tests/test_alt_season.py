"""山寨季过滤（研究 R8）与报告分段指标（D-085）。"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from bot.backtest.engine import BacktestResult
from bot.backtest.report import build_summary
from bot.backtest.sim_env import sim_config
from bot.config import XsMomentumSleeveConfig
from bot.strategy.indicators import MarketPanel
from bot.strategy.momentum_v1 import MomentumV1
from bot.strategy.sleeves import ALT_SEASON_MIN_COINS, alt_season

N_ALTS = 8


def _frames(alt_drift: float, btc_drift: float = 0.002, n: int = 400) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(5)
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    drifts = {"BTCUSDT": btc_drift} | {f"X{i}USDT": alt_drift + 0.0002 * i for i in range(N_ALTS)}
    out = {}
    for k, (sym, d) in enumerate(drifts.items()):
        c = 100 * np.cumprod(1 + d + rng.normal(0, 0.01, n))
        out[sym] = pd.DataFrame(
            {
                "open": c,
                "high": c * 1.01,
                "low": c * 0.99,
                "close": c,
                "quote_volume": np.full(n, 1e9 / (k + 1)),
            },
            index=idx,
        )
    return out


ALTS = [f"X{i}USDT" for i in range(N_ALTS)]


def test_alt_season_compares_basket_with_btc() -> None:
    t = pd.Timestamp("2025-01-01")
    strong = MarketPanel.from_frames(_frames(alt_drift=0.006))
    weak = MarketPanel.from_frames(_frames(alt_drift=-0.003))
    assert alt_season(strong, ALTS, "BTCUSDT", t, 60) is True
    assert alt_season(weak, ALTS, "BTCUSDT", t, 60) is False
    assert alt_season(strong, ALTS[: ALT_SEASON_MIN_COINS - 1], "BTCUSDT", t, 60) is False  # 币太少
    assert alt_season(strong, ALTS, "NOPEUSDT", t, 60) is False


def _cfg(base_cfg: dict[str, Any], lookback: int | None):  # type: ignore[no-untyped-def]
    base_cfg["sleeves"]["satellite"]["alt_season_lookback"] = lookback
    return sim_config(base_cfg)


def test_default_has_no_filter(base_cfg) -> None:  # type: ignore[no-untyped-def]
    sat = sim_config(base_cfg).sleeves["satellite"]
    assert isinstance(sat, XsMomentumSleeveConfig) and sat.alt_season_lookback is None


def test_satellite_off_outside_alt_season_and_unchanged_inside(base_cfg) -> None:  # type: ignore[no-untyped-def]
    import copy

    t = pd.Timestamp("2025-01-01")
    weak = MarketPanel.from_frames(_frames(alt_drift=0.001, btc_drift=0.004))  # 山寨币仍上涨，但跑输 BTC
    plain = MomentumV1(_cfg(copy.deepcopy(base_cfg), None), weak).raw_target("satellite", t)
    filtered = MomentumV1(_cfg(copy.deepcopy(base_cfg), 60), weak).raw_target("satellite", t)
    assert plain and filtered == {}  # 不过滤时卫星有持仓，过滤后为空
    strong = MarketPanel.from_frames(_frames(alt_drift=0.006))
    a = MomentumV1(_cfg(copy.deepcopy(base_cfg), None), strong).raw_target("satellite", t)
    b = MomentumV1(_cfg(copy.deepcopy(base_cfg), 60), strong).raw_target("satellite", t)
    assert a and a == b  # 山寨季内规则完全不变
    core_plain = MomentumV1(_cfg(copy.deepcopy(base_cfg), None), weak).raw_target("core", t)
    core_filtered = MomentumV1(_cfg(copy.deepcopy(base_cfg), 60), weak).raw_target("core", t)
    assert core_plain == core_filtered  # 核心袖子不受影响


def test_summary_reports_segments() -> None:
    idx = pd.date_range("2025-01-01", "2025-12-31", freq="D")
    nav = pd.Series(np.linspace(1.0, 1.5, len(idx)), index=idx)
    daily = pd.DataFrame({"navu": nav, "nav": nav, "gross": 0.5, "turnover": 0.0, "cost": 0.0}, index=idx)
    res = BacktestResult(daily=daily, trades=pd.DataFrame())
    summ = build_summary(res, {}, segment_split=date(2025, 7, 1))
    for k in ("seg_a_sharpe", "seg_a_mdd", "seg_b_sharpe", "seg_b_calmar"):
        assert k in summ
    assert "seg_a_sharpe" not in build_summary(res, {})
