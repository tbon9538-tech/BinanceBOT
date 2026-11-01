"""研究类子命令：backtest / experiment / walkforward / bootstrap / dsr（M2–M3）。

所有数据都通过 ResearchMarketView 读取，保留集被强制过滤（spec §3.6）。
"""

from __future__ import annotations

import argparse
import csv
import math
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from bot import paths
from bot.backtest import stats
from bot.backtest.data import load_research_panel
from bot.backtest.engine import EngineOptions
from bot.backtest.report import write_report
from bot.backtest.research import (
    DOC_REPORTS_DIR,
    REPORTS_DIR,
    TRIALS_FILE,
    ResearchContext,
    Variant,
    expand_experiment,
    load_experiments,
    run_experiment,
    walk_forward,
)
from bot.config import AppConfig, load_assets
from bot.data.holdout import HoldoutGuard
from bot.data.store import MarketStore, ResearchMarketView

EXIT_OK = 0


def _dates(cfg: AppConfig, args: argparse.Namespace) -> tuple[date, date]:
    start = date.fromisoformat(args.start) if getattr(args, "start", None) else cfg.backtest.start
    end = date.fromisoformat(args.end) if getattr(args, "end", None) else cfg.backtest.end
    HoldoutGuard(cfg.research.holdout_start).check_end(end)
    return start, end


def build_context(cfg: AppConfig, config_path: Path, start: date, end: date) -> ResearchContext:
    store = MarketStore(paths.MARKET_DB)
    view = ResearchMarketView(store, HoldoutGuard(cfg.research.holdout_start))
    assets = load_assets(cfg.universe.assets_file)
    panel, asset_ok = load_research_panel(store, view, cfg, assets, end=end)
    base = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    return ResearchContext(base, panel, asset_ok, start, end)


def _print_summary(name: str, summ: dict[str, Any]) -> None:
    print(
        f"{name}：年化 {summ['cagr']:.1%}，波动 {summ['vol']:.1%}，夏普 {summ['sharpe']:.2f}，"
        f"最大回撤 {summ['mdd']:.1%}，Calmar {summ['calmar']:.2f}，平均仓位 {summ['avg_gross']:.2f}"
    )
    for bn, bs in summ["benchmarks"].items():
        print(f"  基准 {bn}：年化 {bs['cagr']:.1%}，夏普 {bs['sharpe']:.2f}，最大回撤 {bs['mdd']:.1%}")


def cmd_backtest(cfg: AppConfig, args: argparse.Namespace) -> int:
    start, end = _dates(cfg, args)
    ctx = build_context(cfg, args.config, start, end)
    run_cfg, res = ctx.run(Variant("current"))
    bench = ctx.benchmarks(run_cfg, EngineOptions.from_config(run_cfg).cost)
    name = args.name or f"backtest_{start}_{end}"
    out = REPORTS_DIR / name
    summ = write_report(out, name, res, bench, ctx.panel.close[run_cfg.regime.symbol])
    _print_summary(name, summ)
    print(f"报告：{out}")
    return EXIT_OK


def cmd_experiment(cfg: AppConfig, args: argparse.Namespace) -> int:
    exps = load_experiments()
    if args.id not in exps:
        print(f"experiments.yaml 中没有实验 {args.id}；可选：{', '.join(exps)}")
        return 2
    spec = exps[args.id]
    start = date.fromisoformat(spec["start"]) if "start" in spec else cfg.backtest.start
    end = date.fromisoformat(spec["end"]) if "end" in spec else cfg.backtest.end
    HoldoutGuard(cfg.research.holdout_start).check_end(end)
    ctx = build_context(cfg, args.config, start, end)
    table = run_experiment(args.id, spec, ctx)
    for r in table.itertuples():
        print(
            f"{r.variant:<40} 年化 {r.cagr:6.1%} 夏普 {r.sharpe:5.2f} 回撤 {r.mdd:6.1%} Calmar {r.calmar:5.2f} "
            f"H1 {r.h1_sharpe:5.2f} H2 {r.h2_sharpe:5.2f}"
        )
    print(f"摘要：{DOC_REPORTS_DIR / args.id / 'summary.md'}")
    return EXIT_OK


