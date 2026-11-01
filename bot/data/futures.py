"""U 本位永续的历史数据（研究 R10）：资金费率与日线，来自 data.binance.vision，只用于研究。

- 只下载、只读取研究截止日 `end` 及之前的数据（保留集守卫由调用方用 HoldoutGuard 检查，D-085）；
- 资金费率按"所属持有日"汇总：在结算时刻收到的费用属于它之前的那个持有日（结算时刻取整到整点、再减 1 毫秒所在的 UTC 日）。
  因此 d 日 00:00 结算的费用记在 d−1 日；在 d 日全天持有空头，收到的是 d 日 08:00、16:00 与 d+1 日 00:00 三次。
"""

from __future__ import annotations

import io
import logging
import zipfile
from datetime import date, timedelta

import httpx
import pandas as pd

from bot.data.klines import parse_vision_csv, to_float_frame
from bot.data.vision import VisionClient

log = logging.getLogger(__name__)

FUNDING_PREFIX = "data/futures/um/monthly/fundingRate"
KLINES_PREFIX = "data/futures/um/monthly/klines"


def months(start: date, end: date) -> list[str]:
    """start 与 end 所在月份之间（含）的 YYYY-MM 列表。"""
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def funding_key(symbol: str, month: str) -> str:
    return f"{FUNDING_PREFIX}/{symbol}/{symbol}-fundingRate-{month}.zip"


def kline_key(symbol: str, month: str) -> str:
    return f"{KLINES_PREFIX}/{symbol}/1d/{symbol}-1d-{month}.zip"


def _csv(data: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist() if n.endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"zip 中应只有一个 CSV，实际 {names}")
        return z.read(names[0])


def parse_funding_csv(csv: bytes) -> pd.Series:
    """资金费率 CSV（calc_time, funding_interval_hours, last_funding_rate）→ 以结算时刻（UTC）为索引的费率。"""
    df = pd.read_csv(io.BytesIO(csv))
    if "calc_time" not in df.columns:  # 兼容没有表头的文件
        df = pd.read_csv(
            io.BytesIO(csv), header=None, names=["calc_time", "funding_interval_hours", "last_funding_rate"]
        )
    idx = pd.to_datetime(df["calc_time"].astype("int64"), unit="ms", utc=True)
    return pd.Series(
        df["last_funding_rate"].astype("float64").to_numpy(), index=idx, name="funding"
    ).sort_index()


def daily_funding(raw: pd.Series) -> pd.Series:
    """按所属持有日汇总（见模块说明），索引为不带时区的日期。"""
    if raw.empty:
        return pd.Series(dtype="float64")
    # 结算时刻有几毫秒的抖动（例如 00:00:00.005），先取整到整点再减 1 毫秒，午夜结算才会归到前一天
    day = (
        (pd.DatetimeIndex(raw.index).round("h") - pd.Timedelta(milliseconds=1))
        .tz_convert("UTC")
        .tz_localize(None)
        .normalize()
    )
    return raw.groupby(day).sum().rename("funding")


def _fetch_months(client: VisionClient, keys: list[str]) -> list[bytes]:
    out: list[bytes] = []
    for key in keys:
        try:
            data, _ = client.fetch_verified(key)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                log.info("%s 不存在（该月尚未上市或没有数据），跳过", key)
                continue
            raise
        out.append(_csv(data))
    return out


def load_funding(client: VisionClient, symbol: str, start: date, end: date) -> pd.Series:
    """[start, end] 持有日的每日资金费率之和（多下载一个月以覆盖 end 次日 00:00 的结算）。"""
    keys = [funding_key(symbol, m) for m in months(start, end + timedelta(days=1))]
    parts = [parse_funding_csv(c) for c in _fetch_months(client, keys)]
    if not parts:
        return pd.Series(dtype="float64")
    daily = daily_funding(pd.concat(parts).sort_index())
    return daily.loc[(daily.index >= pd.Timestamp(start)) & (daily.index <= pd.Timestamp(end))]


def load_perp_daily(client: VisionClient, symbol: str, start: date, end: date) -> pd.DataFrame:
    """永续日线（float，DatetimeIndex），只保留 [start, end]。"""
    frames = [
        parse_vision_csv(c) for c in _fetch_months(client, [kline_key(symbol, m) for m in months(start, end)])
    ]
    if not frames:
        return pd.DataFrame()
    df = to_float_frame(pd.concat(frames, ignore_index=True).drop_duplicates("date"))
    return df.loc[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))].sort_index()
