"""live 只读公共行情（data-api.binance.vision），实现 MarketDataSource（spec §3.1、§3.2）。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

import pandas as pd

from bot.data.klines import DAY_MS, empty_frame, parse_rest_klines
from bot.exchange.binance.client import BinanceHttpClient

KLINES_LIMIT = 1000


def _epoch_days(d: date) -> int:
    return (d - date(1970, 1, 1)).days


class BinanceMarketData:
    def __init__(self, client: BinanceHttpClient) -> None:
        self.client = client

    def get_exchange_info(self) -> Mapping[str, Any]:
        info: Mapping[str, Any] = self.client.get("/api/v3/exchangeInfo")
        self.client.rate.set_limit_from_exchange_info(info)
        return info

    def get_daily_klines(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """返回 [start, end] 内已收盘的日线（raw frame，价格为十进制字符串），自动分页。"""
        if end < start:
            return empty_frame()
        start_ms = _epoch_days(start) * DAY_MS
        end_ms = (_epoch_days(end) + 1) * DAY_MS - 1
        server_ms = self.client.server_time_ms()
        frames = []
        cursor = start_ms
        while cursor <= end_ms:
            rows = self.client.get(
                "/api/v3/klines",
                {
                    "symbol": symbol,
                    "interval": "1d",
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": KLINES_LIMIT,
                },
            )
            if not rows:
                break
            frames.append(parse_rest_klines(rows, server_ms))
            last_open = int(rows[-1][0])
            if len(rows) < KLINES_LIMIT:
                break
            cursor = last_open + DAY_MS
        if not frames:
            return empty_frame()
        df = pd.concat(frames, ignore_index=True)
        return df.drop_duplicates("open_time_ms").sort_values("open_time_ms").reset_index(drop=True)
