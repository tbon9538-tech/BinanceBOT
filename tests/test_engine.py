from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
import pytest

from bot.backtest import stats
from bot.backtest.engine import EngineOptions, run_backtest
from bot.config import AppConfig, load_config
from bot.domain.types import Regime
from bot.strategy.indicators import MarketPanel
from bot.strategy.momentum_v1 import MomentumV1
from tests.conftest import CONFIG_PATH
from tests.test_strategy import frame, geometric


@dataclass(frozen=True)
class _TB:
    weights: dict[str, float]
    regime: Regime = Regime.RISK_ON


class StubStrategy:
    """固定目标权重的桩策略，用于验证引擎的记账。"""

    def __init__(self, panel: MarketPanel, weights: dict[str, float]) -> None:
        self.weights = weights
        self.regime = pd.DataFrame({"state": "RISK_ON"}, index=panel.dates)

    def target(self, t: pd.Timestamp, ov: object = None) -> _TB:
        return _TB(self.weights)

    def sleeve_of(self, col: str) -> str:
        return "core"


@pytest.fixture
def cfg() -> AppConfig:
    return load_config(CONFIG_PATH)


def test_constant_weight_known_answer(cfg: AppConfig) -> None:
    r = 0.01
    p = MarketPanel.from_frames({"AUSDT": frame(geometric(60, r))})
    opts = EngineOptions(initial_capital=1000, fee=0, slippage=0, ideal=True)
    res = run_backtest(
        cfg, p, p.dates[0].date(), p.dates[-1].date(), opts=opts, strategy=StubStrategy(p, {"AUSDT": 0.5})
    )  # type: ignore[arg-type]
    n = len(p.dates) - 1  # 第一天收盘后才生成目标，第二天开盘建仓
    assert res.nav.iloc[-1] == pytest.approx((1 + 0.5 * r) ** n, rel=1e-9)


def test_costs_are_conserved(cfg: AppConfig) -> None:
    rng = np.random.default_rng(3)
    p = MarketPanel.from_frames({"AUSDT": frame(list(100 * np.cumprod(1 + rng.normal(0, 0.03, 80))))})
    opts = EngineOptions(initial_capital=1000, fee=0.001, slippage=0.0005, ideal=True)
    res = run_backtest(
        cfg, p, p.dates[0].date(), p.dates[-1].date(), opts=opts, strategy=StubStrategy(p, {"AUSDT": 0.6})
    )  # type: ignore[arg-type]
    assert res.daily["cost"].sum() == pytest.approx(res.trades["notional"].sum() * 0.0015)
    assert res.trades["cost"].sum() == pytest.approx(res.daily["cost"].sum())


def test_delisted_position_exits_with_haircut(cfg: AppConfig) -> None:
    p = MarketPanel.from_frames({"AUSDT": frame(geometric(30, 0.0)), "BUSDT": frame(geometric(60, 0.0))})
    opts = EngineOptions(initial_capital=1000, fee=0, slippage=0, delist_haircut=0.5, ideal=True)
    res = run_backtest(
        cfg, p, p.dates[0].date(), p.dates[-1].date(), opts=opts, strategy=StubStrategy(p, {"AUSDT": 0.4})
    )  # type: ignore[arg-type]
    assert len(res.delist_exits) == 1
    assert res.nav.iloc[-1] == pytest.approx(1 - 0.4 * 0.5)  # 价格不变：只损失 haircut


def _market(n: int = 420, seed: int = 11) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    frames = {
        "BTCUSDT": frame(list(100 * np.cumprod(1 + 0.002 + rng.normal(0, 0.03, n))), "2021-01-01", qv=5e9)
    }
    for i in range(8):
        drift = 0.003 - 0.001 * i
        frames[f"C{i}USDT"] = frame(
            list(10 * np.cumprod(1 + drift + rng.normal(0, 0.05, n))), "2021-01-01", qv=1e8 * (i + 1)
        )
    return frames


def test_no_lookahead(cfg: AppConfig) -> None:
    frames = _market()
    p1 = MarketPanel.from_frames(frames)
    k = 300
    shocked = {s: f.copy() for s, f in frames.items()}
    for s, f in shocked.items():  # 在 k+1 日放入极端值
        f.iloc[k + 1 :, :] = f.iloc[k + 1 :, :] * (100.0 if s != "C0USDT" else 0.01)
    p2 = MarketPanel.from_frames(shocked)
    start, end = p1.dates[200].date(), p1.dates[-1].date()
    r1 = run_backtest(cfg, p1, start, end, strategy=MomentumV1(cfg, p1))
    r2 = run_backtest(cfg, p2, start, end, strategy=MomentumV1(cfg, p2))
    cut = p1.dates[k]
    pd.testing.assert_series_equal(r1.nav.loc[:cut], r2.nav.loc[:cut])
    assert (r1.daily["weights"].loc[:cut] == r2.daily["weights"].loc[:cut]).all()


def test_momentum_v1_runs_and_respects_limits(cfg: AppConfig) -> None:
    p = MarketPanel.from_frames(_market())
    res = run_backtest(cfg, p, p.dates[150].date(), p.dates[-1].date())
    assert (res.daily["gross"] <= 0.95 + 1e-6).all()
    off = res.daily["regime"] == "RISK_OFF"
    assert (res.daily.loc[off, "weights"] == "{}").all()
    s = stats.summarize(res.nav)
    assert math.isfinite(s["sharpe"])


def test_stats_and_dsr() -> None:
    idx = pd.date_range("2020-01-01", periods=400)
    nav = pd.Series(np.linspace(1, 2, 400), index=idx)
    assert stats.max_drawdown(nav) == 0
    nav2 = pd.Series([1, 2, 1, 1.5, 2.5], index=idx[:5], dtype=float)
    assert stats.max_drawdown(nav2) == pytest.approx(-0.5)
    assert stats.drawdown_episodes(nav2)[0]["recovery"] == "2020-01-05"
    rng = np.random.default_rng(0)
    r = pd.Series(rng.normal(0.001, 0.02, 2000))
    single = stats.dsr_from_returns(r, [1.0])
    many = stats.dsr_from_returns(r, list(rng.normal(0.5, 0.5, 200)))
    assert 0 <= many < single <= 1  # 试验越多，折扣越大
    mdds = stats.block_bootstrap_mdd(r, n=50)
    assert (mdds <= 0).all() and len(mdds) == 50


def test_dates_validation(cfg: AppConfig) -> None:
    p = MarketPanel.from_frames({"BTCUSDT": frame(geometric(10, 0.0))})
    with pytest.raises(ValueError):
        run_backtest(cfg, p, date(2030, 1, 1), date(2030, 2, 1))
