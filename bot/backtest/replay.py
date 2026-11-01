"""执行回放（spec §9.1，dev_plan M6-6）：用历史行情逐日运行**真实的** run_once 流程（SimSpotAccount），
与研究引擎在同一区间、同一资产集合下的净值对比，验证执行层与研究引擎一致。

- 每个运行日 D 的 00:10：盘口 = D 日开盘价 ± 5bp，参考价 = 开盘价；手续费 0.1%（与研究引擎单边成本 0.15% 对齐）；
- 行情源只返回 ≤ D−1 的已收盘日线（run_once 自己请求到 D−1，没有前视）；
- 每天收盘后按收盘价估值受管资产与 USDT，与研究引擎的每日 NAV 比较。
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from bot.app.run_once import DailyRunner, RunDeps, managed_assets
from bot.backtest.engine import EngineOptions, run_backtest
from bot.backtest.sim_account import SimSpotAccount
from bot.backtest.sim_env import FakeMarket, SimClock, sim_rules
from bot.config import AppConfig, AssetsConfig
from bot.data.store import MarketStore
from bot.notify.telegram import TelegramNotifier
from bot.persistence.repo import StateRepo
from bot.strategy.indicators import MarketPanel, column_symbol
from bot.strategy.momentum_v1 import MomentumV1

D = Decimal
HALF_SPREAD = D("0.0005")


@dataclass
class ReplayResult:
    replay_nav: pd.Series
    engine_nav: pd.Series
    max_rel_diff: float
    final_rel_diff: float
    orders: int


def run_replay(
    cfg_raw: Mapping[str, Any],
    store: MarketStore,
    assets: AssetsConfig,
    start: date,
    end: date,
    workdir: Path,
    *,
    initial_usdt: str = "1000",
) -> ReplayResult:
    raw_cfg = copy.deepcopy(dict(cfg_raw))
    raw_cfg["max_capital_usdt"] = 10**9  # 研究引擎没有资金上限；回放必须同样不设上限才可比
    cfg = AppConfig.model_validate(raw_cfg)
    symbols = sorted({f"{a}USDT" for a in assets.approved_assets} | {cfg.regime.symbol})
    raw = {s: store.read_raw(s, end_exclusive=(end + timedelta(days=1)).isoformat()) for s in symbols}
    raw = {s: f for s, f in raw.items() if not f.empty}
    allowed = {s for s in raw if s.removesuffix("USDT") in set(assets.approved_assets)}

    # ---- 研究引擎（同一资产集合）----
    panel = MarketPanel.from_frames(
        {s: store.read_symbol(s, end_exclusive=(end + timedelta(days=1)).isoformat()) for s in raw}
    )
    strat = MomentumV1(cfg, panel, asset_ok=lambda c: column_symbol(c) in allowed)
    # 研究引擎在 t 日收盘生成目标、t+1 日开盘成交；从 start−1 开始，才能与回放在 start 日开盘首次成交对齐
    eng = run_backtest(
        cfg,
        panel,
        start - timedelta(days=1),
        end,
        opts=EngineOptions.from_config(cfg, initial_capital=float(initial_usdt)),
        strategy=strat,
    )

    # ---- 执行回放 ----
    by_date = {s: f.set_index("date") for s, f in raw.items()}
    clock = SimClock(datetime(start.year, start.month, start.day, 0, 10, tzinfo=UTC))
    ref_px = {s: D(str(f["close"].iloc[-1])) for s, f in raw.items()}
    acct = SimSpotAccount(
        rules={s: sim_rules(s, s[:-4], ref_px[s]) for s in raw},
        balances={"USDT": D(initial_usdt)},
        books={},
        account_id=cfg.account_id,
        now=clock.now,  # 成交时间必须用回放时钟，否则余额核对会把历史成交算进"快照之后"
    )
    workdir.mkdir(parents=True, exist_ok=True)
    deps = RunDeps(
        cfg=cfg, assets=assets, account=acct, market=FakeMarket(raw), store=MarketStore(workdir / "market.db"),
        repo=StateRepo.open(workdir / "state.db", clock=clock.now), notifier=TelegramNotifier(token="", chat_id=""),
        kill_file=workdir / "KILL", lock_file=workdir / "run.lock", now=clock.now, sleep=clock.sleep, data_retry_sleep_sec=0.0,
    )  # fmt: skip
    navs: dict[pd.Timestamp, float] = {}
    day = start
    while day <= end:
        iso = day.isoformat()
        for s, f in by_date.items():
            if iso in f.index:
                o = D(str(f.at[iso, "open"]))
                acct.set_book(s, o * (1 - HALF_SPREAD), o * (1 + HALF_SPREAD))
                acct.set_avg_price(s, o)
                acct.prices[s] = o
        clock.t = datetime(day.year, day.month, day.day, 0, 10, tzinfo=UTC)
        DailyRunner(deps).run_once()
        bal = acct.get_balances()
        managed = managed_assets(deps.repo, cfg)
        nav = float(bal["USDT"].total) if "USDT" in bal else 0.0
        for a in managed:
            s = f"{a}USDT"
            if a in bal and s in by_date and iso in by_date[s].index:
                nav += float(bal[a].total) * float(by_date[s].at[iso, "close"])
        navs[pd.Timestamp(day)] = nav
        day += timedelta(days=1)
    rep = pd.Series(navs) / float(initial_usdt)
    engn = eng.nav.reindex(rep.index) / float(eng.nav.iloc[0])
    rel = (rep / engn - 1).abs()
    return ReplayResult(
        rep, engn, float(rel.max()), float(abs(rep.iloc[-1] / engn.iloc[-1] - 1)), len(acct.placed)
    )
