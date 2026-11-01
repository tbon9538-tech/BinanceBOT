"""`data` 与 `assets` 子命令的实现（M1）。"""

from __future__ import annotations

import argparse
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from bot.app.assets_review import build_review, pending_review, write_outputs
from bot.config import AppConfig, load_assets
from bot.data.store import MarketStore
from bot.data.sync import SyncReport, sync_rest_tail, sync_vision, usdt_symbol_meta
from bot.data.vision import VisionClient
from bot.exchange.binance.client import BinanceHttpClient
from bot.exchange.binance.market_data import BinanceMarketData

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_PARTIAL_FAILURE = 4


def last_closed_day(now: datetime | None = None) -> date:
    """最近一根已收盘日线的日期（UTC）。"""
    now = now or datetime.now(UTC)
    return now.date() - timedelta(days=1)


def _market(cfg: AppConfig) -> BinanceMarketData:
    return BinanceMarketData(
        BinanceHttpClient(cfg.data.market_data_rest, timeout_sec=cfg.execution.http_timeout_sec)
    )


def _print_coverage(store: MarketStore) -> None:
    cov = store.coverage_by_year()
    meta = store.symbol_meta()
    print("\n各年份覆盖（交易对数 / 交易对·天）：")
    for _, r in cov.iterrows():
        print(f"  {r['year']}: {r['symbols']:>4} 个交易对，{r['rows']:>7} 行")
    if not meta.empty:
        with_data = set(store.symbols())
        m = meta[meta["symbol"].isin(with_data)]
        counts = m["status"].value_counts().to_dict()
        print(f"\n有数据的 USDT 交易对：{len(m)} 个；按当前状态：{counts}")


def cmd_data(cfg: AppConfig, args: argparse.Namespace) -> int:
    store = MarketStore(Path(args.db))
    if args.action == "report":
        _print_coverage(store)
        return EXIT_OK

    since = date.fromisoformat(args.since)
    last_closed = last_closed_day()
    market = _market(cfg)
    vision = VisionClient(Path(args.raw_dir))
    try:
        info = market.get_exchange_info()
        meta = usdt_symbol_meta(info, vision.list_usdt_symbols())
        store.upsert_symbol_meta(meta)
        if args.symbols:
            wanted = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
            meta = [m for m in meta if m["symbol"] in wanted]
        print(f"待同步 USDT 交易对：{len(meta)} 个；数据区间 {since} ～ {last_closed}")
        report: SyncReport = sync_vision(
            store, vision, meta, since=since, last_closed=last_closed, workers=args.workers, progress=print
        )
        trading = [m["symbol"] for m in meta if m["status"] == "TRADING"]
        sync_rest_tail(store, market, trading, since=since, last_closed=last_closed, report=report)
    finally:
        vision.close()
        market.client.close()

    print(
        f"\n完成：入库 vision 文件 {report.files_ingested} 个，vision 行 {report.vision_rows}，"
        f"REST 行 {report.rest_rows}；失败 {len(report.failures)} 项"
    )
    for f in report.failures[:20]:
        print(f"  失败：{f}")
    _print_coverage(store)
    return EXIT_PARTIAL_FAILURE if report.failures else EXIT_OK


def cmd_assets(cfg: AppConfig, args: argparse.Namespace) -> int:
    store = MarketStore(Path(args.db))
    assets = load_assets(cfg.universe.assets_file)
    as_of = date.fromisoformat(args.as_of) if args.as_of else last_closed_day()
    market = _market(cfg)
    try:
        info = market.get_exchange_info()
    finally:
        market.client.close()
    result = build_review(store, info, assets, cfg.universe, as_of)
    md_path, draft_path = write_outputs(result, assets, Path(args.out_dir))
    pend = pending_review(result)
    print(f"数据截至 {as_of}。待审核资产 {len(pend)} 个（只列前 30 个）：")
    for _, r in pend.head(30).iterrows():
        flags = ",".join(
            f
            for f, on in [("疑似股票/ETF", r["suspected_equity"]), ("疑似稳定币", r["suspected_stable"])]
            if on
        )
        rank, sym, medqv = int(r["liquidity_rank"]), r["symbol"], r["medqv_30d_musd"]
        print(f"  #{rank:>3} {sym:<14} 成交额中位数 {medqv:>8} 百万  {flags}")
    if result.stale_symbols:
        print(f"注意：{len(result.stale_symbols)} 个交易对的本地数据未更新到 {as_of}，请先运行 data sync")
    print(f"\n完整清单：{md_path}\n审核草稿：{draft_path}（程序不会修改 config/assets.yaml）")
    return EXIT_OK
