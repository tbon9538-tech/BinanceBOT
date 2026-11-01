"""测试用的数据构造工具。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

import pandas as pd

from bot.data.klines import DAY_MS, KLINE_COLUMNS


def day_ms(d: date) -> int:
    return (d - date(1970, 1, 1)).days * DAY_MS


def kline_row(d: date, close: float, qv: float = 1e7, micro: bool = False) -> list[str | int]:
    """币安 12 列 K 线格式的一行。"""
    o = day_ms(d)
    c = o + DAY_MS - 1
    if micro:
        o, c = o * 1000, c * 1000 + 999
    px = f"{close:.8f}"
    return [
        o,
        px,
        f"{close * 1.01:.8f}",
        f"{close * 0.99:.8f}",
        px,
        "100.0",
        c,
        f"{qv:.8f}",
        10,
        "0",
        "0",
        "0",
    ]


def raw_frame(
    start: date,
    n: int,
    close: Callable[[int], float] = lambda i: 100.0 + i,
    qv: Callable[[int], float] = lambda i: 1e7,
    skip: set[int] | None = None,
) -> pd.DataFrame:
    """连续 n 天的 raw frame（价格为字符串）；skip 中的序号被跳过，用来制造缺口。"""
    rows = []
    for i in range(n):
        if skip and i in skip:
            continue
        d = start + timedelta(days=i)
        r = kline_row(d, close(i), qv(i))
        rows.append(
            {
                "open_time_ms": int(r[0]),
                "date": d.isoformat(),
                "open": r[1],
                "high": r[2],
                "low": r[3],
                "close": r[4],
                "volume": r[5],
                "quote_volume": r[7],
                "trades": 10,
                "close_time_ms": int(r[6]),
            }
        )
    return pd.DataFrame(rows, columns=KLINE_COLUMNS)


def csv_bytes(rows: list[list[str | int]], header: bool = False) -> bytes:
    lines = []
    if header:
        lines.append("open_time,open,high,low,close,volume,close_time,quote_volume,count,tbb,tbq,ignore")
    lines += [",".join(str(x) for x in r) for r in rows]
    return ("\n".join(lines) + "\n").encode()
