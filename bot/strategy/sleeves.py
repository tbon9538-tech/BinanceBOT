"""每日原始目标 D_t（spec §4.3）。纯函数：只读面板与配置。"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Collection, Mapping

import numpy as np
import pandas as pd

from bot.config import TrendSleeveConfig, UniverseConfig, XsMomentumSleeveConfig
from bot.strategy import ctrend
from bot.strategy.indicators import MarketPanel, column_symbol
from bot.strategy.portfolio import cap_weights

log = logging.getLogger(__name__)

ANN = math.sqrt(365)
BLEND_LOOKBACKS = (14, 28, 56)


def core_target(
    panel: MarketPanel,
    cfg: TrendSleeveConfig,
    t: pd.Timestamp,
    risk_on: bool,
    asset_on: Mapping[str, bool] | None = None,
) -> dict[str, float]:
    """核心袖子：Risk-On 时 w = min(max_gross, target_vol / σ)；多个资产时风险等分（D-050）。

    asset_on 为单资产趋势过滤的结果（R4，asset_filter: own_sma）：未通过的资产不持有，
    它那一份风险预算留作现金，n 仍按全部核心资产计算。
    """
    if not risk_on:
        return {}
    n = len(cfg.symbols)
    out: dict[str, float] = {}
    vol = panel.vol(cfg.vol_lookback)
    for sym in cfg.symbols:
        if asset_on is not None and not asset_on.get(sym, False):
            continue
        if sym not in vol.columns:
            log.error("核心资产 %s 不在面板中", sym)
            continue
        sigma = float(vol.at[t, sym]) * ANN
        if not np.isfinite(sigma) or sigma <= 0:
            log.error("核心资产 %s 在 %s 的波动率不可用", sym, t.date())
            continue
        out[sym] = min(cfg.max_gross, cfg.target_annual_vol / sigma) / n
    return out


def rank_universe(
    panel: MarketPanel,
    t: pd.Timestamp,
    ucfg: UniverseConfig,
    *,
    exclude_symbols: Collection[str] = (),
    asset_ok: Callable[[str], bool] = lambda col: True,
) -> list[str]:
    """spec §2.4：按 30 日成交额中位数降序取前 N（并列按 symbol 升序）。返回面板列名。"""
    min_valid = math.ceil(ucfg.volume_median_days * 25 / 30)
    medqv = panel.median_quote_volume(ucfg.volume_median_days, min_valid).loc[t]
    age = panel.listing_age().loc[t]
    close = panel.close.loc[t]
    ok = (
        close.notna()
        & medqv.notna()
        & (age >= ucfg.min_listing_days)
        & (medqv >= ucfg.min_median_quote_volume)
    )
    cands = [c for c in medqv.index[ok.to_numpy()] if column_symbol(c) not in exclude_symbols and asset_ok(c)]
    ranked = sorted(cands, key=lambda c: (-float(medqv[c]), c))
    return ranked[: ucfg.universe_size]


def _window(cfg: XsMomentumSleeveConfig) -> int:
    w = max(cfg.momentum_lookback + 1, cfg.coin_sma_period, cfg.vol_lookback + 1)
    if cfg.score == "blend":
        w = max(w, max(BLEND_LOOKBACKS) + 1)
    if cfg.score == "ctrend":
        w = max(w, ctrend.WINDOW_DAYS)
    return w


def satellite_scores(
    panel: MarketPanel,
    cfg: XsMomentumSleeveConfig,
    t: pd.Timestamp,
    cols: list[str],
    model: ctrend.CtrendModel | None = None,
) -> pd.Series:
    L, s = cfg.momentum_lookback, cfg.momentum_skip
    if cfg.score == "ctrend":
        if model is None:
            raise ValueError("score: ctrend 需要 CtrendModel（由 MomentumV1 创建）")
        return model.score(t, cols)
    if cfg.score == "ret_over_vol":
        score = panel.mom(L, s).loc[t, cols] / panel.vol(L).loc[t, cols]
    elif cfg.score == "ret":
        score = panel.mom(L, s).loc[t, cols]
    else:
        parts = [panel.mom(lb, 0).loc[t, cols] / panel.vol(lb).loc[t, cols] for lb in BLEND_LOOKBACKS]
        score = sum(parts[1:], parts[0]) / len(parts)
    return score.astype("float64")


def satellite_target(
    panel: MarketPanel,
    cfg: XsMomentumSleeveConfig,
    t: pd.Timestamp,
    risk_on: bool,
    universe: list[str],
    blocked: Collection[str] = (),
    model: ctrend.CtrendModel | None = None,
) -> dict[str, float]:
    """卫星袖子：横截面动量 + 单币过滤 + 波动率目标。blocked 为冷却期中的列。

    score: ctrend 时得分只用于排序，入选资格仍是"回看期收益 > 0"（与 ret_over_vol 的 score > 0 等价，R3）。
    """
    if not risk_on or not universe:
        return {}
    cols = [c for c in universe if c not in blocked]
    if not cols:
        return {}
    complete = panel.complete(_window(cfg)).loc[t, cols]
    cols = [c for c in cols if bool(complete[c])]
    if not cols:
        return {}
    score = satellite_scores(panel, cfg, t, cols, model)
    close = panel.close.loc[t, cols]
    positive = (
        panel.mom(cfg.momentum_lookback, cfg.momentum_skip).loc[t, cols] > 0
        if cfg.score == "ctrend"
        else score > 0
    )
    ok = positive & score.notna() & (close > panel.sma(cfg.coin_sma_period).loc[t, cols])
    if cfg.max_lookback_return is not None:
        ok &= panel.lbr(cfg.momentum_lookback).loc[t, cols] <= cfg.max_lookback_return
    cand = [c for c in cols if bool(ok[c])]
    if not cand:
        return {}
    selected = sorted(cand, key=lambda c: (-float(score[c]), c))[: cfg.top_n]
    k = len(selected)
    if cfg.weighting == "equal":
        raw = dict.fromkeys(selected, 1.0 / k)
    else:
        inv = {c: 1.0 / float(panel.vol(cfg.momentum_lookback).at[t, c]) for c in selected}
        tot = sum(inv.values())
        raw = {c: v / tot for c, v in inv.items()}
    a = cap_weights(raw, cfg.max_weight_per_coin)
    loc = panel.dates.get_loc(t)
    assert isinstance(loc, int)
    r = panel.ret().iloc[loc - cfg.vol_lookback + 1 : loc + 1][list(a)]
    port = r.to_numpy() @ np.array(list(a.values()))
    sigma = float(np.std(port, ddof=1)) * ANN if len(port) > 1 else float("nan")
    if not np.isfinite(sigma) or sigma <= 0:
        log.error("卫星组合在 %s 的波动率不可用", t.date())
        return {}
    gross = min(cfg.max_gross, cfg.target_annual_vol / sigma)
    return {c: w * gross for c, w in a.items()}
