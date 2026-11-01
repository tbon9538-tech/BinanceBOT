"""标的池：候选交易对、资产审核规则、流动性筛选（spec §2）。

M1 实现规则部分（§2.1、§2.2、§2.4 的单币判定）；逐日排名与组合构建在 M2-3 完成。
所有函数都是纯函数。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

import numpy as np
import pandas as pd

from bot.config import AssetsConfig


class AssetClass(StrEnum):
    APPROVED = "approved"
    EXCLUDED = "excluded"  # 在 excluded_assets 名单中
    EXCLUDED_PATTERN = "excluded_pattern"  # 被 excluded_patterns 命中，且不在例外名单中
    PENDING = "pending"  # 待人工审核，不参与交易


def classify_asset(asset: str, cfg: AssetsConfig) -> AssetClass:
    """spec §2.2：排除名单优先于正则；正则命中但在例外名单中的资产视为未命中。"""
    if asset in cfg.excluded_set():
        return AssetClass.EXCLUDED
    if asset not in cfg.pattern_exceptions and any(re.search(p, asset) for p in cfg.excluded_patterns):
        return AssetClass.EXCLUDED_PATTERN
    if asset in cfg.approved_assets:
        return AssetClass.APPROVED
    return AssetClass.PENDING


def tradable_usdt_symbols(exchange_info: Mapping[str, Any]) -> dict[str, str]:
    """spec §2.1：quoteAsset == USDT、status == TRADING、isSpotTradingAllowed。返回 symbol → baseAsset。

    不读取 permissions 字段（已恒为空数组）。
    """
    out: dict[str, str] = {}
    for s in exchange_info.get("symbols", []):
        if (
            s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"
            and s.get("isSpotTradingAllowed") is True
        ):
            out[s["symbol"]] = s["baseAsset"]
    return out


@dataclass(frozen=True, slots=True)
class LiquidityStats:
    listing_date: date  # 当前交易段的首日
    listing_age: int  # 含首尾两天的日历天数
    median_quote_volume: float | None  # 最近 volume_median_days 天的成交额中位数；有效值不足时为 None


def liquidity_stats(
    seg: pd.DataFrame, as_of: date, *, volume_median_days: int = 30, min_valid_ratio: float = 25 / 30
) -> LiquidityStats | None:
    """seg：单个交易对**当前交易段**的 float 日线（DatetimeIndex），只使用 ≤ as_of 的数据。

    spec §2.4：最近 30 天中有效值少于 25 根时，成交额中位数视为不可用。
    """
    seg = seg.loc[seg.index <= pd.Timestamp(as_of)]
    if seg.empty:
        return None
    first = seg.index[0].date()
    window_start = pd.Timestamp(as_of) - pd.Timedelta(days=volume_median_days - 1)
    qv = seg.loc[seg.index >= window_start, "quote_volume"].dropna()
    min_valid = int(np.ceil(volume_median_days * min_valid_ratio))
    med = float(qv.median()) if len(qv) >= min_valid else None
    return LiquidityStats(first, (as_of - first).days + 1, med)


def passes_liquidity(
    stats: LiquidityStats | None, *, min_listing_days: int, min_median_quote_volume: float
) -> bool:
    return (
        stats is not None
        and stats.median_quote_volume is not None
        and stats.listing_age >= min_listing_days
        and stats.median_quote_volume >= min_median_quote_volume
    )


# ---- 审核辅助标记（只用于提示人工审核，不参与自动决策）----


def annualized_vol(seg: pd.DataFrame, as_of: date, days: int = 90) -> float | None:
    s = seg.loc[
        (seg.index <= pd.Timestamp(as_of)) & (seg.index > pd.Timestamp(as_of) - pd.Timedelta(days=days))
    ]
    r = s["close"].pct_change().dropna()
    return float(r.std(ddof=1) * np.sqrt(365)) if len(r) >= 20 else None


def suspected_stablecoin(seg: pd.DataFrame, as_of: date) -> bool:
    """spec §2.2 第 3 条：90 日年化波动 < 5% 且价格与 1 的偏差 < 5%。"""
    vol = annualized_vol(seg, as_of)
    s = seg.loc[seg.index <= pd.Timestamp(as_of)]
    if vol is None or s.empty:
        return False
    return vol < 0.05 and abs(float(s["close"].iloc[-1]) - 1.0) < 0.05


def weekend_move_ratio(seg: pd.DataFrame, as_of: date, days: int = 90) -> float | None:
    """周末（周六、周日）平均绝对日收益 / 工作日平均绝对日收益。

    代币化股票/ETF 在美股休市的周末几乎不动，这个比值会明显偏低；加密资产通常接近 1。
    """
    s = seg.loc[
        (seg.index <= pd.Timestamp(as_of)) & (seg.index > pd.Timestamp(as_of) - pd.Timedelta(days=days))
    ]
    r: pd.Series = s["close"].pct_change().dropna().abs()
    dow = pd.DatetimeIndex(r.index).day_of_week
    weekend: pd.Series = r[dow >= 5]
    weekday: pd.Series = r[dow < 5]
    if len(weekend) < 8 or len(weekday) < 20 or float(weekday.mean()) == 0:
        return None
    return float(weekend.mean() / weekday.mean())


def suspected_equity_token(seg: pd.DataFrame, as_of: date, threshold: float = 0.35) -> bool:
    ratio = weekend_move_ratio(seg, as_of)
    return ratio is not None and ratio < threshold
