"""指标（spec §4.1）与面板数据。

面板（panel）是"日期 × 列"的 float DataFrame，日期为连续的日历日（DatetimeIndex，UTC 日期）。
每一列对应一个交易对的**一个交易段**（spec §2.3）：
- 最新交易段的列名就是交易对名，例如 "LUNAUSDT"；
- 更早交易段的列名为 "LUNAUSDT~2020-08-01"（~ 后为该段首日）。
这样所有滚动窗口天然不会跨段。下单时用 `column_symbol()` 取回交易对名。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from bot.data.segments import segment_ids

SEGMENT_SEP = "~"
FIELDS = ("open", "high", "low", "close", "quote_volume")


def column_symbol(col: str) -> str:
    return col.split(SEGMENT_SEP, 1)[0]


def _segment_columns(symbol: str, df: pd.DataFrame, gap_days: int) -> dict[str, pd.DataFrame]:
    if df.empty:
        return {}
    ids = segment_ids(pd.DatetimeIndex(df.index), gap_days)
    last = ids[-1]
    out: dict[str, pd.DataFrame] = {}
    arr = np.asarray(ids)
    for sid in sorted(set(ids)):
        part = df.loc[arr == sid]
        name = symbol if sid == last else f"{symbol}{SEGMENT_SEP}{part.index[0].date().isoformat()}"
        out[name] = part
    return out


@dataclass
class MarketPanel:
    """多个交易对的日线面板，并缓存常用指标。"""

    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    quote_volume: pd.DataFrame
    _cache: dict[tuple[object, ...], pd.DataFrame] = field(default_factory=dict, repr=False)

    @classmethod
    def from_frames(
        cls,
        frames: Mapping[str, pd.DataFrame],
        *,
        gap_days: int = 7,
        start: date | None = None,
        end: date | None = None,
    ) -> MarketPanel:
        """frames：symbol → float 日线（DatetimeIndex）。自动按交易段拆列，并补齐为连续日历日。"""
        cols: dict[str, pd.DataFrame] = {}
        for sym, df in frames.items():
            cols.update(_segment_columns(sym, df.sort_index(), gap_days))
        if not cols:
            raise ValueError("没有任何数据")
        lo = min(df.index[0] for df in cols.values())
        hi = max(df.index[-1] for df in cols.values())
        if start is not None:
            lo = max(lo, pd.Timestamp(start))
        if end is not None:
            hi = min(hi, pd.Timestamp(end))
        idx = pd.date_range(lo, hi, freq="D", name="date")
        def field_panel(f: str) -> pd.DataFrame:
            return pd.DataFrame({c: d[f] for c, d in cols.items()}).reindex(idx).astype("float64")

        return cls(
            open=field_panel("open"),
            high=field_panel("high"),
            low=field_panel("low"),
            close=field_panel("close"),
            quote_volume=field_panel("quote_volume"),
        )

    @property
    def dates(self) -> pd.DatetimeIndex:
        return pd.DatetimeIndex(self.close.index)

    @property
    def columns(self) -> list[str]:
        return list(self.close.columns)

    def _get(self, key: tuple[object, ...], fn: object) -> pd.DataFrame:
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    # ---- spec §4.1 ----
    def ret(self) -> pd.DataFrame:
        """r_t = c_t / c_{t−1} − 1；任一端缺失时为 NaN。"""
        return self._get(("ret",), lambda: self.close / self.close.shift(1) - 1)

    def mom(self, lookback: int, skip: int) -> pd.DataFrame:
        """mom(L, s)_t = c_{t−s} / c_{t−L} − 1。"""
        return self._get(
            ("mom", lookback, skip), lambda: self.close.shift(skip) / self.close.shift(lookback) - 1
        )

    def vol(self, lookback: int) -> pd.DataFrame:
        """日波动：最近 L 个日收益的标准差（ddof=1），要求 L 个收益全部有效。"""
        return self._get(
            ("vol", lookback), lambda: self.ret().rolling(lookback, min_periods=lookback).std(ddof=1)
        )

    def sma(self, n: int) -> pd.DataFrame:
        return self._get(("sma", n), lambda: self.close.rolling(n, min_periods=n).mean())

    def lbr(self, lookback: int) -> pd.DataFrame:
        return self.mom(lookback, 0)

    def complete(self, window: int) -> pd.DataFrame:
        """[t−W+1, t] 窗口内收盘价无缺失（spec §3.4）。"""
        return self._get(
            ("complete", window),
            lambda: self.close.notna().rolling(window, min_periods=window).sum() >= window,
        )

    def atr(self, n: int) -> pd.DataFrame:
        """Wilder ATR：初值为最早 n 个 TR 的简单平均，之后 ATR_t = ATR_{t−1} + (TR_t − ATR_{t−1}) / n。"""

        def calc() -> pd.DataFrame:
            prev = self.close.shift(1)
            tr = np.maximum.reduce(
                [
                    (self.high - self.low).to_numpy(),
                    (self.high - prev).abs().to_numpy(),
                    (self.low - prev).abs().to_numpy(),
                ]
            )
            tr_df = pd.DataFrame(tr, index=self.close.index, columns=self.close.columns)
            return tr_df.apply(lambda s: _wilder(s, n))

        return self._get(("atr", n), calc)

    def median_quote_volume(self, days: int, min_valid: int) -> pd.DataFrame:
        """最近 days 个日历日的成交额中位数，有效值少于 min_valid 时为 NaN（spec §2.4，D-040）。"""
        return self._get(
            ("medqv", days, min_valid),
            lambda: self.quote_volume.rolling(days, min_periods=min_valid).median(),
        )

    def listing_age(self) -> pd.DataFrame:
        """每列（交易段）从首日到当天的日历天数（含首尾），首日之前为 NaN。"""

        def calc() -> pd.DataFrame:
            out = {}
            day_num = (self.dates - self.dates[0]).days.to_numpy()
            for c in self.columns:
                valid = self.close[c].notna().to_numpy()
                if not valid.any():
                    out[c] = np.full(len(day_num), np.nan)
                    continue
                first = day_num[np.argmax(valid)]
                age = (day_num - first + 1).astype("float64")
                age[day_num < first] = np.nan
                out[c] = age
            return pd.DataFrame(out, index=self.close.index)

        return self._get(("age",), calc)

    def last_valid_date(self) -> pd.Series:
        """每列最后一根日线的日期（用于识别下架）。"""
        return self.close.apply(lambda s: s.last_valid_index())

    def subset(self, cols: Iterable[str]) -> MarketPanel:
        cs = list(cols)
        return MarketPanel(self.open[cs], self.high[cs], self.low[cs], self.close[cs], self.quote_volume[cs])


def _wilder(tr: pd.Series, n: int) -> pd.Series:
    vals = tr.to_numpy(dtype="float64")
    out = np.full(len(vals), np.nan)
    run: list[float] = []
    atr = np.nan
    for i, v in enumerate(vals):
        if np.isnan(v):
            run.clear()
            atr = np.nan
            continue
        if np.isnan(atr):
            run.append(v)
            if len(run) == n:
                atr = float(np.mean(run))
                out[i] = atr
            continue
        atr = atr + (v - atr) / n
        out[i] = atr
    return pd.Series(out, index=tr.index)
