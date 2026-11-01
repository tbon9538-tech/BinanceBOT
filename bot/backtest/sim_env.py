"""离线模拟环境：合成行情 + 假行情源 + SimSpotAccount + 临时数据库。

供 run_once 端到端测试与故障演练脚本（scripts/drills/）共用，不访问网络。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from bot.app.run_once import RunDeps
from bot.backtest.sim_account import SimSpotAccount
from bot.config import AppConfig, AssetsConfig
from bot.data.klines import DAY_MS, KLINE_COLUMNS
from bot.data.store import MarketStore
from bot.domain.types import SymbolRules, SymbolStatus
from bot.notify.telegram import TelegramNotifier
from bot.persistence.repo import StateRepo

D = Decimal
ALTS = [f"A{i}" for i in range(1, 7)]


def _raw(start: date, closes: list[float], qv: float) -> pd.DataFrame:
    rows = []
    for i, c in enumerate(closes):
        d = start + timedelta(days=i)
        o = (d - date(1970, 1, 1)).days * DAY_MS
        px = f"{c:.8f}"
        rows.append(
            {
                "open_time_ms": o, "date": d.isoformat(), "open": px, "high": f"{c * 1.01:.8f}",
                "low": f"{c * 0.99:.8f}", "close": px, "volume": "100", "quote_volume": f"{qv:.2f}",
                "trades": 10, "close_time_ms": o + DAY_MS - 1,
            }
        )  # fmt: skip
    return pd.DataFrame(rows, columns=KLINE_COLUMNS)


def synthetic_market(last_day: date, n_days: int = 420, seed: int = 1) -> dict[str, pd.DataFrame]:
    """BTC 温和上涨 + 6 个漂移不同的山寨币。"""
    start = last_day - timedelta(days=n_days - 1)

    def walk(s: int, drift: float, vol: float) -> list[float]:
        rng = np.random.default_rng(s)
        return list(100 * np.cumprod(1 + drift + rng.normal(0, vol, n_days)))

    frames = {"BTCUSDT": _raw(start, walk(seed, 0.003, 0.02), 5e9)}
    for k, a in enumerate(ALTS):
        frames[f"{a}USDT"] = _raw(start, walk(seed + 10 + k, 0.004 - 0.0005 * k, 0.04), 1e8 * (k + 1))
    return frames


class FakeMarket:
    """MarketDataSource：从内存中的 raw frame 返回已收盘日线。可以模拟断网或数据延迟。"""

    def __init__(self, frames: Mapping[str, pd.DataFrame]) -> None:
        self.frames = dict(frames)
        self.withhold_last: set[str] = set()
        self.offline = False

    def get_exchange_info(self) -> Mapping[str, Any]:
        if self.offline:
            raise ConnectionError("模拟断网")
        return {
            "symbols": [
                {
                    "symbol": s,
                    "baseAsset": s[:-4],
                    "quoteAsset": "USDT",
                    "status": "TRADING",
                    "isSpotTradingAllowed": True,
                }
                for s in self.frames
            ]
        }

    def get_daily_klines(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        if self.offline:
            raise ConnectionError("模拟断网")
        df = self.frames[symbol]
        if symbol in self.withhold_last:
            end = end - timedelta(days=1)
        return df[(df["date"] >= start.isoformat()) & (df["date"] <= end.isoformat())].reset_index(drop=True)


def sim_rules(symbol: str, base: str, ref_price: Decimal | None = None) -> SymbolRules:
    """模拟用的交易规则。给出参考价时按价格设定 tickSize 与 stepSize（低价币如 PEPE 也能正常下单）。"""
    tick, step = D("0.0001"), D("0.00001")
    if ref_price is not None and ref_price > 0:
        mag = int(math.floor(math.log10(float(ref_price))))
        tick = D(10) ** (mag - 5)
        step = D(10) ** (int(math.floor(math.log10(10 / float(ref_price)))) - 3)
    return SymbolRules(
        symbol=symbol, base_asset=base, quote_asset="USDT", status=SymbolStatus.TRADING, tick_size=tick,
        min_price=tick, max_price=D("1000000"), step_size=step, min_qty=step,
        max_qty=D("900000000000"), min_notional=D("5"), max_notional=D("9000000"), bid_multiplier_up=D("5"),
        bid_multiplier_down=D("0.2"), ask_multiplier_up=D("5"), ask_multiplier_down=D("0.2"), avg_price_mins=5,
    )  # fmt: skip


def sim_account(frames: Mapping[str, pd.DataFrame], usdt: str = "1000") -> SimSpotAccount:
    last = {s: D(f["close"].iloc[-1]) for s, f in frames.items()}
    return SimSpotAccount(
        rules={s: sim_rules(s, s[:-4]) for s in frames},
        balances={"USDT": D(usdt)},
        books={s: (p * D("0.9995"), p * D("1.0005")) for s, p in last.items()},
        avg_prices=last,
        prices=last,
        account_id="spot-main",
    )


@dataclass
class SimClock:
    t: datetime
    sleeps: list[float] = field(default_factory=list)

    def now(self) -> datetime:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += timedelta(seconds=s)


def build_sim_deps(
    cfg: AppConfig, workdir: Path, run_date: date, *, usdt: str = "1000"
) -> tuple[RunDeps, SimSpotAccount, FakeMarket, SimClock]:
    frames = synthetic_market(run_date - timedelta(days=1))
    clock = SimClock(datetime(run_date.year, run_date.month, run_date.day, 0, 10, tzinfo=UTC))
    acct = sim_account(frames, usdt)
    market = FakeMarket(frames)
    workdir.mkdir(parents=True, exist_ok=True)
    deps = RunDeps(
        cfg=cfg,
        assets=AssetsConfig(approved_assets=["BTC", *ALTS]),
        account=acct,
        market=market,
        store=MarketStore(workdir / "market.db"),
        repo=StateRepo.open(workdir / "state.db", clock=clock.now),
        notifier=TelegramNotifier(token="", chat_id=""),
        kill_file=workdir / "KILL",
        lock_file=workdir / "run.lock",
        now=clock.now,
        sleep=clock.sleep,
        data_retry_sleep_sec=0.0,
    )
    return deps, acct, market, clock


def sim_config(base: Mapping[str, Any]) -> AppConfig:
    """在真实配置基础上放宽标的池门槛，适配合成行情。"""
    import copy

    raw = copy.deepcopy(dict(base))
    raw["universe"] = {**raw.get("universe", {}), "min_median_quote_volume": 0, "min_listing_days": 60}
    raw["data"] = {**raw.get("data", {}), "history_days": 400}
    return AppConfig.model_validate(raw)
