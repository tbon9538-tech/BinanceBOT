from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from bot.config import TrendSleeveConfig, UniverseConfig, XsMomentumSleeveConfig
from bot.strategy.indicators import MarketPanel, column_symbol
from bot.strategy.portfolio import cap_weights, combine_sleeves
from bot.strategy.regime import regime_series
from bot.strategy.sleeves import core_target, rank_universe, satellite_target
from bot.strategy.smoothing import Overrides, StopWindow, smoothed_target


def frame(close: list[float], start: str = "2024-01-01", qv: float = 1e8) -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(close), freq="D")
    c = pd.Series(close, index=idx, dtype="float64")
    return pd.DataFrame(
        {"open": c.shift(1).fillna(c), "high": c * 1.01, "low": c * 0.99, "close": c, "quote_volume": qv}
    )


def geometric(n: int, r: float, p0: float = 100.0) -> list[float]:
    return [p0 * (1 + r) ** i for i in range(n)]


def test_indicators_match_definitions() -> None:
    close = [100.0, 110.0, 99.0, 108.9, 120.0, 118.0]
    p = MarketPanel.from_frames({"A": frame(close)})
    t = p.dates[-1]
    assert p.mom(4, 1).at[t, "A"] == pytest.approx(close[-2] / close[-5] - 1)
    rets = np.array(close[1:]) / np.array(close[:-1]) - 1
    assert p.vol(3).at[t, "A"] == pytest.approx(np.std(rets[-3:], ddof=1))
    assert p.sma(3).at[t, "A"] == pytest.approx(np.mean(close[-3:]))
    assert math.isnan(p.sma(7).at[t, "A"])
    assert p.listing_age().at[t, "A"] == 6


def test_wilder_atr_initial_average() -> None:
    p = MarketPanel.from_frames({"A": frame(geometric(30, 0.0))})
    tr = 2.0  # high − low = 1.01c − 0.99c，c = 100
    assert p.atr(14).iloc[14]["A"] == pytest.approx(tr)  # 第 1 根没有前收盘，TR 从第 2 根开始
    assert math.isnan(p.atr(14).iloc[13]["A"])


def test_panel_splits_segments_into_columns() -> None:
    old = frame(geometric(20, 0.01), "2022-05-01")
    new = frame(geometric(10, 0.0, 1.0), "2022-05-31")
    p = MarketPanel.from_frames({"LUNAUSDT": pd.concat([old, new])})
    assert set(p.columns) == {"LUNAUSDT", "LUNAUSDT~2022-05-01"}
    assert column_symbol("LUNAUSDT~2022-05-01") == "LUNAUSDT"
    assert p.close["LUNAUSDT"].first_valid_index() == pd.Timestamp("2022-05-31")
    assert p.listing_age().at[pd.Timestamp("2022-06-09"), "LUNAUSDT"] == 10


def test_regime_buffer_and_hold() -> None:
    close = pd.Series(
        [100.0] * 5 + [101.0, 103.0, 101.5, 97.0, 99.0], index=pd.date_range("2024-01-01", periods=10)
    )
    r = regime_series(close, sma_period=5, buffer=0.02)
    # SMA≈100～101：101 不触发（< 1.02 × SMA），103 触发 RISK_ON，101.5 保持，97 触发 RISK_OFF
    assert list(r["state"].iloc[5:]) == ["RISK_OFF", "RISK_ON", "RISK_ON", "RISK_OFF", "RISK_OFF"]
    assert bool(r["switched_on"].iloc[6]) and bool(r["switched_off"].iloc[8])
    assert (r["state"].iloc[:4] == "RISK_OFF").all()


