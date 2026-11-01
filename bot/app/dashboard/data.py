"""管理界面的数据：只读地从状态库和行情库整理出页面要显示的内容（D-075）。

这里不访问交易所；实时余额由 server 在需要时另外查询。
大盘图用行情库中的日线按当前配置重新计算（与策略调用同一个函数），只用于展示，不参与任何决策。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from bot import paths
from bot.app.daemon import next_run_at
from bot.config import AppConfig
from bot.data.store import MarketStore
from bot.domain.types import Regime
from bot.persistence.backup import list_backups
from bot.persistence.models import EventRow, FillRow, RunRow
from bot.persistence.repo import StateRepo
from bot.strategy.regime import regime_with_exposure

BEIJING = timedelta(hours=8)
REGIME_DAYS = 365
STALE_AFTER = timedelta(hours=26)  # 超过这么久没有新的运行，页面上提示可能漏跑


@dataclass(frozen=True)
class NavPoint:
    day: date
    navu: float
    drawdown: float


@dataclass(frozen=True)
class RegimePoint:
    day: date
    close: float
    sma: float | None
    risk_on: bool


@dataclass(frozen=True)
class Overview:
    env: str
    strategy_id: str
    account_id: str
    now: datetime
    state: str
    state_since: datetime | None
    state_reason: str | None
    kill_file: bool
    last_run: RunRow | None
    stale: bool
    next_run: datetime
    nav: Decimal | None
    navu: Decimal | None
    gross: Decimal | None
    snapshot_time: datetime | None
    drawdown: float | None
    max_capital: Decimal
    regime: RegimePoint | None
    nav_history: list[NavPoint]
    regime_history: list[RegimePoint]
    targets: dict[str, dict[str, float]]  # 袖子 → 交易对 → 占策略资金的权重（W）
    targets_date: date | None
    positions: list[tuple[str, Decimal, date | None, bool, bool]]  # 资产、数量、建仓日、灰尘、冻结
    runs: list[RunRow]
    events: list[EventRow]
    fills: list[FillRow]
    backups: list[Path] = field(default_factory=list)
    backup_enabled: bool = True


def nav_history(repo: StateRepo, cfg: AppConfig) -> list[NavPoint]:
    """每天最后一个 POST_TRADE 快照的单位净值（没有 POST_TRADE 的日子用当天最后一个快照）与回撤。"""
    by_day: dict[date, tuple[int, float]] = {}
    for s in repo.list_nav_snapshots(cfg.strategy_id, cfg.account_id):
        if s.kind not in ("PRE_TRADE", "POST_TRADE"):
            continue
        rank = 1 if s.kind == "POST_TRADE" else 0
        d = s.time.date()
        if d not in by_day or rank >= by_day[d][0]:
            by_day[d] = (rank, float(s.nav_per_unit))
    out: list[NavPoint] = []
    peak = 0.0
    for d in sorted(by_day):
        v = by_day[d][1]
        peak = max(peak, v)
        out.append(NavPoint(d, v, v / peak - 1 if peak > 0 else 0.0))
    return out


def regime_from_market(store: MarketStore, cfg: AppConfig, *, days: int = REGIME_DAYS) -> list[RegimePoint]:
    """用行情库中大盘标的的日线，按当前配置计算最近 days 天的大盘状态（前面多读一段用于预热）。"""
    rc = cfg.regime
    df = store.read_symbol(rc.symbol)
    if df.empty:
        return []
    warmup = 3 * max(rc.sma_period, rc.tsmom_lookback, *rc.ensemble_periods)
    close = df["close"].iloc[-(days + warmup) :]
    reg = regime_with_exposure(
        close, rc.sma_period, rc.buffer, mode=rc.mode, tsmom_lookback=rc.tsmom_lookback,
        ensemble_periods=tuple(rc.ensemble_periods),
    )  # fmt: skip
    sma = close.rolling(rc.sma_period).mean()  # 展示用：ensemble / tsmom 模式下也画出主均线
    tail = reg.iloc[-days:]
    cols = zip(
        pd.DatetimeIndex(tail.index),
        tail["close"].to_numpy(),
        sma.reindex(tail.index).to_numpy(),
        tail["state"].to_numpy(),
        strict=True,
    )
    return [
        RegimePoint(d.date(), float(c), None if pd.isna(m) else float(m), s == Regime.RISK_ON.value)
        for d, c, m, s in cols
    ]


def regime_history(repo: StateRepo, cfg: AppConfig, *, days: int = REGIME_DAYS) -> list[RegimePoint]:
    """运行时写入状态库的大盘状态（行情库不可用时的后备）。"""
    rows = repo.list_regime_states(cfg.strategy_id)[-days:]
    return [
        RegimePoint(r.date, float(r.close), float(r.sma) if r.sma is not None else None, r.state == "RISK_ON")
        for r in rows
    ]


def build_overview(
    cfg: AppConfig,
    repo: StateRepo,
    kill_file: Path,
    *,
    store: MarketStore | None = None,
    now: datetime | None = None,
) -> Overview:
    now = now or datetime.now(UTC)
    sid = cfg.strategy_id
    st = repo.get_system_state(sid)
    last = repo.last_run(sid)
    snap = repo.latest_nav_snapshot(sid, cfg.account_id)
    navs = nav_history(repo, cfg)
    regimes = (regime_from_market(store, cfg) if store is not None else []) or regime_history(repo, cfg)
    tdate = repo.latest_target_date(sid)
    targets: dict[str, dict[str, float]] = {}
    if tdate is not None:
        for t in repo.get_daily_targets(sid, tdate, kind="W"):
            if t.weight > 0:
                targets.setdefault(t.sleeve, {})[t.symbol] = float(t.weight)
    positions = [
        (p.asset, p.qty, p.entry_date, p.dust, p.frozen)
        for p in repo.list_positions(cfg.account_id, sid, managed_only=True)
    ]
    finished = last.finished_at or last.started_at if last else None
    backups = list_backups(cfg.backup.dir, paths.state_db(cfg.env.value).stem) if cfg.backup.enabled else []
    return Overview(
        env=cfg.env.value,
        strategy_id=sid,
        account_id=cfg.account_id,
        now=now,
        state=st.state if st else "RUNNING",
        state_since=st.since if st else None,
        state_reason=st.reason if st else None,
        kill_file=kill_file.exists(),
        last_run=last,
        stale=finished is None or now - finished > STALE_AFTER,
        next_run=next_run_at(now, cfg.rebalance.run_time_utc),
        nav=snap.nav_usdt if snap else None,
        navu=snap.nav_per_unit if snap else None,
        gross=snap.gross_exposure if snap else None,
        snapshot_time=snap.time if snap else None,
        drawdown=navs[-1].drawdown if navs else None,
        max_capital=Decimal(cfg.max_capital_usdt),
        regime=regimes[-1] if regimes else None,
        nav_history=navs,
        regime_history=regimes,
        targets=targets,
        targets_date=tdate,
        positions=positions,
        runs=repo.list_runs(sid, limit=30),
        events=list(reversed(repo.list_events(sid)))[:50],
        fills=sorted(repo.list_fills(sid, cfg.account_id), key=lambda f: f.time, reverse=True)[:30],
        backups=backups,
        backup_enabled=cfg.backup.enabled,
    )


def event_summary(e: EventRow, limit: int = 160) -> str:
    """事件内容的单行摘要（页面展示用，由模板负责转义）。"""
    try:
        payload: Any = json.loads(e.payload_json)
    except ValueError:
        return e.payload_json[:limit]
    text = "；".join(f"{k}={v}" for k, v in payload.items()) if isinstance(payload, dict) else str(payload)
    return text if len(text) <= limit else text[: limit - 1] + "…"
