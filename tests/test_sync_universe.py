from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bot.app.assets_review import build_review, draft_assets, pending_review, write_outputs
from bot.config import AssetsConfig, UniverseConfig
from bot.data.klines import to_float_frame
from bot.data.store import MarketStore
from bot.data.sync import SyncReport, plan_symbol_files, sync_rest_tail, sync_vision, usdt_symbol_meta
from bot.data.vision import VisionFile
from bot.strategy.universe import (
    AssetClass,
    classify_asset,
    liquidity_stats,
    passes_liquidity,
    suspected_equity_token,
    suspected_stablecoin,
    tradable_usdt_symbols,
)
from tests.helpers import raw_frame

MONTHLY = "data/spot/monthly/klines/{s}/1d/{s}-1d-{p}.zip"
DAILY = "data/spot/daily/klines/{s}/1d/{s}-1d-{p}.zip"


class FakeVision:
    def __init__(
        self, monthly: dict[str, list[str]], daily: dict[str, list[str]], fail: set[str] = frozenset()
    ):  # type: ignore[assignment]
        self.monthly, self.daily, self.fail = monthly, daily, fail
        self.daily_listed: list[tuple[str, str]] = []

    def list_monthly_files(self, symbol: str) -> list[VisionFile]:
        return [VisionFile(MONTHLY.format(s=symbol, p=p)) for p in self.monthly.get(symbol, [])]

    def list_daily_files(self, symbol: str, month: str) -> list[VisionFile]:
        self.daily_listed.append((symbol, month))
        return [
            VisionFile(DAILY.format(s=symbol, p=p)) for p in self.daily.get(symbol, []) if p.startswith(month)
        ]

    def fetch(self, f: VisionFile) -> tuple[pd.DataFrame, str]:
        sym = f.key.split("/")[4]
        if sym in self.fail:
            raise RuntimeError("download failed")
        if f.is_monthly:
            start = date.fromisoformat(f.period + "-01")
            n = (pd.Timestamp(start) + pd.offsets.MonthEnd(0)).day
        else:
            start, n = date.fromisoformat(f.period), 1
        return raw_frame(start, n), "sha"


def test_plan_files_for_trading_and_delisted_symbols() -> None:
    v = FakeVision(
        {"BTCUSDT": ["2026-08", "2026-09"], "OLDUSDT": ["2019-01", "2019-02"]},
        {"BTCUSDT": ["2026-10-01", "2026-10-30", "2026-10-31"]},
    )
    last_closed = date(2026, 10, 30)
    todo = plan_symbol_files(
        v, "BTCUSDT", since=date(2018, 1, 1), last_closed=last_closed, is_trading=True,  # type: ignore[arg-type]
        ingested={MONTHLY.format(s="BTCUSDT", p="2026-08")},
    )  # fmt: skip
    assert [f.period for f in todo] == ["2026-09", "2026-10-01", "2026-10-30"]  # 不含尚未收盘的 10-31
    todo_old = plan_symbol_files(
        v,
        "OLDUSDT",
        since=date(2018, 1, 1),
        last_closed=last_closed,
        is_trading=False,
        ingested=set(),  # type: ignore[arg-type]
    )
    assert [f.period for f in todo_old] == ["2019-01", "2019-02"]
    assert ("OLDUSDT", "2019-03") not in v.daily_listed  # 已下架多年的币不逐月列日文件


def test_usdt_symbol_meta_merges_exchange_info_and_vision() -> None:
    info = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "status": "TRADING",
                "isSpotTradingAllowed": True,
            },
            {
                "symbol": "ETHBTC",
                "baseAsset": "ETH",
                "quoteAsset": "BTC",
                "status": "TRADING",
                "isSpotTradingAllowed": True,
            },
        ]
    }
    meta = {m["symbol"]: m for m in usdt_symbol_meta(info, ["BTCUSDT", "GONEUSDT"])}
    assert set(meta) == {"BTCUSDT", "GONEUSDT"}
    assert meta["GONEUSDT"]["status"] == "NOT_IN_EXCHANGEINFO"
    assert meta["GONEUSDT"]["base_asset"] == "GONE"


