"""每次运行的数据质量检查（spec §3.3）。纯函数，输入为 float 日线（DatetimeIndex）。

1. 大盘标的在 t 日及之前 sma_period 天内不能缺日线（缺失时中止本次运行，由调用方告警）；
2. 每根日线必须满足 0 < low ≤ min(open, close) ≤ max(open, close) ≤ high：不满足的日线剔除并记 WARN，
   剔除后该币在指标窗口内出现缺口，按 §3.4 的完整性要求当天自然不参与计算；
3. 收盘价单日变化超过阈值（默认 0.6）只记 WARN，不中止。
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd


def invalid_bar_dates(df: pd.DataFrame) -> list[date]:
    """不满足 0 < low ≤ min(open, close) ≤ max(open, close) ≤ high 的日线日期（含任一价格缺失的行）。"""
    if df.empty:
        return []
    lo = df[["open", "close"]].min(axis=1)
    hi = df[["open", "close"]].max(axis=1)
    ok = (df["low"] > 0) & (df["low"] <= lo) & (hi <= df["high"])
    return [pd.Timestamp(d).date() for d in df.index[~ok.fillna(False).astype(bool)]]


def drop_invalid_bars(df: pd.DataFrame) -> tuple[pd.DataFrame, list[date]]:
    bad = invalid_bar_dates(df)
    if not bad:
        return df, []
    return df.drop(index=[pd.Timestamp(d) for d in bad]), bad


def missing_days(df: pd.DataFrame, t: date, days: int) -> list[date]:
    """[t − days + 1, t] 中没有（有效）日线的日期。"""
    have = {pd.Timestamp(d).date() for d in df.index}
    return [d for i in range(days) if (d := t - timedelta(days=days - 1 - i)) not in have]


def price_jumps(close: pd.DataFrame, t: pd.Timestamp, threshold: float) -> dict[str, float]:
    """t 日收盘相对前一日变化超过阈值的列 → 变化幅度。"""
    if t not in close.index:
        return {}
    loc = close.index.get_loc(t)
    if not isinstance(loc, int) or loc == 0:
        return {}
    r = close.iloc[loc] / close.iloc[loc - 1] - 1
    return {str(c): float(v) for c, v in r.items() if pd.notna(v) and abs(v) > threshold}
