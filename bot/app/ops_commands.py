"""运维子命令：run / status / kill / pause / resume / capital / adopt（M5–M6）。"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv
from filelock import FileLock, Timeout

from bot import paths
from bot.app.daemon import run_daemon
from bot.app.run_once import KILL_FILE, LOCK_FILE, QUOTE, DailyRunner, RunDeps, _prices_for, managed_assets
from bot.config import AppConfig, load_assets
from bot.data.store import MarketStore
from bot.domain.errors import ConfigError
from bot.domain.types import Env, SystemState
from bot.exchange.binance.client import BinanceHttpClient, RateLimitState
from bot.exchange.binance.credentials import credentials_present, load_credentials
from bot.exchange.binance.market_data import BinanceMarketData
from bot.exchange.binance.spot_account import BinanceSpotAccount
from bot.logging_setup import setup_logging
from bot.notify.telegram import TelegramNotifier
from bot.persistence.repo import StateRepo
from bot.risk import ledger

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 2
EXIT_LIVE_REFUSED = 7
KILL_LOCK_WAIT_SEC = 120.0
LIVE_CONFIRM_TOKEN = "I_UNDERSTAND"


class LiveRefused(Exception):
    pass


def check_live_allowed(cfg: AppConfig, args: argparse.Namespace) -> bool:
    """spec §13：env=live 时必须同时满足 --live-confirm=I_UNDERSTAND、控制台 YES（或 --yes）、显式 max_capital_usdt。"""
    if cfg.env is not Env.LIVE:
        return False
    if getattr(args, "live_confirm", None) != LIVE_CONFIRM_TOKEN:
        raise LiveRefused(f"env=live 必须加 --live-confirm={LIVE_CONFIRM_TOKEN}（spec §13）")
    if not getattr(args, "yes", False):
        ans = input(f"即将以 live 环境运行，策略资金上限 {cfg.max_capital_usdt} USDT。输入 YES 继续：")
        if ans.strip() != "YES":
            raise LiveRefused("未确认，退出")
    return True


def check_withdrawal_disabled(account: BinanceSpotAccount) -> None:
    """spec §13：live Key 不得开启提现。字段缺失或为 true 时一律拒绝启动（D-061）。"""
    info = account.client.signed_get("/sapi/v1/account/apiRestrictions")
    if not isinstance(info, dict) or "enableWithdrawals" not in info:
        raise LiveRefused("无法确认 API Key 的提现权限（apiRestrictions 返回格式不符），拒绝启动")
    if info["enableWithdrawals"] is not False:
        raise LiveRefused("API Key 开启了提现权限，拒绝启动。请在币安后台关闭提现权限并绑定 IP 白名单")


def build_deps(cfg: AppConfig, *, allow_live: bool = False) -> RunDeps:
    load_dotenv(Path(".env"), override=False)
    setup_logging(cfg.logging.level, Path(cfg.logging.dir), cfg.logging.retention_days)
    if not credentials_present():
        raise ConfigError("缺少 API Key：请在 .env 中设置 BINANCE_API_KEY 等（见 .env.example）")
    creds = load_credentials()
    shared = RateLimitState() if cfg.env is Env.LIVE else None  # live 时行情与交易共用同一 IP 额度
    account = BinanceSpotAccount(
        cfg.env,
        creds,
        account_id=cfg.account_id,
        allow_live=allow_live,
        recv_window_ms=cfg.execution.recv_window_ms,
        timeout_sec=cfg.execution.http_timeout_sec,
        rate_state=shared,
    )
    if cfg.env is Env.LIVE:
        check_withdrawal_disabled(account)
    market = BinanceMarketData(
        BinanceHttpClient(
            cfg.data.market_data_rest, timeout_sec=cfg.execution.http_timeout_sec, rate_state=shared
        )
    )
    return RunDeps(
        cfg=cfg,
        assets=load_assets(cfg.universe.assets_file),
        account=account,
        market=market,
        store=MarketStore(paths.MARKET_DB),
        repo=StateRepo.open(paths.STATE_DB),
        notifier=TelegramNotifier(enabled=cfg.notify.telegram_enabled),
    )


def cmd_run(cfg: AppConfig, args: argparse.Namespace) -> int:
    try:
        allow_live = check_live_allowed(cfg, args)
        deps = build_deps(cfg, allow_live=allow_live)
    except LiveRefused as e:
        print(f"拒绝启动：{e}", file=sys.stderr)
        return EXIT_LIVE_REFUSED
    runner = DailyRunner(deps)
    if args.daemon:
        return run_daemon(runner, cfg.rebalance.run_time_utc)
    if not args.once:
        print("请指定 --once（执行一次）或 --daemon（常驻，每天定时执行）", file=sys.stderr)
        return EXIT_ERROR
    res = runner.run_once(force_rebalance=args.force_rebalance, dry_run=True if args.dry_run else None)
    print(f"运行 {res.run_id}：{res.status}")
    for m in res.messages:
        print(f"  {m}")
    return EXIT_OK if res.status in ("DONE", "SKIPPED", "DRY_RUN", "KILLED") else EXIT_ERROR


def cmd_status(cfg: AppConfig, args: argparse.Namespace) -> int:
    deps = build_deps(cfg)
    repo = deps.repo
    st = repo.get_system_state(cfg.strategy_id)
    last = repo.last_run(cfg.strategy_id)
    snap = repo.latest_nav_snapshot(cfg.strategy_id, cfg.account_id)
    print(
        f"环境 {cfg.env.value}，策略 {cfg.strategy_id}，系统状态 {st.state if st else 'RUNNING（未运行过）'}"
    )
    if last:
        print(f"最近一次运行：{last.run_id} {last.status} {last.error or ''}")
    if snap:
        print(
            f"最近快照（{snap.kind} {snap.time:%Y-%m-%d %H:%M}）：净值 {snap.nav_usdt:.2f}，单位净值 {snap.nav_per_unit:.4f}"
        )
    bal = DailyRunner(deps)._view().get_balances()
    managed = managed_assets(repo, cfg)
    print("受管资产：" + ("，".join(f"{a} {bal[a].total}" for a in sorted(managed) if a in bal) or "无"))
    print(f"USDT：{bal[QUOTE].total if QUOTE in bal else 0}")
    return EXIT_OK


def cmd_kill(cfg: AppConfig, args: argparse.Namespace) -> int:
    """立即执行。有运行在进行时先写 KILL 文件，等它在下一笔订单前停下（最长 120 秒）（spec §5.5）。"""
    deps = build_deps(cfg, allow_live=cfg.env is Env.LIVE)
    KILL_FILE.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
    lock = FileLock(str(LOCK_FILE))
    try:
        lock.acquire(timeout=KILL_LOCK_WAIT_SEC)
    except Timeout:
        print(
            f"{KILL_LOCK_WAIT_SEC:.0f} 秒内拿不到进程锁；KILL 文件已保留，正在运行的进程会在下一笔订单前停止",
            file=sys.stderr,
        )
        return EXIT_ERROR
    try:
        res = DailyRunner(deps).kill(reason="kill 命令")
    finally:
        lock.release()
    print(f"已紧急停止：{res.status}")
    return EXIT_OK


def _repo(cfg: AppConfig) -> StateRepo:
    return StateRepo.open(paths.STATE_DB)


def cmd_pause(cfg: AppConfig, args: argparse.Namespace) -> int:
    _repo(cfg).set_system_state(cfg.strategy_id, SystemState.PAUSED.value, reason="pause 命令")
    print("已暂停：之后的运行只做快照和报告，不交易")
    return EXIT_OK


def cmd_resume(cfg: AppConfig, args: argparse.Namespace) -> int:
    repo = _repo(cfg)
    st = repo.get_system_state(cfg.strategy_id)
    state = SystemState(st.state) if st else SystemState.RUNNING
    if not args.confirm:
        print("恢复交易必须加 --confirm", file=sys.stderr)
        return EXIT_ERROR
    if state is SystemState.KILLED and not args.after_kill:
        print("系统处于 KILLED，恢复必须同时加 --after-kill", file=sys.stderr)
        return EXIT_ERROR
    if KILL_FILE.exists():
        print("KILL 文件仍然存在，请先删除", file=sys.stderr)
        return EXIT_ERROR
    repo.set_system_state(
        cfg.strategy_id, SystemState.RUNNING.value, reason=f"resume（原状态 {state.value}）"
    )
    print(f"已恢复：{state.value} → RUNNING")
    return EXIT_OK


def cmd_capital(cfg: AppConfig, args: argparse.Namespace) -> int:
    """出入金登记（按当时的单位净值申购 / 赎回份额）与余额变化确认（spec §5.2）。"""
    deps = build_deps(cfg)
    repo = deps.repo
    acct = DailyRunner(deps)._view()
    rules = acct.get_all_rules()
    balances = acct.get_balances()
    managed = managed_assets(repo, cfg)
    prices = _prices_for(acct, managed, rules)
    nav = ledger.compute_nav(balances, prices, managed).nav
    details = {
        "balances": {a: str(b.total) for a, b in balances.items()},
        "note": args.note,
        "fills_through": DailyRunner(deps)._fills_through(),
    }
    if args.acknowledge:
        units, navu = DailyRunner(deps)._units(nav)
        repo.append_nav_snapshot(strategy_id=cfg.strategy_id, account_id=cfg.account_id, kind="ACK", nav_usdt=nav,
                                 units=units, nav_per_unit=navu, gross_exposure=Decimal(0), details=details)  # fmt: skip
        print("已确认当前余额，作为下一次核对的基准")
        return EXIT_OK
    try:
        amount = Decimal(args.deposit or args.withdraw)
    except InvalidOperation:
        print("金额格式不正确", file=sys.stderr)
        return EXIT_ERROR
    kind = "DEPOSIT" if args.deposit else "WITHDRAW"
    # 资金已经到账 / 已经转出：按流动发生前的净值计算份额
    nav_before = nav - amount if kind == "DEPOSIT" else nav + amount
    units, _ = DailyRunner(deps)._units(nav_before)
    navu = nav_before / units
    new_units = ledger.apply_capital_flow(units, nav_before, amount, kind)
    repo.append_capital_flow(strategy_id=cfg.strategy_id, account_id=cfg.account_id, kind=kind, amount_usdt=amount,
                             nav_per_unit=navu, units_delta=new_units - units, note=args.note)  # fmt: skip
    repo.append_nav_snapshot(strategy_id=cfg.strategy_id, account_id=cfg.account_id, kind="ACK", nav_usdt=nav,
                             units=new_units, nav_per_unit=navu, gross_exposure=Decimal(0), details=details)  # fmt: skip
    print(f"已登记 {kind} {amount} USDT：单位净值 {navu:.4f}，份额 {units:.4f} → {new_units:.4f}")
    return EXIT_OK


def cmd_adopt(cfg: AppConfig, args: argparse.Namespace) -> int:
    repo = _repo(cfg)
    asset = args.asset.upper()
    _, newly = repo.ensure_managed_position(cfg.account_id, cfg.strategy_id, asset, as_of=datetime.now(UTC))
    print(f"{asset} {'已纳入受管资产' if newly else '本来就是受管资产'}")
    return EXIT_OK
