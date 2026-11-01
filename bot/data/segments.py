"""交易段切分与上市天数（spec §2.3）。

相邻两根日线的日期间隔超过 gap_days 天时切分为新交易段（例如 LUNAUSDT 旧币下架后同名新币上市）。
所有指标只在同一交易段内计算。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import pandas as pd


@dataclass(frozen=True, slots=True)
class Segment:
    start: date
    end: date
    bars: int


def _as_dates(dates: Sequence[date] | pd.DatetimeIndex) -> list[date]:
    if isinstance(dates, pd.DatetimeIndex):
        return [d.date() for d in dates]
    return list(dates)


def segment_ids(dates: Sequence[date] | pd.DatetimeIndex, gap_days: int = 7) -> list[int]:
    """为每根日线返回所属交易段编号（从 0 开始）。dates 必须升序且不重复。"""
    ds = _as_dates(dates)
    out: list[int] = []
    seg = 0
    for i, d in enumerate(ds):
        if i > 0:
            diff = (d - ds[i - 1]).days
            if diff <= 0:
                raise ValueError(f"日期必须严格升序：{ds[i - 1]} → {d}")
            if diff > gap_days:
                seg += 1
        out.append(seg)
    return out


def split_segments(dates: Sequence[date] | pd.DatetimeIndex, gap_days: int = 7) -> list[Segment]:
    ds = _as_dates(dates)
    ids = segment_ids(ds, gap_days)
    segs: list[Segment] = []
    for i, d in enumerate(ds):
        if i == 0 or ids[i] != ids[i - 1]:
            segs.append(Segment(d, d, 1))
        else:
            s = segs[-1]
            segs[-1] = Segment(s.start, d, s.bars + 1)
    return segs


def listing_age(dates: Sequence[date] | pd.DatetimeIndex, gap_days: int = 7) -> list[int]:
    """每根日线的上市天数：所在交易段首日到当天的日历天数（含首尾两天）。"""
    ds = _as_dates(dates)
    ids = segment_ids(ds, gap_days)
    out: list[int] = []
    seg_start = ds[0] if ds else None
    for i, d in enumerate(ds):
        if i == 0 or ids[i] != ids[i - 1]:
            seg_start = d
        assert seg_start is not None
        out.append((d - seg_start).days + 1)
    return out


def current_segment(df: pd.DataFrame, gap_days: int = 7) -> pd.DataFrame:
    """只保留以日期为索引的 DataFrame 中最后一个交易段。"""
    if df.empty:
        return df
    ids = segment_ids(pd.DatetimeIndex(df.index), gap_days)
    last = ids[-1]
    mask = [i == last for i in ids]
    return df.loc[mask]