@pytest.mark.parametrize(
    ("raw", "expected_total"),
    [
        ({"a": 1.0}, 0.25),
        ({"a": 0.5, "b": 0.5}, 0.5),
        ({"a": 0.6, "b": 0.3, "c": 0.1}, 0.75),
        ({k: 0.2 for k in "abcde"}, 1.0),
    ],
)
def test_cap_weights(raw: dict[str, float], expected_total: float) -> None:
    w = cap_weights(raw, 0.25)
    assert max(w.values()) <= 0.25 + 1e-12
    assert sum(w.values()) == pytest.approx(expected_total)


def test_cap_weights_redistributes_excess() -> None:
    w = cap_weights({"a": 0.5, "b": 0.3, "c": 0.1, "d": 0.1}, 0.3)
    assert w["a"] == pytest.approx(0.3)
    assert sum(w.values()) == pytest.approx(1.0)
    assert w["c"] == pytest.approx(w["d"])


def test_combine_sleeves() -> None:
    w = combine_sleeves({"core": {"BTC": 0.8}, "sat": {"ETH": 0.5, "BTC": 0.1}}, {"core": 0.5, "sat": 0.5})
    assert w == pytest.approx({"BTC": 0.45, "ETH": 0.25})


def test_core_target_vol_scaling() -> None:
    rng = np.random.default_rng(1)
    close = list(100 * np.cumprod(1 + rng.normal(0, 0.04, 60)))
    p = MarketPanel.from_frames({"BTCUSDT": frame(close)})
    t = p.dates[-1]
    cfg = TrendSleeveConfig(type="trend", capital_weight=0.5, target_annual_vol=0.30, symbols=["BTCUSDT"])
    sigma = p.vol(30).at[t, "BTCUSDT"] * math.sqrt(365)
    assert core_target(p, cfg, t, True)["BTCUSDT"] == pytest.approx(min(0.95, 0.30 / sigma))
    assert core_target(p, cfg, t, False) == {}


def _sat_cfg(**kw) -> XsMomentumSleeveConfig:  # type: ignore[no-untyped-def]
    base = dict(
        type="xs_momentum",
        capital_weight=0.5,
        target_annual_vol=0.30,
        momentum_lookback=10,
        coin_sma_period=5,
        vol_lookback=10,
        top_n=2,
    )
    base.update(kw)
    return XsMomentumSleeveConfig(**base)  # type: ignore[arg-type]


def _sat_panel() -> MarketPanel:
    rng = np.random.default_rng(7)
    n = 40

    def walk(drift: float) -> list[float]:
        return list(100 * np.cumprod(1 + drift + rng.normal(0, 0.02, n)))

    return MarketPanel.from_frames(
        {
            "UPUSDT1": frame(walk(0.010)),
            "UPUSDT2": frame(walk(0.006)),
            "UPUSDT3": frame(walk(0.004)),
            "DOWNUSDT": frame(walk(-0.01)),
            "PUMPUSDT": frame(geometric(n, 0.12)),  # 回看期涨幅远超 150%
        }
    )


def test_satellite_filters_rank_and_vol_target() -> None:
    p = _sat_panel()
    t = p.dates[-1]
    cfg = _sat_cfg()
    uni = p.columns
    w = satellite_target(p, cfg, t, True, uni)
    assert "DOWNUSDT" not in w and "PUMPUSDT" not in w  # 得分为负 / 超过涨幅上限
    assert len(w) == 2 and set(w) <= {"UPUSDT1", "UPUSDT2", "UPUSDT3"}
    a = dict.fromkeys(w, 0.25)  # 等权 0.5 被封顶到 0.25
    r = p.ret().iloc[-10:][list(a)]
    sigma = float(np.std(r.to_numpy() @ np.array(list(a.values())), ddof=1)) * math.sqrt(365)
    gross = min(0.95, 0.30 / sigma)
    assert sum(w.values()) == pytest.approx(0.5 * gross)
    blocked = set(w)
    w2 = satellite_target(p, cfg, t, True, uni, blocked=blocked)
    assert not (set(w2) & blocked)
    assert satellite_target(p, cfg, t, False, uni) == {}
    no_cap = satellite_target(p, _sat_cfg(max_lookback_return=None, top_n=5), t, True, uni)
    assert "PUMPUSDT" in no_cap


