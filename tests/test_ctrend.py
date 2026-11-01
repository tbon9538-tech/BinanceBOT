"""CTREND 式信号（研究 R3）：信号定义、标准化、没有前视、样本不足时不选币、与策略的集成。"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from bot.backtest.sim_env import sim_config
from bot.config import XsMomentumSleeveConfig
from bot.strategy import ctrend
from bot.strategy.ctrend import CtrendModel, signal_panels, zscore
from bot.strategy.indicators import MarketPanel
from bot.strategy.momentum_v1 import MomentumV1
from bot.strategy.sleeves import satellite_target

LAST = date(2025, 6, 30)


N_ALTS = 20


def _panel(seed: int = 1, n_days: int = 420, noise: float = 0.03, spread: float = 0.0003) -> MarketPanel:
    """BTC + 20 个漂移各不相同、持续存在的山寨币（回归每个横截面至少需要"信号数 + 2"个币）。"""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(end=pd.Timestamp(LAST), periods=n_days, freq="D")
    frames = {}
    for k, sym in enumerate(["BTCUSDT"] + [f"X{i}USDT" for i in range(N_ALTS)]):
        drift = 0.003 - spread * k
        c = 100 * np.cumprod(1 + drift + rng.normal(0, noise, n_days))
        qv = 1e8 * (k + 1) * np.exp(rng.normal(0, 0.3, n_days))
        frames[sym] = pd.DataFrame(
            {"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": qv / c, "quote_volume": qv},
            index=idx,
        )
    return MarketPanel.from_frames(frames)


def _cfg(**kw: object) -> XsMomentumSleeveConfig:
    base: dict[str, object] = {
        "type": "xs_momentum",
        "capital_weight": 0.5,
        "momentum_lookback": 21,
        "score": "ctrend",
        "target_annual_vol": 0.35,
    }
    base.update(kw)
    return XsMomentumSleeveConfig(**base)  # type: ignore[arg-type]


def _all(panel: MarketPanel):  # type: ignore[no-untyped-def]
    return lambda d: [c for c in panel.columns if c != "BTCUSDT"]


def test_default_config_does_not_use_ctrend(base_cfg) -> None:  # type: ignore[no-untyped-def]
    sat = sim_config(base_cfg).sleeves["satellite"]
    assert isinstance(sat, XsMomentumSleeveConfig) and sat.score == "ret_over_vol"
    assert (sat.ctrend_signals, sat.ctrend_window_weeks, sat.ctrend_horizon_days) == ("price_volume", 52, 7)


def test_signals_match_definitions() -> None:
    p = _panel()
    t, c = p.dates[-1], "X1USDT"
    sig = signal_panels(p, "price_volume")
    assert set(sig) == {"p3", "p5", "p10", "p20", "p50", "v3", "v5", "v10", "v20"}
    assert set(signal_panels(p, "price")) == {"p3", "p5", "p10", "p20", "p50"}
    close = p.close[c]
    assert sig["p10"].at[t, c] == pytest.approx(close.iloc[-1] / close.iloc[-10:].mean() - 1)
    qv = p.quote_volume[c]
    assert sig["v5"].at[t, c] == pytest.approx(math.log(qv.iloc[-5:].mean() / qv.iloc[-50:].mean()))
    assert math.isnan(sig["p50"][c].iloc[48])  # 不足 50 天没有值


def test_zscore_clips_and_handles_flat_columns() -> None:
    x = pd.DataFrame({"a": [0.0] * 20 + [100.0], "b": [5.0] * 21})
    z = zscore(x)
    assert z["a"].max() == ctrend.Z_CLIP and (z["b"] == 0).all()


def test_no_lookahead_future_prices_do_not_change_score() -> None:
    p = _panel()
    t = p.dates[300]
    model = CtrendModel(p, _cfg(ctrend_window_weeks=26), _all(p))
    cols = [c for c in p.columns if c != "BTCUSDT"]
    w, s = model.weights(t), model.score(t, cols)
    assert w is not None and s.notna().all()
    # 把 t 之后的价格与成交额全部改掉：t 日的系数与得分不能变
    rng = np.random.default_rng(7)
    after = p.dates > t
    close, qv = p.close.copy(), p.quote_volume.copy()
    close.loc[after] *= rng.uniform(0.5, 2.0, size=close.loc[after].shape)
    qv.loc[after] *= rng.uniform(0.1, 10.0, size=qv.loc[after].shape)
    p2 = MarketPanel(open=p.open, high=p.high, low=p.low, close=close, quote_volume=qv)
    model2 = CtrendModel(p2, _cfg(ctrend_window_weeks=26), _all(p2))
    np.testing.assert_allclose(model2.weights(t), w)
    pd.testing.assert_series_equal(model2.score(t, cols), s)


def test_uses_only_realized_anchors_and_needs_half_window() -> None:
    p = _panel()
    model = CtrendModel(p, _cfg(ctrend_window_weeks=52), _all(p))
    # 前 50 天信号不全；需要 26 个已实现的锚点：大约第 50 + 26×7 天之后才有系数
    assert model.weights(p.dates[200]) is None
    assert model.weights(p.dates[260]) is not None
    t = p.dates[260]
    used = model.anchors[model.anchors <= t - pd.Timedelta(days=7)]
    assert used[-1] + pd.Timedelta(days=7) <= t


def test_persistent_winners_rank_high() -> None:
    """漂移差异持续存在时，学到的权重应当把真实漂移高的币排在前面（得分与漂移排序正相关）。"""
    p = _panel(n_days=700, noise=0.01, spread=0.0005)
    alts = [f"X{i}USDT" for i in range(N_ALTS)]
    model = CtrendModel(p, _cfg(ctrend_signals="price", ctrend_window_weeks=52), _all(p))
    score = model.score(p.dates[-1], alts)
    true_rank = pd.Series(-np.arange(N_ALTS, dtype="float64"), index=alts)  # X0 漂移最大
    assert score.rank().corr(true_rank.rank()) > 0.5


def test_satellite_selects_nothing_without_coefficients_and_keeps_gate() -> None:
    p = _panel()
    cfg = _cfg(coin_sma_period=50, top_n=3)
    model = CtrendModel(p, cfg, _all(p))
    uni = [f"X{i}USDT" for i in range(N_ALTS)]
    assert satellite_target(p, cfg, p.dates[120], True, uni, model=model) == {}  # 系数不足
    t = p.dates[-1]
    out = satellite_target(p, cfg, t, True, uni, model=model)
    assert 0 < len(out) <= 3
    for c in out:  # 入选资格不变：回看期收益 > 0、收盘价在 50 日均线之上
        assert p.mom(21, 0).at[t, c] > 0 and p.close.at[t, c] > p.sma(50).at[t, c]
    with pytest.raises(ValueError):
        satellite_target(p, cfg, t, True, uni)  # ctrend 必须提供模型


def test_strategy_builds_model_only_for_ctrend(base_cfg) -> None:  # type: ignore[no-untyped-def]
    p = _panel()
    plain = MomentumV1(sim_config(base_cfg), p)
    assert plain._ctrend == {}
    base_cfg["sleeves"]["satellite"]["score"] = "ctrend"
    strat = MomentumV1(sim_config(base_cfg), p)
    assert set(strat._ctrend) == {"satellite"}
    tb = strat.target(p.dates[-1])
    assert all(w >= 0 for w in tb.weights.values())
