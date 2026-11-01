"""现货 5 分钟 K 线（研究 R11），来自 data.binance.vision，只用于研究。

- 按月下载；还没有月文件的月份（例如刚结束的月份）用逐日文件补齐；每个文件按同名 .CHECKSUM 校验 sha256，
  缓存在 data/raw/ 下，重复运行不会重新下载；
- 只返回 end（研究截止日）及之前的 K 线；保留集守卫由调用方用 HoldoutGuard 检查（D-085）；
- 时间戳 2025-01-01 起为微秒，统一换算为毫秒。
"""

from __future__ import annotations

import hashlib
import io
import logging
import zipfile
from datetime import date, timedelta
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from bot.data.klines import normalize_ms

log = logging.getLogger(__name__)

VISION_URL = "https://data.binance.vision"
MONTHLY = "data/spot/monthly/klines"
DAILY = "data/spot/daily/klines"


class ChecksumMismatch(Exception):
    pass


def _months(start: date, end: date) -> list[date]:
    out, d = [], date(start.year, start.month, 1)
    while d <= end:
        out.append(d)
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return out


def fetch_verified(http: httpx.Client, raw_dir: Path, key: str) -> bytes | None:
    """下载并按 .CHECKSUM 校验；本地已有校验通过的副本时直接使用。文件不存在（404）时返回 None。"""
    resp = http.get(f"{VISION_URL}/{key}.CHECKSUM")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    expected = resp.text.strip().split()[0].lower()
    path = raw_dir / key
    if path.exists() and hashlib.sha256(data := path.read_bytes()).hexdigest() == expected:
        return data
    resp = http.get(f"{VISION_URL}/{key}")
    resp.raise_for_status()
    data = resp.content
    if hashlib.sha256(data).hexdigest() != expected:
        raise ChecksumMismatch(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".zip.part")
    tmp.write_bytes(data)
    tmp.replace(path)
    return data


def parse_csv(data: bytes) -> pd.DataFrame:
    """zip 内的 K 线 CSV（可能有表头）→ open_ms、open、high、low、close、quote_volume（float）。"""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        [name] = [n for n in z.namelist() if n.endswith(".csv")]
        raw = z.read(name)
    df = pd.read_csv(io.BytesIO(raw), header=None)
    if not str(df.iat[0, 0]).strip().lstrip("-").isdigit():  # 首行是表头
        df = df.iloc[1:]
    out = pd.DataFrame(
        {
            "open_ms": [normalize_ms(int(x)) for x in df[0]],
            "open": df[1].astype("float64").to_numpy(),
            "high": df[2].astype("float64").to_numpy(),
            "low": df[3].astype("float64").to_numpy(),
            "close": df[4].astype("float64").to_numpy(),
            "quote_volume": df[7].astype("float64").to_numpy(),
        }
    )
    return out


def load_5m(
    symbol: str, start: date, end: date, raw_dir: Path, *, transport: httpx.BaseTransport | None = None
) -> pd.DataFrame:
    """[start, end] 的 5 分钟 K 线，以开盘时间（UTC，不带时区）为索引，按时间排序、去重。"""
    parts: list[pd.DataFrame] = []
    with httpx.Client(timeout=60.0, transport=transport, follow_redirects=False) as http:
        for m in _months(start, end):
            tag = f"{m:%Y-%m}"
            data = fetch_verified(http, raw_dir, f"{MONTHLY}/{symbol}/5m/{symbol}-5m-{tag}.zip")
            if data is not None:
                parts.append(parse_csv(data))
                continue
            d = m  # 还没有月文件：逐日补齐
            while d.month == m.month and d <= end:
                day = fetch_verified(http, raw_dir, f"{DAILY}/{symbol}/5m/{symbol}-5m-{d:%Y-%m-%d}.zip")
                if day is None:
                    log.warning("%s %s 的 5 分钟数据不存在", symbol, d)
                else:
                    parts.append(parse_csv(day))
                d += timedelta(days=1)
    if not parts:
        return pd.DataFrame(columns=["open", "high", "low", "close", "quote_volume"])
    df = pd.concat(parts, ignore_index=True).drop_duplicates("open_ms").sort_values("open_ms")
    df.index = pd.to_datetime(df.pop("open_ms").to_numpy(np.int64), unit="ms")
    lo, hi = pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(days=1)
    return df.loc[(df.index >= lo) & (df.index < hi)]
