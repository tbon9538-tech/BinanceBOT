"""每日运行流程 `python -m bot run --once`（spec §6.2）与 kill 流程（spec §5.5）。

    1. 进程锁（data/run.lock）→ 2. KILL 文件 / 系统状态 → 3. 时钟、规则、余额
    4. 未决意图与持仓对账 → 5. 行情增量更新与数据检查 → 6. 计算大盘状态与目标（纯函数）
    7. PRE_TRADE 快照与风控判定 → 8~9. 先卖后买 → 10. 对账、POST_TRADE 快照、日报、心跳

所有外部依赖（账户、行情、仓库、通知、时钟、睡眠）都通过 RunDeps 注入，便于测试。
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
from filelock import FileLock, Timeout

from bot.app.account_view import StrategyAccountView, refresh_baselines
from bot.config import AppConfig, AssetsConfig, TrendSleeveConfig
from bot.data.store import MarketStore
from bot.data.sync import SyncReport, sync_rest_tail
from bot.domain.types import Balance, Env, SymbolRules, SystemState
from bot.exchange.base import MarketDataSource, SpotAccount
from bot.execution.planner import count_skips, plan_buys, plan_liquidation, plan_sells
from bot.execution.spot_executor import KILL_MAX_ATTEMPTS, ExecutionReport, SpotExecutor
from bot.notify import heartbeat
from bot.notify.telegram import TelegramNotifier
from bot.persistence.repo import StateRepo, TargetWeight
from bot.risk import guards, ledger
from bot.risk.guards import Action, GuardInput
from bot.strategy.indicators import MarketPanel, column_symbol
from bot.strategy.momentum_v1 import MomentumV1
from bot.strategy.smoothing import Overrides
from bot.strategy.universe import AssetClass, classify_asset

log = logging.getLogger(__name__)

QUOTE = "USDT"
KILL_FILE = Path("KILL")
LOCK_FILE = Path("data/run.lock")
DATA_RETRIES = 6
DATA_RETRY_SLEEP_SEC = 300.0
REVIEW_ALERT_DAYS = 7


@dataclass
class RunDeps:
    cfg: AppConfig
    assets: AssetsConfig
    account: SpotAccount
    market: MarketDataSource
    store: MarketStore
    repo: StateRepo
    notifier: TelegramNotifier
    kill_file: Path = KILL_FILE
    lock_file: Path = LOCK_FILE
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], None] = time.sleep
    data_retry_sleep_sec: float = DATA_RETRY_SLEEP_SEC
    heartbeat_transport: Any = None


@dataclass
class RunResult:
    status: str  # DONE / SKIPPED / FAILED / LOCKED / KILLED / HALTED / PAUSED / DRY_RUN
    run_id: str
    messages: list[str] = field(default_factory=list)
    sells: ExecutionReport | None = None
    buys: ExecutionReport | None = None


def _d(x: float) -> Decimal:
    return Decimal(str(round(x, 10)))


def managed_assets(repo: StateRepo, cfg: AppConfig) -> set[str]:
    return {p.asset for p in repo.list_positions(cfg.account_id, cfg.strategy_id, managed_only=True)}


def _prices_for(
    account: SpotAccount, assets: set[str], rules: Mapping[str, SymbolRules]
) -> dict[str, Decimal]:
    syms = [f"{a}{QUOTE}" for a in sorted(assets) if f"{a}{QUOTE}" in rules]
    raw = account.get_prices(syms) if syms else {}
    return {s.removesuffix(QUOTE): p for s, p in raw.items()}


def _books(
    account: SpotAccount, symbols: set[str], rules: Mapping[str, SymbolRules]
) -> tuple[dict[str, tuple[Decimal, Decimal]], dict[str, Decimal]]:
    books: dict[str, tuple[Decimal, Decimal]] = {}
    avg: dict[str, Decimal] = {}
    for s in sorted(symbols):
        r = rules.get(s)
        if r is None or not r.is_trading:
            continue
        try:
            books[s] = account.get_book_ticker(s)
            avg[s] = account.get_avg_price(s)
        except Exception as e:  # 单个交易对取不到报价：跳过它（planner 会记为无报价）
            log.warning("获取 %s 报价失败：%s", s, e)
    return books, avg


class DailyRunner:
    def __init__(self, deps: RunDeps) -> None:
        self.d = deps
        self.cfg = deps.cfg
        self.acct: SpotAccount = deps.account

    def _view(self) -> SpotAccount:
        """刷新外部基线，返回策略视角的账户（D-062）。"""
        managed = managed_assets(self.d.repo, self.cfg)
        base = refresh_baselines(
            self.d.repo, self.cfg.account_id, self.cfg.strategy_id, self.d.account.get_balances(), managed,
            now=self.d.now(),
        )  # fmt: skip
        return StrategyAccountView(self.d.account, base)

    # ---------------- 公共入口 ----------------

    def run_once(self, *, force_rebalance: bool = False, dry_run: bool | None = None) -> RunResult:
        dry = self.cfg.dry_run if dry_run is None else dry_run
        run_date = self.d.now().date()
        run_id = f"{self.cfg.strategy_id}:{run_date.isoformat()}"
        self.d.lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(self.d.lock_file))
        try:
            lock.acquire(timeout=0)
        except Timeout:
            log.warning("另一个运行正在进行（%s），本次退出", self.d.lock_file)
            return RunResult("LOCKED", run_id, ["进程锁被占用"])
        try:
            return self._run_locked(run_id, run_date, force_rebalance=force_rebalance, dry=dry)
        except Exception as e:
            log.exception("运行失败")
            with contextlib.suppress(Exception):
                self.d.repo.set_run_status(run_id, "FAILED", error=f"{type(e).__name__}: {e}")
            self._event("UNHANDLED_EXCEPTION", "CRITICAL", {"error": f"{type(e).__name__}: {e}"}, run_id)
            self.d.notifier.send(f"[{self.cfg.strategy_id}] 运行失败：{type(e).__name__}: {e}", "CRITICAL")
            return RunResult("FAILED", run_id, [f"{type(e).__name__}: {e}"])
        finally:
            lock.release()

    def kill(self, *, reason: str = "kill 命令") -> RunResult:
        """紧急停止（spec §5.5）：撤单、清仓受管资产、进入 KILLED。调用方负责拿锁。"""
        run_date = self.d.now().date()
        run_id = f"{self.cfg.strategy_id}:{run_date.isoformat()}:kill"
        repo = self.d.repo
        repo.start_run(
            run_id, strategy_id=self.cfg.strategy_id, account_id=self.cfg.account_id, run_date=run_date
        )
        self.acct = self._view()
        rules = self.acct.get_all_rules()
        managed = managed_assets(repo, self.cfg)
        ex = self._executor(run_id, run_date, abort=lambda: False)
        ex.cancel_strategy_open_orders(sorted(f"{a}{QUOTE}" for a in managed if f"{a}{QUOTE}" in rules))
        report = self._liquidate(ex, rules, managed)
        repo.set_system_state(self.cfg.strategy_id, SystemState.KILLED.value, reason=reason)
        repo.set_run_status(run_id, "DONE")
        self._event("KILLED", "CRITICAL", {"reason": reason}, run_id)
        left = {a: str(b.total) for a, b in self.acct.get_balances().items() if a in managed and b.total > 0}
        self.d.notifier.send(
            f"[{self.cfg.strategy_id}] 已紧急停止（{reason}）。剩余受管资产：{left or '无'}", "CRITICAL"
        )
        if self.d.kill_file.exists():
            self.d.kill_file.unlink()
        return RunResult("KILLED", run_id, [reason], sells=report)

    # ---------------- 主流程 ----------------

    def _run_locked(self, run_id: str, run_date: date, *, force_rebalance: bool, dry: bool) -> RunResult:
        cfg, repo, acct = self.cfg, self.d.repo, self.d.account
        msgs: list[str] = []
        repo.ensure_account(cfg.account_id, env=cfg.env.value)
        prior = repo.get_run(run_id)
        already_done = prior is not None and prior.status == "DONE"
        repo.start_run(run_id, strategy_id=cfg.strategy_id, account_id=cfg.account_id, run_date=run_date)

        # 2) KILL 文件
        if self.d.kill_file.exists():
            res = self.kill(reason="检测到 KILL 文件")
            repo.set_run_status(run_id, "DONE")
            return res
        state_row = repo.get_system_state(cfg.strategy_id)
        system_state = SystemState(state_row.state) if state_row else SystemState.RUNNING
        if state_row is None:
            repo.set_system_state(cfg.strategy_id, SystemState.RUNNING.value, reason="首次运行")

        # 3) 时钟、规则、余额
        for hook in ("sync_time", "load_exchange_info"):
            fn = getattr(acct, hook, None)
            if callable(fn):
                fn()
        acct = self.acct = self._view()
        rules = acct.get_all_rules()
        balances = acct.get_balances()

        # 4) 对账
        ex = self._executor(run_id, run_date, abort=self.d.kill_file.exists)
        pend = ex.reconcile_pending_intents(balances)
        managed = managed_assets(repo, cfg)
        ex.cancel_strategy_open_orders(sorted(f"{a}{QUOTE}" for a in managed if f"{a}{QUOTE}" in rules))
        balances = acct.get_balances()
        prices = _prices_for(acct, managed, rules)
        ex.reconcile_positions(balances, prices=prices)
        if pend.resolved or pend.unresolved:
            msgs.append(f"上次遗留意图：已确认 {len(pend.resolved)} 个，仍未确认 {len(pend.unresolved)} 个")

        # 5) 行情
        t = run_date - timedelta(days=1)
        data_ok = self._update_market_data(t)
        if not data_ok:
            self._event("DATA_NOT_READY", "WARN", {"t": t.isoformat()}, run_id)
            self.d.notifier.send(f"[{cfg.strategy_id}] {t} 的 BTC 日线仍不可用，本次不交易", "WARN")

        # 6) 目标（纯函数）
        tb = None
        weights: dict[str, float] = {}
        if data_ok:
            tb, weights = self._targets(t, rules, balances, run_id)

        # 7) NAV、快照与风控
        nav = ledger.compute_nav(balances, prices, managed)
        units, navu = self._units(nav.nav)
        prev = repo.latest_nav_snapshot(cfg.strategy_id, cfg.account_id, kind="PRE_TRADE")
        peak = max(repo.peak_nav_per_unit(cfg.strategy_id, cfg.account_id) or navu, navu)
        repo.append_nav_snapshot(
            strategy_id=cfg.strategy_id, account_id=cfg.account_id, kind="PRE_TRADE", nav_usdt=nav.nav,
            units=units, nav_per_unit=navu, gross_exposure=nav.gross, run_id=run_id,
            details={"balances": {a: str(b.total) for a, b in balances.items()}, "unpriced": list(nav.unpriced)},
        )  # fmt: skip
        g = guards.evaluate(
            GuardInput(
                kill_requested=self.d.kill_file.exists(),
                system_state=system_state,
                data_ok=data_ok,
                unexplained=self._unexplained(balances, prices),
                navu=navu,
                navu_peak=peak,
                navu_prev=prev.nav_per_unit if prev else None,
                hours_since_prev=(self.d.now() - prev.time).total_seconds() / 3600 if prev else None,
                price_deviation=self._price_deviation(prices),
                balances_ok=QUOTE in balances and not nav.unpriced,
                max_drawdown_halt=cfg.risk.max_drawdown_halt,
                daily_anomaly_limit=cfg.risk.daily_anomaly_limit,
                data_sanity_max_deviation=cfg.risk.data_sanity_max_deviation,
            )
        )
        for ev in g.events:
            self._event(
                ev,
                "CRITICAL" if ev in ("DATA_ANOMALY", "DRAWDOWN_HALT", "KILLED") else "WARN",
                {"reasons": g.reasons},
                run_id,
            )
        msgs += g.reasons
        if g.action is Action.KILL:
            res = self.kill(reason="检测到 KILL 文件")
            repo.set_run_status(run_id, "DONE")
            return res
        if g.action is Action.HALT:
            self._liquidate(ex, rules, managed)
            repo.set_system_state(cfg.strategy_id, SystemState.HALTED.value, reason="; ".join(g.reasons))
            self.d.notifier.send(
                f"[{cfg.strategy_id}] 回撤熔断，已清仓并进入 HALTED：{g.reasons}", "CRITICAL"
            )
            repo.set_run_status(run_id, "DONE")
            return RunResult("HALTED", run_id, msgs)
        if g.action is Action.PAUSE:
            repo.set_system_state(cfg.strategy_id, SystemState.PAUSED.value, reason="; ".join(g.reasons))
            self.d.notifier.send(f"[{cfg.strategy_id}] 数据异常，已暂停（未清仓）：{g.reasons}", "CRITICAL")
            repo.set_run_status(run_id, "SKIPPED")
            return RunResult("PAUSED", run_id, msgs)

        sells = buys = None
        trading = g.allow_trading and tb is not None and not (already_done and not force_rebalance)
        if already_done and not force_rebalance:
            msgs.append("今天已完成调仓，只更新快照")
        if trading:
            capital = min(nav.nav, Decimal(cfg.max_capital_usdt))
            sells, buys, plan_msgs = self._trade(ex, weights, capital, rules, balances, managed, run_id,
                                                 allow_buys=g.allow_buys, force=force_rebalance, dry=dry)  # fmt: skip
            msgs += plan_msgs

        # 10) 对账、快照、日报
        balances = acct.get_balances()
        managed = managed_assets(repo, cfg)
        prices = _prices_for(acct, managed, rules)
        if trading and not dry:
            ex.reconcile_positions(balances, prices=prices)
        post = ledger.compute_nav(balances, prices, managed)
        repo.append_nav_snapshot(
            strategy_id=cfg.strategy_id, account_id=cfg.account_id, kind="POST_TRADE", nav_usdt=post.nav,
            units=units, nav_per_unit=post.nav / units, gross_exposure=post.gross, run_id=run_id,
            details={"balances": {a: str(b.total) for a, b in balances.items()}},
        )  # fmt: skip
        status = "DRY_RUN" if dry and trading else ("DONE" if (trading or already_done) else "SKIPPED")
        repo.set_run_status(run_id, "DONE" if status in ("DONE", "DRY_RUN") else "SKIPPED", signal_date=t)
        self._daily_report(t, tb, post, units, peak, sells, buys, balances, managed, msgs)
        if status == "DONE":
            heartbeat.ping(cfg.notify.heartbeat_url, transport=self.d.heartbeat_transport)
        return RunResult(status, run_id, msgs, sells=sells, buys=buys)

    # ---------------- 步骤 ----------------

    def _executor(self, run_id: str, run_date: date, *, abort: Callable[[], bool]) -> SpotExecutor:
        return SpotExecutor(
            self.acct, self.d.repo, self.cfg.execution, strategy_id=self.cfg.strategy_id,
            account_id=self.cfg.account_id, run_id=run_id, run_date=run_date, should_abort=abort,
            sleep=self.d.sleep, clock=self.d.now,
        )  # fmt: skip

    def _needed_symbols(self, tradable_live: set[str]) -> set[str]:
        cfg = self.cfg
        excluded = self.d.assets.excluded_set()
        need = {cfg.regime.symbol}
        for s in cfg.enabled_sleeves().values():
            if isinstance(s, TrendSleeveConfig):
                need.update(s.symbols)
        for sym in tradable_live:
            if sym.removesuffix(QUOTE) not in excluded:
                need.add(sym)
        return need

    def _update_market_data(self, t: date) -> bool:
        """REST 增量补齐到 t；BTC 的 t 日日线不可用时等待重试（spec §3.2）。"""
        from bot.strategy.universe import tradable_usdt_symbols

        info = self.d.market.get_exchange_info()
        need = self._needed_symbols(set(tradable_usdt_symbols(info)))
        since = t - timedelta(days=self.cfg.data.history_days)
        report = SyncReport()
        for attempt in range(DATA_RETRIES + 1):
            sync_rest_tail(
                self.d.store, self.d.market, sorted(need), since=since, last_closed=t, report=report
            )
            if self.d.store.last_date(self.cfg.regime.symbol) == t.isoformat():
                return True
            if attempt < DATA_RETRIES:
                log.warning(
                    "BTC %s 日线尚不可用，%.0f 秒后重试（%d/%d）",
                    t,
                    self.d.data_retry_sleep_sec,
                    attempt + 1,
                    DATA_RETRIES,
                )
                self.d.sleep(self.d.data_retry_sleep_sec)
        return False

    def _allowed_symbols(self, rules: Mapping[str, SymbolRules]) -> set[str]:
        out = set()
        for sym, r in rules.items():
            if r.quote_asset != QUOTE or not r.is_trading:
                continue
            if classify_asset(r.base_asset, self.d.assets) is AssetClass.APPROVED:
                out.add(sym)
        return out

    def _targets(
        self, t: date, rules: Mapping[str, SymbolRules], balances: Mapping[str, Balance], run_id: str
    ) -> tuple[Any, dict[str, float]]:
        cfg, store = self.cfg, self.d.store
        if cfg.risk.trailing_stop.enabled:
            raise RuntimeError(
                "v1 实盘流程未接入 ATR 止损（E6 结论为关闭）；请在 config.yaml 中关闭 risk.trailing_stop"
            )
        allowed = self._allowed_symbols(rules)
        core = {
            s
            for sl in cfg.enabled_sleeves().values()
            if isinstance(sl, TrendSleeveConfig)
            for s in sl.symbols
        }
        start = (t - timedelta(days=cfg.data.history_days)).isoformat()
        end_excl = (t + timedelta(days=1)).isoformat()
        frames = {}
        for sym in sorted(allowed | core | {cfg.regime.symbol}):
            df = store.read_symbol(sym, start, end_excl)
            if not df.empty:
                frames[sym] = df
        panel = MarketPanel.from_frames(frames, gap_days=cfg.universe.segment_gap_days)
        ts = pd.Timestamp(t)
        force_exit = {f"{a}{QUOTE}" for a in cfg.risk.force_exit_assets}
        managed = managed_assets(self.d.repo, cfg)
        frozen = {
            f"{a}{QUOTE}" for a in managed if (r := rules.get(f"{a}{QUOTE}")) is not None and not r.is_trading
        }
        for sym in sorted(frozen):
            self._event("HELD_SYMBOL_NOT_TRADING", "CRITICAL", {"symbol": sym}, run_id)
        tradable = {s for s, r in rules.items() if r.is_trading}
        strat = MomentumV1(cfg, panel, asset_ok=lambda c: column_symbol(c) in allowed)
        tb = strat.target(ts, Overrides(always_excluded=frozenset(force_exit | frozen)))
        weights = {column_symbol(c): w for c, w in tb.weights.items() if column_symbol(c) in tradable}
        reg = {str(k): v for k, v in strat.regime.loc[ts].to_dict().items()}
        self.d.repo.upsert_regime_state(
            cfg.strategy_id, t, symbol=cfg.regime.symbol, close=_d(float(reg["close"])),
            sma=_d(float(reg["sma"])) if pd.notna(reg["sma"]) else None, state=str(reg["state"]),
            switched=bool(reg["switched_on"] or reg["switched_off"]),
        )  # fmt: skip
        rows = [TargetWeight(sl, s, "D", _d(w)) for sl, m in tb.raw.items() for s, w in m.items()]
        rows += [TargetWeight(sl, s, "S", _d(w)) for sl, m in tb.smoothed.items() for s, w in m.items()]
        rows += [TargetWeight("all", s, "W", _d(w)) for s, w in weights.items()]
        self.d.repo.write_daily_targets(cfg.strategy_id, t, rows)
        if tb.switched_on or tb.switched_off:
            self._event("REGIME_SWITCH", "WARN", {"state": tb.regime.value}, run_id)
            self.d.notifier.send(f"[{cfg.strategy_id}] 大盘状态切换为 {tb.regime.value}（{t}）", "WARN")
        self._review_alert(panel, ts, rules, run_id)
        return tb, weights

    def _review_alert(
        self, panel: MarketPanel, t: pd.Timestamp, rules: Mapping[str, SymbolRules], run_id: str
    ) -> None:
        """通过流动性筛选的待审核资产（spec §2.2 第 2 条）：同一批资产 7 天内只提醒一次。"""
        last = self.d.repo.list_events(self.cfg.strategy_id, type_="ASSET_REVIEW_NEEDED")
        if last and self.d.now() - last[-1].time < timedelta(days=REVIEW_ALERT_DAYS):
            return
        pending = sorted(
            r.base_asset for s, r in rules.items()
            if r.quote_asset == QUOTE and r.is_trading and classify_asset(r.base_asset, self.d.assets) is AssetClass.PENDING
            and s in panel.columns
        )  # fmt: skip
        if pending:
            self._event("ASSET_REVIEW_NEEDED", "INFO", {"assets": pending[:50]}, run_id)
            self.d.notifier.send(
                f"[{self.cfg.strategy_id}] 有 {len(pending)} 个资产待审核，请运行 `python -m bot assets review`",
                "INFO",
            )

    def _units(self, nav: Decimal) -> tuple[Decimal, Decimal]:
        repo, cfg = self.d.repo, self.cfg
        last = repo.latest_nav_snapshot(cfg.strategy_id, cfg.account_id)
        if last is None:
            units = ledger.initial_units(nav) if nav > 0 else Decimal(1)
        else:
            units = last.units
            for f in repo.list_capital_flows(cfg.strategy_id, cfg.account_id):
                if f.time > last.time:
                    units += f.units_delta
        return units, (nav / units if units > 0 else Decimal(1))

    def _unexplained(
        self, balances: Mapping[str, Balance], prices: Mapping[str, Decimal]
    ) -> dict[str, Decimal]:
        """与上一次 POST_TRADE / ACK 快照相比，无法由本系统成交解释的余额变化（spec §5.2）。"""
        repo, cfg = self.d.repo, self.cfg
        snaps = [
            s
            for s in repo.list_nav_snapshots(cfg.strategy_id, cfg.account_id)
            if s.kind in ("POST_TRADE", "ACK")
        ]
        if not snaps:
            return {}
        base = snaps[-1]
        prev = {a: Decimal(v) for a, v in json.loads(base.details_json).get("balances", {}).items()}
        managed = managed_assets(repo, cfg) | {QUOTE}
        cur = {a: b.total for a, b in balances.items() if a in managed}
        prev = {a: v for a, v in prev.items() if a in managed}
        explained = {
            a: repo.net_asset_change(
                a, strategy_id=cfg.strategy_id, account_id=cfg.account_id, recorded_after=base.time
            )
            for a in managed
        }
        return ledger.unexplained_changes(prev, cur, explained, prices)

    def _price_deviation(self, prices: Mapping[str, Decimal]) -> dict[str, float]:
        """执行环境价格与 live 公共行情的偏差；testnet 价格独立于 live，不检查（spec §5.4）。"""
        if self.cfg.env is Env.TESTNET or not prices:
            return {}
        out: dict[str, float] = {}
        for asset, px in prices.items():
            last = self.d.store.read_raw(f"{asset}{QUOTE}")
            if last.empty:
                continue
            live = Decimal(str(last["close"].iloc[-1]))
            if live > 0:
                out[asset] = float(abs(px / live - 1))
        return out

    def _trade(
        self,
        ex: SpotExecutor,
        weights: Mapping[str, float],
        capital: Decimal,
        rules: Mapping[str, SymbolRules],
        balances: Mapping[str, Balance],
        managed: set[str],
        run_id: str,
        *,
        allow_buys: bool,
        force: bool,
        dry: bool,
    ) -> tuple[ExecutionReport | None, ExecutionReport | None, list[str]]:
        cfg, acct = self.cfg, self.acct
        msgs: list[str] = []
        held = {f"{a}{QUOTE}" for a in managed}
        books, avg = _books(acct, set(weights) | held, rules)
        sp = plan_sells(
            weights, capital, balances, rules, books, avg, cfg.rebalance, cfg.execution,
            managed_assets=managed, ignore_band=force, blocked_symbols=ex.blocked_symbols,
        )  # fmt: skip
        msgs.append(
            f"卖单 {len(sp.sells)} 笔，买入候选 {len(sp.buy_candidates)} 个，跳过 {count_skips(sp.skipped)}"
        )
        if dry:
            for o in sp.sells:
                msgs.append(f"[dry-run] SELL {o.symbol} {o.quantity} @ {o.price}")
            for c in sp.buy_candidates:
                msgs.append(f"[dry-run] BUY 候选 {c.symbol} 约 {c.delta_usdt:.2f} USDT")
            return None, None, msgs
        sells = ex.execute(sp.sells, balances=balances) if sp.sells else None
        if sells is not None and sells.aborted:
            msgs.append("下单过程中检测到 KILL，停止")
            return sells, None, msgs
        if not allow_buys:
            msgs.append("风控：本次禁止买入")
            return sells, None, msgs
        balances2 = acct.get_balances()
        books2, avg2 = _books(acct, {c.symbol for c in sp.buy_candidates}, rules)
        usdt_free = balances2[QUOTE].free if QUOTE in balances2 else Decimal(0)
        bp = plan_buys(
            sp.buy_candidates, usdt_free, rules, books2, avg2, cfg.rebalance, cfg.execution,
            blocked_symbols=ex.blocked_symbols,
        )  # fmt: skip
        msgs.append(f"买单 {len(bp.buys)} 笔（可用 {bp.available_usdt:.2f} USDT，缩放 {bp.scale:.3f}）")
        buys = ex.execute(bp.buys, balances=balances2) if bp.buys else None
        return sells, buys, msgs

    def _liquidate(
        self, ex: SpotExecutor, rules: Mapping[str, SymbolRules], managed: set[str]
    ) -> ExecutionReport | None:
        balances = self.acct.get_balances()
        held = {f"{a}{QUOTE}" for a in managed}
        books, avg = _books(self.acct, held, rules)
        plan = plan_liquidation(
            balances, rules, books, avg, self.cfg.rebalance, self.cfg.execution, managed_assets=managed
        )
        if not plan.sells:
            return None
        return ex.execute(plan.sells, max_attempts=KILL_MAX_ATTEMPTS, balances=balances)

    def _event(self, type_: str, level: str, payload: Mapping[str, Any], run_id: str | None) -> None:
        self.d.repo.append_event(
            type_,
            level,
            dict(payload),
            strategy_id=self.cfg.strategy_id,
            account_id=self.cfg.account_id,
            run_id=run_id,
        )

    def _daily_report(
        self, t: date, tb: Any, nav: ledger.NavBreakdown, units: Decimal, peak: Decimal,
        sells: ExecutionReport | None, buys: ExecutionReport | None, balances: Mapping[str, Balance],
        managed: set[str], msgs: list[str],
    ) -> None:  # fmt: skip
        if not self.cfg.notify.daily_report:
            return
        navu = nav.nav / units if units > 0 else Decimal(0)
        dd = navu / peak - 1 if peak > 0 else Decimal(0)
        lines = [
            f"[{self.cfg.strategy_id} · {self.cfg.env.value}] 日报 {t}",
            f"单位净值 {navu:.4f}，净值 {nav.nav:.2f} USDT，回撤 {dd:.1%}，仓位 {nav.gross:.0%}",
            f"大盘：{tb.regime.value if tb else '未知'}",
        ]
        if nav.positions:
            lines.append(
                "持仓："
                + "，".join(f"{a} {v:.1f}" for a, v in sorted(nav.positions.items(), key=lambda x: -x[1]))
            )
        n_fills = sum(len(o.fills) for r in (sells, buys) if r for o in r.outcomes)
        lines.append(f"成交 {n_fills} 笔")
        unmanaged = sorted(a for a, b in balances.items() if a not in managed and a != QUOTE and b.total > 0)
        if unmanaged:
            lines.append("非受管资产：" + "，".join(unmanaged[:15]))
        lines += [m for m in msgs if m][:8]
        self.d.notifier.send("\n".join(lines), "INFO")
