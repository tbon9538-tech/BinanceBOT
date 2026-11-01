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
DAEMON_WAITING_FRESH = timedelta(minutes=3)  # 常驻进程每 60 秒写一次存活标记
DAEMON_RUNNING_FRESH = timedelta(hours=3)  # 运行中不写标记；数据未就绪时最多重试约 30 分钟


@dataclass(frozen=True)
class DaemonStatus:
    """常驻进程的状态，来自它写的存活标记 daemon.alive（D-080）。"""

    alive: bool
    state: str | None  # waiting / running；没有标记时为 None
    seen: datetime | None
    next_run: datetime | None


def daemon_status(path: Path | None, now: datetime) -> DaemonStatus:
    if path is None or not path.exists():
        return DaemonStatus(False, None, None, None)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        seen = datetime.fromisoformat(raw["seen"])
        state = str(raw["state"])
        nxt = datetime.fromisoformat(raw["next_run"])
    except (OSError, ValueError, KeyError, TypeError):
        return DaemonStatus(False, None, None, None)
    fresh = DAEMON_RUNNING_FRESH if state == "running" else DAEMON_WAITING_FRESH
    return DaemonStatus(now - seen <= fresh, state, seen, nxt)


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
    daemon: DaemonStatus = DaemonStatus(False, None, None, None)

    @property
    def navu_change(self) -> float | None:
        """单位净值相对前一个快照日的变化。"""
        h = self.nav_history
        return h[-1].navu / h[-2].navu - 1 if len(h) >= 2 and h[-2].navu > 0 else None

    @property
    def max_drawdown(self) -> float | None:
        return min((p.drawdown for p in self.nav_history), default=None)

    @property
    def btc_vs_sma(self) -> float | None:
        r = self.regime
        return r.close / r.sma - 1 if r is not None and r.sma else None

    @property
    def capital(self) -> float | None:
        """策略资金 = min(净值, 资金上限)，与每日流程一致。"""
        return min(float(self.nav), float(self.max_capital)) if self.nav is not None else None

    @property
    def held_value(self) -> float | None:
        """持币市值（USDT）：快照里的仓位比例 × 净值。"""
        return (
            float(self.gross) * float(self.nav) if self.gross is not None and self.nav is not None else None
        )

    @property
    def exposure_of_capital(self) -> float | None:
        """持币市值占策略资金（与"持仓：目标 vs 实际"同一口径，D-084）。"""
        held, cap = self.held_value, self.capital
        return held / cap if held is not None and cap else None


@dataclass(frozen=True)
class Headline:
    """页首的一句话状态（D-084）：最需要处理的情况排在前面。"""

    tone: str  # good / warning / critical
    title: str
    sub: str


def headline(ov: Overview) -> Headline:
    if ov.state == "KILLED":
        return Headline("critical", "已紧急停止", "挂单已撤、受管资产已卖出；需要手动恢复交易")
    if ov.state == "HALTED":
        return Headline("critical", "已熔断", ov.state_reason or "风控停止了交易；确认原因后手动恢复")
    if ov.kill_file:
        return Headline("critical", "等待紧急停止", "KILL 文件存在，常驻进程检测到后会撤单并清仓")
    if ov.state == "PAUSED":
        return Headline("warning", "已暂停交易", "定时运行照常更新快照和报告，但不下单")
    if not ov.daemon.alive:
        return Headline("warning", "定时运行未进行", "常驻进程没有运行，到点不会自动运行")
    if ov.stale:
        return Headline("warning", "需要检查", "超过 26 小时没有新的运行，请检查常驻进程日志")
    if ov.daemon.state == "running":
        return Headline("good", "运行正常", "定时运行正在进行")
    nxt = ov.daemon.next_run
    return Headline("good", "运行正常", f"下次定时运行 {_bj(nxt)}（北京）" if nxt else "常驻进程运行中")


@dataclass(frozen=True)
class Check:
    label: str
    value: str
    level: str  # ok / off（未配置或已关闭）/ bad


def health_checks(ov: Overview, *, telegram: bool, heartbeat: bool) -> list[Check]:
    """页首右侧的检查项。图标与文字一起表达状态，不只靠颜色。"""
    d = ov.daemon
    if d.alive and d.seen is not None:
        doing = "运行中" if d.state == "running" else "等待中"
        daemon = Check("常驻进程", f"{doing} · 心跳 {_ago(ov.now - d.seen)}", "ok")
    else:
        daemon = Check("常驻进程", "未运行" + (f" · 最后心跳 {_bj(d.seen)}（北京）" if d.seen else ""), "bad")
    r = ov.last_run
    if r is None:
        run = Check("最近运行", "还没有运行", "bad")
    else:
        run = Check(
            "最近运行",
            f"{r.status} · {r.run_date}" + (" · 已超过 26 小时" if ov.stale else ""),
            "bad" if ov.stale or r.status == "FAILED" else "ok",
        )
    if ov.backup_enabled:
        backup = Check(
            "状态库备份",
            f"{len(ov.backups)} 份" if ov.backups else "还没有备份",
            "ok" if ov.backups else "off",
        )
    else:
        backup = Check("状态库备份", "已关闭", "off")
    return [
        daemon,
        run,
        backup,
        Check("Telegram 通知", "已配置" if telegram else "未配置", "ok" if telegram else "off"),
        Check("外部心跳", "已配置" if heartbeat else "未配置", "ok" if heartbeat else "off"),
    ]


def _bj(t: datetime) -> str:
    return (t + BEIJING).strftime("%m-%d %H:%M")


def _ago(dt: timedelta) -> str:
    s = max(0, int(dt.total_seconds()))
    if s < 60:
        return f"{s} 秒前"
    if s < 3600:
        return f"{s // 60} 分钟前"
    return f"{s // 3600} 小时前"


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
    alive_file: Path | None = None,
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
        daemon=daemon_status(alive_file, now),
    )


def event_summary(e: EventRow, limit: int = 160) -> str:
    """事件内容的单行摘要（页面展示用，由模板负责转义）。"""
    try:
        payload: Any = json.loads(e.payload_json)
    except ValueError:
        return e.payload_json[:limit]
    text = "；".join(f"{k}={v}" for k, v in payload.items()) if isinstance(payload, dict) else str(payload)
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(frozen=True)
class HoldingRow:
    asset: str
    qty: float
    value: float  # 市值（USDT）
    actual: float  # 占策略资金
    target: float  # 目标权重（占策略资金）


def compare_holdings(
    positions: list[tuple[str, float, float]],
    targets: dict[str, dict[str, float]],
    nav: float,
    max_capital: float,
    *,
    quote: str = "USDT",
) -> tuple[float, list[HoldingRow]]:
    """目标与实际持仓对比，都按"占策略资金"计算（策略资金 = min(净值, 资金上限)，与每日流程一致）。

    positions：(资产, 数量, 市值)。返回 (策略资金, 按 max(目标, 实际) 从大到小排列的行)。
    """
    capital = min(nav, max_capital)
    want: dict[str, float] = {}
    for sleeve in targets.values():
        for sym, w in sleeve.items():
            a = sym.removesuffix(quote)
            want[a] = want.get(a, 0.0) + w
    held = {a: (q, v) for a, q, v in positions}
    rows = [
        HoldingRow(
            a,
            held.get(a, (0.0, 0.0))[0],
            held.get(a, (0.0, 0.0))[1],
            held.get(a, (0.0, 0.0))[1] / capital if capital > 0 else 0.0,
            want.get(a, 0.0),
        )
        for a in set(want) | set(held)
    ]
    return capital, sorted(rows, key=lambda r: (-max(r.target, r.actual), r.asset))