def test_sync_vision_writes_rows_and_records_failures(tmp_path: Path) -> None:
    store = MarketStore(tmp_path / "m.db")
    v = FakeVision({"AUSDT": ["2024-01"], "BADUSDT": ["2024-01"]}, {}, fail={"BADUSDT"})
    meta = [{"symbol": "AUSDT", "status": "BREAK"}, {"symbol": "BADUSDT", "status": "BREAK"}]
    rep = sync_vision(store, v, meta, since=date(2024, 1, 1), last_closed=date(2024, 3, 1), workers=2)  # type: ignore[arg-type]
    assert rep.files_ingested == 1 and rep.vision_rows == 31
    assert len(rep.failures) == 1 and rep.failures[0].startswith("BADUSDT")
    rep2 = sync_vision(store, v, meta[:1], since=date(2024, 1, 1), last_closed=date(2024, 3, 1))  # type: ignore[arg-type]
    assert rep2.files_ingested == 0  # 断点续传：已入库的文件不再下载


def test_sync_rest_tail_fills_recent_days(tmp_path: Path) -> None:
    store = MarketStore(tmp_path / "m.db")
    store.upsert_klines("AUSDT", raw_frame(date(2026, 10, 1), 28), "vision")  # 到 10-28

    class FakeMarket:
        def __init__(self) -> None:
            self.calls: list[tuple[str, date, date]] = []

        def get_exchange_info(self):  # type: ignore[no-untyped-def]
            return {}

        def get_daily_klines(self, symbol: str, start: date, end: date) -> pd.DataFrame:
            self.calls.append((symbol, start, end))
            return raw_frame(start, (end - start).days + 1)

    m = FakeMarket()
    rep = SyncReport()
    sync_rest_tail(store, m, ["AUSDT"], since=date(2018, 1, 1), last_closed=date(2026, 10, 31), report=rep)
    assert m.calls == [("AUSDT", date(2026, 10, 29), date(2026, 10, 31))]
    assert rep.rest_rows == 3 and store.last_date("AUSDT") == "2026-10-31"


ASSETS = AssetsConfig(
    approved_assets=["ETH"],
    excluded_assets={"stable": ["USDC"]},
    excluded_patterns=[r"^[A-Z0-9]+(UP|DOWN|BULL|BEAR)$"],
    pattern_exceptions=["JUP"],
)


@pytest.mark.parametrize(
    ("asset", "cls"),
    [
        ("ETH", AssetClass.APPROVED),
        ("USDC", AssetClass.EXCLUDED),
        ("BTCUP", AssetClass.EXCLUDED_PATTERN),
        ("JUP", AssetClass.PENDING),
        ("SUPER", AssetClass.PENDING),
        ("SOL", AssetClass.PENDING),
    ],
)
def test_classify_asset(asset: str, cls: AssetClass) -> None:
    assert classify_asset(asset, ASSETS) is cls


def test_tradable_usdt_symbols_filters_status_and_permission() -> None:
    info = {
        "symbols": [
            {
                "symbol": "AUSDT",
                "baseAsset": "A",
                "quoteAsset": "USDT",
                "status": "TRADING",
                "isSpotTradingAllowed": True,
            },
            {
                "symbol": "BUSDT",
                "baseAsset": "B",
                "quoteAsset": "USDT",
                "status": "BREAK",
                "isSpotTradingAllowed": True,
            },
            {
                "symbol": "CUSDT",
                "baseAsset": "C",
                "quoteAsset": "USDT",
                "status": "TRADING",
                "isSpotTradingAllowed": False,
            },
            {
                "symbol": "ABTC",
                "baseAsset": "A",
                "quoteAsset": "BTC",
                "status": "TRADING",
                "isSpotTradingAllowed": True,
            },
        ]
    }
    assert tradable_usdt_symbols(info) == {"AUSDT": "A"}


