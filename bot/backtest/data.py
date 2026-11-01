"""研究数据加载：只通过 ResearchMarketView 读取，永远不包含保留集（spec §3.6、§9.3）。"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date

import pandas as pd

from bot.config import AppConfig, AssetsConfig
from bot.data.store import MarketStore, ResearchMarketView
from bot.strategy.indicators import MarketPanel, column_symbol


def approx_approved(meta: pd.DataFrame, assets: AssetsConfig) -> set[str]:
    """回测中 approved 的近似（spec §2.2 第 5 条）：不在排除名单、且不被排除正则命中的全部资产。返回交易对集合。"""
    excluded = assets.excluded_set()
    out = set()
    for _, r in meta.iterrows():
        base = str(r["base_asset"])
        if base in excluded:
            continue
        if base not in assets.pattern_exceptions and any(
            re.search(p, base) for p in assets.excluded_patterns
        ):
            continue
        out.add(str(r["symbol"]))
    return out


def load_research_panel(
    store: MarketStore,
    view: ResearchMarketView,
    cfg: AppConfig,
    assets: AssetsConfig,
    end: date | None = None,
) -> tuple[MarketPanel, Callable[[str], bool]]:
    """读取全部近似批准的 USDT 交易对（含已下架），以及大盘与核心资产，构造面板。

    从最早的数据开始读取，保证大盘状态机的重放起点与数据起点一致（spec §4.2）。
    """
    meta = store.symbol_meta()
    allowed = approx_approved(meta, assets)
    must = {cfg.regime.symbol}
    for s in cfg.enabled_sleeves().values():
        if s.type == "trend":
            must.update(s.symbols)
    symbols = sorted((set(view.symbols()) & allowed) | must)
    frames = {}
    for sym in symbols:
        df = view.read_symbol(sym, end=end)
        if not df.empty:
            frames[sym] = df
    panel = MarketPanel.from_frames(frames, gap_days=cfg.universe.segment_gap_days)

    def asset_ok(col: str) -> bool:
        return column_symbol(col) in allowed

    return panel, asset_ok
