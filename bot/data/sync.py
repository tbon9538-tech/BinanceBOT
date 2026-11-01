"""历史日线同步：data.binance.vision（含已下架交易对）+ REST 补齐最近几天（spec §9.2）。

下载与解析在线程池中进行（并发 ≤ 8），数据库写入只在主线程进行。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import pandas as pd

from bot.data.store import MarketStore
from bot.data.vision import DAILY_KLINES, MONTHLY_KLINES, VisionClient, VisionFile
from bot.exchange.base import MarketDataSource

log = logging.getLogger(__name__)

MAX_WORKERS = 8
NOT_IN_EXCHANGEINFO = "NOT_IN_EXCHANGEINFO"


@dataclass
class SyncReport:
    symbols: int = 0
    files_ingested: int = 0
    vision_rows: int = 0
    rest_rows: int = 0
    failures: list[str] = field(default_factory=list)


def _month(d: date) -> str:
    return d.strftime("%Y-%m")


def _add_months(month: str, n: int) -> str:
    y, m = int(month[:4]), int(month[5:7])
    idx = y * 12 + (m - 1) + n
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def _months_between(first: str, last: str) -> list[str]:
    out = []
    m = first
    while m <= last:
        out.append(m)
        m = _add_months(m, 1)
    return out


def usdt_symbol_meta(exchange_info: Mapping[str, Any], vision_symbols: Iterable[str]) -> list[dict[str, Any]]:
    """合并 live exchangeInfo 与 vision 目录，得到全部 USDT 交易对的元数据。"""
    info = {s["symbol"]: s for s in exchange_info.get("symbols", []) if s.get("quoteAsset") == "USDT"}
    out = []
    for sym in sorted(set(info) | set(vision_symbols)):
        s = info.get(sym)
        if s is None:
            out.append(
                {
                    "symbol": sym,
                    "base_asset": sym.removesuffix("USDT"),
                    "quote_asset": "USDT",
                    "status": NOT_IN_EXCHANGEINFO,
                    "is_spot_trading_allowed": False,
                }
            )
        else:
            out.append(
                {
                    "symbol": sym,
                    "base_asset": s["baseAsset"],
                    "quote_asset": s["quoteAsset"],
                    "status": s["status"],
                    "is_spot_trading_allowed": bool(s.get("isSpotTradingAllowed", False)),
                }
            )
    return out


def plan_symbol_files(
    vision: VisionClient,
    symbol: str,
    *,
    since: date,
    last_closed: date,
    is_trading: bool,
    ingested: set[str],
) -> list[VisionFile]:
    """列出该交易对需要下载的 vision 文件（未入库的月文件 + 尚无月文件月份的日文件）。"""
    since_m, cur_m = _month(since), _month(last_closed)
    monthly = [f for f in vision.list_monthly_files(symbol) if since_m <= f.period <= cur_m]
    todo = [f for f in monthly if f.key not in ingested]
    last_monthly = max((f.period for f in monthly), default=None)
    # 日文件只对仍在交易、或最近两个月内还有月文件的交易对检查（已下架多年的币无需逐月列目录）
    recent = last_monthly is not None and last_monthly >= _add_months(cur_m, -2)
    if is_trading or recent:
        first_daily_m = _add_months(last_monthly, 1) if last_monthly else max(since_m, _add_months(cur_m, -1))
        for m in _months_between(max(first_daily_m, since_m), cur_m):
            for f in vision.list_daily_files(symbol, m):
                if f.period <= last_closed.isoformat() and f.key not in ingested:
                    todo.append(f)
    return todo


def _download_symbol(
    vision: VisionClient, symbol: str, files: list[VisionFile]
) -> list[tuple[VisionFile, pd.DataFrame, str]]:
    return [(f, *vision.fetch(f)) for f in files]


def sync_vision(
    store: MarketStore,
    vision: VisionClient,
    symbols_meta: list[dict[str, Any]],
    *,
    since: date,
    last_closed: date,
    workers: int = MAX_WORKERS,
    progress: Callable[[str], None] | None = None,
) -> SyncReport:
    report = SyncReport(symbols=len(symbols_meta))
    ingested = store.ingested_files(MONTHLY_KLINES) | store.ingested_files(DAILY_KLINES)
    workers = max(1, min(workers, MAX_WORKERS))

    def task(meta: dict[str, Any]) -> tuple[str, list[tuple[VisionFile, pd.DataFrame, str]]]:
        sym = meta["symbol"]
        files = plan_symbol_files(
            vision,
            sym,
            since=since,
            last_closed=last_closed,
            is_trading=meta["status"] == "TRADING",
            ingested=ingested,
        )
        return sym, _download_symbol(vision, sym, files)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(task, m): m["symbol"] for m in symbols_meta}
        for i, fut in enumerate(as_completed(futures), 1):
            sym = futures[fut]
            try:
                _, results = fut.result()
            except Exception as e:  # 单个交易对失败不影响其他交易对
                log.error("同步 %s 失败：%s", sym, e)
                report.failures.append(f"{sym}: {e}")
                continue
            # 月文件先写、日文件后写，同一天以后写入的为准
            for f, df, sha in sorted(results, key=lambda x: (not x[0].is_monthly, x[0].period)):
                report.vision_rows += store.upsert_klines(sym, df, "vision")
                store.mark_file_ingested(f.key, sha, len(df))
                report.files_ingested += 1
            if progress and (i % 50 == 0 or i == len(futures)):
                progress(f"vision 进度 {i}/{len(futures)}")
    return report


def sync_rest_tail(
    store: MarketStore,
    market: MarketDataSource,
    symbols: Iterable[str],
    *,
    since: date,
    last_closed: date,
    report: SyncReport,
) -> None:
    """用 REST 补齐 vision 尚未发布的最近几天（只对仍在交易的交易对）。"""
    for sym in symbols:
        last = store.last_date(sym)
        start = date.fromisoformat(last) + timedelta(days=1) if last else since
        if start > last_closed:
            continue
        try:
            df = market.get_daily_klines(sym, start, last_closed)
        except Exception as e:
            log.error("REST 补齐 %s 失败：%s", sym, e)
            report.failures.append(f"{sym}(rest): {e}")
            continue
        report.rest_rows += store.upsert_klines(sym, df, "rest")
