"""命令行入口：`python -m bot <子命令>`。

每个子命令先加载并校验配置。已实现的子命令见 HANDLERS；其余提示尚未实现（退出码 3）。
各子命令的实现里程碑见 doc/dev_plan.md。
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from bot import paths
from bot.app.data_commands import cmd_assets, cmd_data
from bot.app.ops_commands import cmd_adopt, cmd_capital, cmd_kill, cmd_pause, cmd_resume, cmd_run, cmd_status
from bot.app.research_commands import (
    cmd_backtest,
    cmd_bootstrap,
    cmd_dsr,
    cmd_experiment,
    cmd_holdout,
    cmd_walkforward,
)
from bot.config import AppConfig, load_config
from bot.data.holdout import HoldoutGuard, HoldoutLog, HoldoutViolation
from bot.domain.errors import ConfigError, RateLimitedError

EXIT_OK = 0
EXIT_CONFIG_ERROR = 1
EXIT_NOT_IMPLEMENTED = 3
EXIT_PARTIAL_FAILURE = 4
EXIT_HOLDOUT_VIOLATION = 5
EXIT_RATE_LIMITED = 6

DEFAULT_CONFIG = Path("config/config.yaml")


def _add_run(p: argparse.ArgumentParser) -> None:
    p.add_argument("--once", action="store_true", help="执行一次每日流程后退出（spec §6.2）")
    p.add_argument("--daemon", action="store_true", help="常驻：每天在 rebalance.run_time_utc 执行一次")
    p.add_argument("--dry-run", action="store_true", help="只计算并输出订单计划，不下单")
    p.add_argument(
        "--force-rebalance", action="store_true", help="忽略当日已完成检查与权重容忍带（testnet 联调用）"
    )
    p.add_argument("--live-confirm", default=None, help="env=live 时必须为 I_UNDERSTAND（spec §13）")
    p.add_argument(
        "--yes", action="store_true", help="跳过控制台 YES 确认（Docker 用，不能替代 --live-confirm）"
    )


def _add_backtest(p: argparse.ArgumentParser) -> None:
    p.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD，默认取配置")
    p.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD，必须早于保留集起点")
    p.add_argument("--name", default=None, help="报告目录名 reports/<name>/")


def _add_experiment(p: argparse.ArgumentParser) -> None:
    p.add_argument("--id", required=True, help="实验编号，例如 E3a（定义在 doc/research/experiments.yaml）")


def _add_walkforward(p: argparse.ArgumentParser) -> None:
    p.add_argument("--train-months", type=int, default=12)
    p.add_argument("--test-months", type=int, default=3)
    p.add_argument("--choose", default="target_annual_vol,core_weight", help="允许在训练窗口中选择的参数")


def _add_bootstrap(p: argparse.ArgumentParser) -> None:
    p.add_argument("--block-days", type=int, default=30)
    p.add_argument("--n", type=int, default=1000)


def _add_holdout(p: argparse.ArgumentParser) -> None:
    p.add_argument("--final", action="store_true", help="确认这是定参后的保留集检验")
    p.add_argument("--confirm", default=None, help="当日日期 YYYY-MM-DD")
    p.add_argument("--i-know-this-is-not-the-first-run", action="store_true")


def _add_dsr(p: argparse.ArgumentParser) -> None:
    p.add_argument("--trials", default="doc/research/trials.csv")


def _add_resume(p: argparse.ArgumentParser) -> None:
    p.add_argument("--confirm", action="store_true")
    p.add_argument("--after-kill", action="store_true")


def _add_capital(p: argparse.ArgumentParser) -> None:
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--deposit", default=None, help="登记充值金额（USDT）")
    g.add_argument("--withdraw", default=None, help="登记提现金额（USDT）")
    g.add_argument("--acknowledge", action="store_true", help="确认无法解释的余额变化")
    p.add_argument("--note", default="")


def _add_adopt(p: argparse.ArgumentParser) -> None:
    p.add_argument("asset", help="要接管的资产，例如 SOL")


def _add_assets(p: argparse.ArgumentParser) -> None:
    p.add_argument("action", choices=["review"], help="review：列出待审核资产，生成审核清单与草稿")
    p.add_argument("--as-of", default=None, help="数据截止日 YYYY-MM-DD，默认最近一根已收盘日线")
    p.add_argument("--db", default=str(paths.MARKET_DB))
    p.add_argument("--out-dir", default=str(paths.ASSETS_REVIEW_DIR))


def _add_data(p: argparse.ArgumentParser) -> None:
    p.add_argument("action", choices=["sync", "report"], help="sync：下载并缓存历史日线；report：覆盖情况")
    p.add_argument("--since", default="2018-01-01")
    p.add_argument("--symbols", default=None, help="只同步这些交易对，逗号分隔")
    p.add_argument("--workers", type=int, default=8, help="并发下载数（上限 8）")
    p.add_argument("--db", default=str(paths.MARKET_DB))
    p.add_argument("--raw-dir", default=str(paths.RAW_DIR))


def _no_args(p: argparse.ArgumentParser) -> None:
    pass


# 子命令 → (说明, 计划实现的里程碑, 参数定义)
COMMANDS: dict[str, tuple[str, str, Callable[[argparse.ArgumentParser], None]]] = {
    "run": ("执行每日流程（数据、信号、风控、下单、对账）", "M5", _add_run),
    "backtest": ("单次回测（研究引擎）", "M2", _add_backtest),
    "experiment": ("运行预注册实验网格", "M2", _add_experiment),
    "walkforward": ("滚动训练与检验", "M3", _add_walkforward),
    "bootstrap": ("最大回撤的 block bootstrap 分布", "M2", _add_bootstrap),
    "holdout": ("保留集一次性检验", "M4", _add_holdout),
    "dsr": ("计算 Deflated Sharpe Ratio", "M2", _add_dsr),
    "kill": ("紧急停止：撤单、清仓受管资产、进入 KILLED", "M6", _no_args),
    "pause": ("暂停交易（只做快照和报告）", "M6", _no_args),
    "resume": ("从 PAUSED / HALTED / KILLED 恢复", "M6", _add_resume),
    "status": ("查看系统状态、余额与持仓", "M5", _no_args),
    "capital": ("登记出入金 / 确认余额变化", "M6", _add_capital),
    "adopt": ("把账户中已有的资产纳入受管", "M6", _add_adopt),
    "assets": ("资产审核", "M1", _add_assets),
    "data": ("历史数据下载与缓存", "M1", _add_data),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m bot", description="币安现货趋势–动量轮动机器人")
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG, help=f"配置文件路径，默认 {DEFAULT_CONFIG}"
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="<子命令>")
    for name, (help_text, milestone, add_args) in COMMANDS.items():
        p = sub.add_parser(name, help=f"{help_text}（{milestone}）", description=help_text)
        add_args(p)
    return parser


Handler = Callable[[AppConfig, argparse.Namespace], int]

HANDLERS: dict[str, Handler] = {
    "data": cmd_data,
    "assets": cmd_assets,
    "backtest": cmd_backtest,
    "experiment": cmd_experiment,
    "walkforward": cmd_walkforward,
    "bootstrap": cmd_bootstrap,
    "dsr": cmd_dsr,
    "holdout": cmd_holdout,
    "run": cmd_run,
    "status": cmd_status,
    "kill": cmd_kill,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "capital": cmd_capital,
    "adopt": cmd_adopt,
}

RESEARCH_COMMANDS = {"backtest", "experiment", "walkforward", "bootstrap"}


def _holdout_checks(cfg: AppConfig, args: argparse.Namespace) -> None:
    """保留集守卫（spec §3.6）：研究命令的 --end 不得落入保留集；holdout 命令必须满足启动条件。"""
    guard = HoldoutGuard(cfg.research.holdout_start)
    if args.command in RESEARCH_COMMANDS and getattr(args, "end", None):
        guard.check_end(date.fromisoformat(args.end))
    if args.command == "holdout":
        HoldoutLog().authorize(
            final=args.final,
            confirm=date.fromisoformat(args.confirm) if args.confirm else None,
            today=datetime.now(UTC).date(),
            not_first_run=args.i_know_this_is_not_the_first_run,
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return EXIT_CONFIG_ERROR
    logging.basicConfig(
        level=cfg.logging.level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # 不逐条记录请求
    try:
        _holdout_checks(cfg, args)
    except HoldoutViolation as e:
        print(f"保留集守卫：{e}", file=sys.stderr)
        return EXIT_HOLDOUT_VIOLATION
    handler = HANDLERS.get(args.command)
    if handler is None:
        _, milestone, _ = COMMANDS[args.command]
        print(f"配置有效。子命令 {args.command!r} 尚未实现，计划在 {milestone} 实现。", file=sys.stderr)
        return EXIT_NOT_IMPLEMENTED
    try:
        return handler(cfg, args)
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except RateLimitedError as e:
        print(f"被限频：{e}。已停止发送请求，请稍后再试（spec §3.5）", file=sys.stderr)
        return EXIT_RATE_LIMITED
