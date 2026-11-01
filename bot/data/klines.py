"""日线 K 线的统一表示与解析（spec §3.2、§9.2）。

统一格式（"raw frame"）：价格与数量保留交易所给出的十进制字符串，不经过 float，保证入库无精度损失。
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

import pandas as pd

KLINE_COLUMNS = [
    "open_time_ms",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "trades",
    "close_time_ms",
]
PRICE_COLUMNS = ["open", "high", "low", "close", "volume", "quote_volume"]
DAY_MS = 86_400_000
_MICROSECOND_THRESHOLD = 10**14  # 16 位时间戳为微秒（data.binance.vision 2025-01-01 起）


def normalize_ms(ts: int) -> int:
    """统一为毫秒：大于 1e14 的视为微秒。"""
    return ts // 1000 if ts > _MICROSECOND_THRESHOLD else ts


def ms_to_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in KLINE_COLUMNS})


def _rows_to_frame(rows: Iterable[Sequence[Any]]) -> pd.DataFrame:
    """rows 为币安 K 线的 12 列格式（REST 与 vision CSV 相同）。"""
    out = []
    for r in rows:
        open_ms = normalize_ms(int(r[0]))
        close_ms = normalize_ms(int(r[6]))
        if open_ms % DAY_MS != 0:
            raise ValueError(f"日线开盘时间不是 UTC 00:00：{open_ms}")
        out.append(
            {
                "open_time_ms": open_ms,
                "date": ms_to_date(open_ms),
                "open": str(r[1]),
                "high": str(r[2]),
                "low": str(r[3]),
                "close": str(r[4]),
                "volume": str(r[5]),
                "quote_volume": str(r[7]),
                "trades": int(r[8]),
                "close_time_ms": close_ms,
            }
        )
    if not out:
        return empty_frame()
    df = pd.DataFrame(out, columns=KLINE_COLUMNS)
    return df.drop_duplicates("open_time_ms", keep="last").sort_values("open_time_ms").reset_index(drop=True)


def parse_rest_klines(rows: Sequence[Sequence[Any]], server_time_ms: int) -> pd.DataFrame:
    """解析 GET /api/v3/klines 的结果；closeTime ≥ 服务器时间的行（未收盘）一律丢弃。"""
    closed = [r for r in rows if int(r[6]) < server_time_ms]
    return _rows_to_frame(closed)


def parse_vision_csv(content: bytes) -> pd.DataFrame:
    """解析 data.binance.vision 的 K 线 CSV：通常没有表头；若首行是表头则跳过。"""
    text = content.decode("utf-8")
    rows = [r for r in csv.reader(io.StringIO(text)) if r]
    if rows and not rows[0][0].strip().lstrip("-").isdigit():
        rows = rows[1:]
    for r in rows:
        if len(r) < 9:
            raise ValueError(f"K 线 CSV 列数不足：{r}")
    return _rows_to_frame(rows)


def to_float_frame(raw: pd.DataFrame) -> pd.DataFrame:
    """raw frame → 以日期为索引的 float 数据，供指标计算使用。"""
    df = raw.copy()
    for c in PRICE_COLUMNS:
        df[c] = df[c].astype("float64")
    df.index = pd.to_datetime(df["date"])
    df.index.name = "date"
    return df[[*PRICE_COLUMNS, "trades"]]