def _float(start: date, n: int, **kw) -> pd.DataFrame:  # type: ignore[no-untyped-def]
    return to_float_frame(raw_frame(start, n, **kw))


def test_liquidity_stats_window_and_age() -> None:
    as_of = date(2024, 4, 30)
    df = _float(date(2024, 1, 1), 121, qv=lambda i: 1e6 * (i + 1))
    st = liquidity_stats(df, as_of)
    assert st is not None and st.listing_age == 121
    assert st.median_quote_volume == pytest.approx(np.median([1e6 * (i + 1) for i in range(91, 121)]))
    assert passes_liquidity(st, min_listing_days=90, min_median_quote_volume=5e6)
    sparse = _float(date(2024, 1, 1), 121, skip=set(range(100, 110)))  # 最近 30 天只有 20 根
    st2 = liquidity_stats(sparse, as_of)
    assert st2 is not None and st2.median_quote_volume is None
    assert not passes_liquidity(st2, min_listing_days=90, min_median_quote_volume=0)


def test_suspected_stable_and_equity_flags() -> None:
    as_of = date(2026, 9, 30)
    start = as_of - timedelta(days=119)
    stable = _float(start, 120, close=lambda i: 1.0 + 0.0001 * ((-1) ** i))
    assert suspected_stablecoin(stable, as_of)
    rng = np.random.default_rng(0)
    rets = rng.normal(0, 0.02, 120)
    # 周末收益设为 0：模拟美股休市
    days = [start + timedelta(days=i) for i in range(120)]
    rets = [0.0 if d.weekday() >= 5 else r for d, r in zip(days, rets, strict=True)]
    px = 100 * np.cumprod(1 + np.array(rets))
    equity = _float(start, 120, close=lambda i: float(px[i]))
    assert suspected_equity_token(equity, as_of, "NVDAB")
    assert not suspected_equity_token(equity, as_of, "BTC")  # 名字不以 B 结尾：不标记
    crypto = _float(start, 120, close=lambda i: float(100 * np.cumprod(1 + rng.normal(0, 0.02, 120))[i]))
    assert not suspected_equity_token(crypto, as_of, "SHIB")


def test_assets_review_end_to_end(tmp_path: Path) -> None:
    store = MarketStore(tmp_path / "m.db")
    as_of = date(2026, 10, 31)
    start = as_of - timedelta(days=199)
    store.upsert_klines("ETHUSDT", raw_frame(start, 200, qv=lambda i: 5e8), "vision")
    store.upsert_klines("SOLUSDT", raw_frame(start, 200, qv=lambda i: 2e8), "vision")
    store.upsert_klines("NEWUSDT", raw_frame(as_of - timedelta(days=29), 30, qv=lambda i: 9e8), "vision")
    store.upsert_klines("SMALLUSDT", raw_frame(start, 200, qv=lambda i: 1e5), "vision")
    info = {
        "symbols": [
            {
                "symbol": s,
                "baseAsset": s[:-4],
                "quoteAsset": "USDT",
                "status": "TRADING",
                "isSpotTradingAllowed": True,
            }
            for s in ["ETHUSDT", "SOLUSDT", "NEWUSDT", "SMALLUSDT"]
        ]
    }
    res = build_review(store, info, ASSETS, UniverseConfig(), as_of)
    assert res.stale_symbols == []
    pend = pending_review(res)
    assert list(pend["symbol"]) == ["SOLUSDT"]  # ETH 已批准；NEW 上市不足 90 天；SMALL 成交额不足
    draft = draft_assets(res, ASSETS)
    assert draft["approved_assets"] == ["ETH", "SOL"]
    md, dr = write_outputs(res, ASSETS, tmp_path / "out")
    assert "SOLUSDT" in md.read_text(encoding="utf-8")
    assert "approved_assets" in dr.read_text(encoding="utf-8")