def test_rank_universe_floor_age_and_exclusion() -> None:
    frames = {
        "BTCUSDT": frame(geometric(120, 0.0), qv=9e9),
        "BIGUSDT": frame(geometric(120, 0.0), qv=5e8),
        "MIDUSDT": frame(geometric(120, 0.0), qv=1e8),
        "TINYUSDT": frame(geometric(120, 0.0), qv=1e6),
        "NEWUSDT": frame(geometric(29, 0.0), "2024-04-01", qv=9e8),
    }
    p = MarketPanel.from_frames(frames)
    t = p.dates[-1]
    ucfg = UniverseConfig(universe_size=2, min_median_quote_volume=5e6, min_listing_days=90)
    assert rank_universe(p, t, ucfg, exclude_symbols={"BTCUSDT"}) == ["BIGUSDT", "MIDUSDT"]
    assert rank_universe(p, t, ucfg, asset_ok=lambda c: c != "BIGUSDT", exclude_symbols={"BTCUSDT"}) == [
        "MIDUSDT"
    ]


def _raw_seq(day0: pd.Timestamp) -> object:
    def raw(d: pd.Timestamp) -> dict[str, float]:
        return {"A": 0.7} if d >= day0 else {}

    return raw


def test_smoothing_ramp_immediate_and_riskoff() -> None:
    s = pd.Timestamp("2024-03-10")
    raw = _raw_seq(s)
    common = dict(mode="smoothed", n=7, weekday=0, last_switch_on=s, overrides=Overrides())
    t = s + pd.Timedelta(days=2)
    ramp = smoothed_target(raw, t, risk_on=True, risk_on_mode="ramp", **common)  # type: ignore[arg-type]
    assert ramp["A"] == pytest.approx(0.7 * 3 / 7)
    imm = smoothed_target(raw, t, risk_on=True, risk_on_mode="immediate", **common)  # type: ignore[arg-type]
    assert imm["A"] == pytest.approx(0.7)
    assert smoothed_target(raw, t, risk_on=False, risk_on_mode="ramp", **common) == {}  # type: ignore[arg-type]


def test_smoothing_stop_excludes_old_vectors_only() -> None:
    raw = _raw_seq(pd.Timestamp("2024-01-01"))
    stop = StopWindow(pd.Timestamp("2024-03-01"), pd.Timestamp("2024-03-08"))
    ov = Overrides(stops={"A": stop})
    kw = dict(
        risk_on=True, mode="smoothed", n=7, weekday=0, risk_on_mode="ramp", last_switch_on=None, overrides=ov
    )
    assert smoothed_target(raw, pd.Timestamp("2024-03-08"), **kw) == {}  # type: ignore[arg-type]
    # 冷却期结束后新算出的目标逐日纳入，旧目标不会让它"一次性买回"
    assert smoothed_target(raw, pd.Timestamp("2024-03-10"), **kw)["A"] == pytest.approx(0.7 * 2 / 7)  # type: ignore[arg-type]
    assert ov.blocked_at(pd.Timestamp("2024-03-05")) == {"A"}
    assert ov.blocked_at(pd.Timestamp("2024-03-09")) == frozenset()


def test_smoothing_weekly_uses_last_rebalance_day() -> None:
    def raw(d: pd.Timestamp) -> dict[str, float]:
        return {"A": float(d.day)}

    t = pd.Timestamp("2024-03-14")  # 周四；最近的周一是 3/11
    out = smoothed_target(
        raw,
        t,
        risk_on=True,
        mode="weekly",
        n=7,
        weekday=0,
        risk_on_mode="ramp",
        last_switch_on=None,
        overrides=Overrides(),
    )
    assert out == {"A": 11.0}