def cmd_walkforward(cfg: AppConfig, args: argparse.Namespace) -> int:
    exps = load_experiments()
    spec = exps.get("WF")
    if spec is None:
        print("experiments.yaml 中缺少 WF（walk-forward 候选网格）")
        return 2
    start, end = _dates(cfg, args)
    ctx = build_context(cfg, args.config, start, end)
    navs = {v.name: ctx.run(v)[1].nav for v in expand_experiment(spec)}
    stitched, chosen = walk_forward(navs, args.train_months, args.test_months)
    s = stats.summarize(stitched)
    out = DOC_REPORTS_DIR / "WF"
    out.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Walk-forward（固定规则，只在候选网格中选择）",
        "",
        f"- 训练 {args.train_months} 个月 / 测试 {args.test_months} 个月；候选 {len(navs)} 个；切换成本忽略",
        f"- 拼接样本外：年化 {s['cagr']:.1%}，夏普 {s['sharpe']:.2f}，最大回撤 {s['mdd']:.1%}，Calmar {s['calmar']:.2f}",
        "",
        "| 测试窗口起点 | 选中 | 训练窗口 Calmar |",
        "|---|---|---|",
    ]
    lines += [f"| {r.test_start} | {r.chosen} | {r.train_score:.2f} |" for r in chosen.itertuples()]
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[2:4]))
    return EXIT_OK


def cmd_bootstrap(cfg: AppConfig, args: argparse.Namespace) -> int:
    start, end = _dates(cfg, args)
    ctx = build_context(cfg, args.config, start, end)
    _, res = ctx.run(Variant("current"))
    mdds = stats.block_bootstrap_mdd(stats.daily_returns(res.nav), block_days=args.block_days, n=args.n)
    pct = {p: float(np.percentile(mdds, 100 - p)) for p in (50, 80, 90, 95)}
    out = DOC_REPORTS_DIR / "bootstrap"
    out.mkdir(parents=True, exist_ok=True)
    text = (
        f"# Block bootstrap 最大回撤分布\n\n- 区间 {start} ～ {end}，块长 {args.block_days} 天，抽样 {args.n} 次\n"
        f"- 样本内最大回撤：{stats.max_drawdown(res.nav):.1%}\n"
        + "".join(f"- {p} 分位（更差的 {100 - p}%）：{v:.1%}\n" for p, v in pct.items())
        + f"- 回撤 ≤ 35% 的概率：{float((mdds >= -0.35).mean()):.0%}；≤ 30%：{float((mdds >= -0.30).mean()):.0%}\n"
    )
    (out / "summary.md").write_text(text, encoding="utf-8")
    print(text)
    return EXIT_OK


def cmd_dsr(cfg: AppConfig, args: argparse.Namespace) -> int:
    trials_path = Path(args.trials) if args.trials else TRIALS_FILE
    with trials_path.open(encoding="utf-8") as f:
        sharpes = [float(r["sharpe"]) for r in csv.DictReader(f) if r.get("sharpe") not in (None, "", "nan")]
    start, end = _dates(cfg, args)
    ctx = build_context(cfg, args.config, start, end)
    _, res = ctx.run(Variant("current"))
    r = stats.daily_returns(res.nav)
    dsr = stats.dsr_from_returns(r, sharpes)
    summ = stats.summarize(res.nav)
    text = (
        f"# Deflated Sharpe Ratio\n\n- 当前配置夏普 {summ['sharpe']:.2f}（区间 {start} ～ {end}）\n"
        f"- 试验总数 {len(sharpes)}，试验夏普标准差 {float(pd.Series(sharpes).std()):.2f}\n"
        f"- DSR = {dsr:.3f}（> 0.9 为强，0.5～0.9 为弱，< 0.5 不显著）\n"
    )
    out = DOC_REPORTS_DIR / "dsr"
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.md").write_text(text, encoding="utf-8")
    print(text)
    return EXIT_OK if math.isfinite(dsr) else 4
