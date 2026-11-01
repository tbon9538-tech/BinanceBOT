"""研究框架：预注册实验网格、试验记录、walk-forward、bootstrap、DSR（spec §9.5，dev_plan §3、§4）。

- 实验定义在 doc/research/experiments.yaml；
- 每个变体的完整报告写入 reports/<实验>/<变体>/（不进 git）；
- 每个实验的汇总表与关键图表写入 doc/research/reports/<实验>/（提交）；
- 所有试验追加到 doc/research/trials.csv，供 DSR 计算试验次数。
"""

from __future__ import annotations

import copy
import csv
import itertools
import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml

from bot.backtest import benchmarks as bm
from bot.backtest import stats
from bot.backtest.engine import BacktestResult, EngineOptions, rebalance_stats, run_backtest
from bot.backtest.report import build_summary, write_report
from bot.config import AppConfig, config_hash
from bot.strategy.indicators import MarketPanel
from bot.strategy.momentum_v1 import MomentumV1

EXPERIMENTS_FILE = Path("doc/research/experiments.yaml")
TRIALS_FILE = Path("doc/research/trials.csv")
REPORTS_DIR = Path("reports")
DOC_REPORTS_DIR = Path("doc/research/reports")
TRIAL_FIELDS = [
    "time", "git_commit", "experiment", "variant", "config_hash", "overrides", "start", "end",
    "cagr", "vol", "sharpe", "mdd", "calmar", "h1_sharpe", "h2_sharpe", "avg_gross", "turnover_per_year",
    "total_cost_ratio",
]  # fmt: skip


def git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:
        return "unknown"


def apply_overrides(base: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    """用点号路径覆盖配置，例如 {"sleeves.satellite.momentum_lookback": 21}。"""
    out = copy.deepcopy(dict(base))
    for path, value in overrides.items():
        node = out
        keys = path.split(".")
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value
    return out


@dataclass
class Variant:
    name: str
    overrides: dict[str, Any] = field(default_factory=dict)
    engine: dict[str, Any] = field(default_factory=dict)  # EngineOptions 覆盖，例如 {"ideal": true}


def expand_experiment(spec: Mapping[str, Any]) -> list[Variant]:
    """variants（显式列表）与 grid（笛卡尔积）二选一或并用；base 作用于所有变体。"""
    base = dict(spec.get("base", {}))
    out: list[Variant] = []
    for v in spec.get("variants", []):
        out.append(Variant(str(v["name"]), {**base, **v.get("overrides", {})}, dict(v.get("engine", {}))))
    grid = spec.get("grid")
    if grid:
        keys = list(grid)
        for combo in itertools.product(*(grid[k] for k in keys)):
            ov = dict(zip(keys, combo, strict=True))
            name = ",".join(f"{k.split('.')[-1]}={v}" for k, v in ov.items())
            out.append(Variant(name, {**base, **ov}, dict(spec.get("engine", {}))))
    return out


class ResearchContext:
    """在一个进程内共享面板与基准，按变体运行回测。"""

    def __init__(
        self, base_cfg: Mapping[str, Any], panel: MarketPanel, asset_ok: Any, start: date, end: date
    ) -> None:
        self.base_cfg = dict(base_cfg)
        self.panel = panel
        self.asset_ok = asset_ok
        self.start, self.end = start, end
        self._bench: dict[str, pd.Series] | None = None

    def config(self, overrides: Mapping[str, Any]) -> AppConfig:
        return AppConfig.model_validate(apply_overrides(self.base_cfg, overrides))

    def benchmarks(self, cfg: AppConfig, cost: float) -> dict[str, pd.Series]:
        if self._bench is None:
            dates = self.panel.dates[
                (self.panel.dates >= pd.Timestamp(self.start)) & (self.panel.dates <= pd.Timestamp(self.end))
            ]
            strat = MomentumV1(cfg, self.panel, asset_ok=self.asset_ok)
            self._bench = {
                "BTC_hold": bm.btc_hold(self.panel, dates, cfg.regime.symbol),
                "BTC_trend": bm.btc_trend(self.panel, strat.regime, dates, cost, cfg.regime.symbol),
                "EW_top10_monthly": bm.ew_top10_monthly(self.panel, dates, cfg.universe, cost, self.asset_ok),
            }
        return self._bench

    def run(self, v: Variant) -> tuple[AppConfig, BacktestResult]:
        cfg = self.config(v.overrides)
        opts = EngineOptions.from_config(cfg, **v.engine)
        strat = MomentumV1(cfg, self.panel, asset_ok=self.asset_ok)
        return cfg, run_backtest(cfg, self.panel, self.start, self.end, opts=opts, strategy=strat)


def append_trial(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRIAL_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


def run_experiment(
    exp_id: str,
    spec: Mapping[str, Any],
    ctx: ResearchContext,
    *,
    reports_dir: Path = REPORTS_DIR,
    doc_dir: Path = DOC_REPORTS_DIR,
    trials_file: Path = TRIALS_FILE,
) -> pd.DataFrame:
    variants = expand_experiment(spec)
    commit = git_commit()
    rows = []
    navs: dict[str, pd.Series] = {}
    for v in variants:
        cfg, res = ctx.run(v)
        bench = ctx.benchmarks(cfg, EngineOptions.from_config(cfg).cost)
        out_dir = reports_dir / exp_id / _safe(v.name)
        summ = write_report(
            out_dir, f"{exp_id} {v.name}", res, bench, ctx.panel.close[cfg.regime.symbol],
            usdt_yield=cfg.backtest.usdt_yield_annual, segment_split=cfg.research.segment_split,
        )  # fmt: skip
        rebal = rebalance_stats(res)
        sat = rebal[rebal["sleeve"] == "satellite"]
        row = {
            "time": datetime.now(UTC).isoformat(timespec="seconds"),
            "git_commit": commit,
            "experiment": exp_id,
            "variant": v.name,
            "config_hash": config_hash(cfg),
            "overrides": json.dumps(
                {**v.overrides, **({"engine": v.engine} if v.engine else {})}, ensure_ascii=False
            ),
            "start": ctx.start.isoformat(),
            "end": ctx.end.isoformat(),
            **{k: summ.get(k) for k in TRIAL_FIELDS if k in summ},
        }
        append_trial(trials_file, row)
        row["sat_trades_per_month"] = float(sat["trades_per_month"].iloc[0]) if len(sat) else float("nan")
        n_boot = int(spec.get("bootstrap", 0))
        if n_boot:
            mdds = stats.block_bootstrap_mdd(stats.daily_returns(res.nav), block_days=30, n=n_boot)
            row["boot_mdd_p80"] = float(np.percentile(mdds, 20))
        row["worst_7d"] = float((res.nav / res.nav.shift(7) - 1).min())
        row["yearly"] = summ["yearly"]
        rows.append(row)
        navs[v.name] = res.nav
    table = pd.DataFrame(rows)
    _write_experiment_summary(exp_id, spec, table, navs, ctx, doc_dir / exp_id)
    return table


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_.=," else "_" for ch in name)[:120]


def _write_experiment_summary(
    exp_id: str,
    spec: Mapping[str, Any],
    table: pd.DataFrame,
    navs: Mapping[str, pd.Series],
    ctx: ResearchContext,
    out: Path,
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    years = sorted({y for ys in table["yearly"] for y in ys})
    lines = [
        f"# {exp_id}：{spec.get('title', '')}",
        "",
        f"- 区间：{ctx.start} ～ {ctx.end}；变体数：{len(table)}；git {git_commit()}",
        f"- 假设：{spec.get('hypothesis', '')}",
        f"- 通过标准：{spec.get('criteria', 'strategy_research.md §0.3')}",
        "",
        "| 变体 | 年化 | 夏普 | 最大回撤 | Calmar | 前半段夏普 | 后半段夏普 | 段A夏普 | 段B夏普 | 段B回撤 | 平均仓位 | 年换手 | "
        "卫星月均交易 | 最差7日 | bootstrap回撤P80 | " + " | ".join(years) + " |",
        "|---" * (15 + len(years)) + "|",
    ]
    for r in table.itertuples():
        y = r.yearly
        lines.append(
            f"| {r.variant} | {r.cagr:.1%} | {r.sharpe:.2f} | {r.mdd:.1%} | {r.calmar:.2f} | "
            f"{getattr(r, 'h1_sharpe', float('nan')):.2f} | {getattr(r, 'h2_sharpe', float('nan')):.2f} | "
            f"{getattr(r, 'seg_a_sharpe', float('nan')):.2f} | {getattr(r, 'seg_b_sharpe', float('nan')):.2f} | "
            f"{getattr(r, 'seg_b_mdd', float('nan')):.1%} | "
            f"{r.avg_gross:.2f} | {r.turnover_per_year:.1f} | {r.sat_trades_per_month:.1f} | "
            f"{r.worst_7d:.1%} | {getattr(r, 'boot_mdd_p80', float('nan')):.1%} | "
            + " | ".join(f"{y.get(k, float('nan')):.1%}" for k in years)
            + " |"
        )
    bench = ctx._bench or {}
    if bench:
        lines += ["", "基准：", ""]
        for name, b in bench.items():
            s = stats.summarize(b)
            lines.append(f"- {name}：年化 {s['cagr']:.1%}，夏普 {s['sharpe']:.2f}，最大回撤 {s['mdd']:.1%}")
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    fig, ax = plt.subplots(figsize=(11, 5.5))
    for name, nav in navs.items():
        ax.plot(nav.index, nav.to_numpy(), linewidth=1.0, label=name[:40])
    for name, b in bench.items():
        ax.plot(b.index, b.to_numpy(), linewidth=0.8, linestyle="--", label=name)
    ax.set_yscale("log")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=6, ncol=2, loc="upper left")
    ax.set_title(f"{exp_id} equity (log)")
    fig.tight_layout()
    fig.savefig(out / "equity.png", dpi=100)
    plt.close(fig)


def load_experiments(path: Path = EXPERIMENTS_FILE) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} 顶层必须是映射")
    return data


def walk_forward(
    navs: Mapping[str, pd.Series], train_months: int, test_months: int, metric: str = "calmar"
) -> tuple[pd.Series, pd.DataFrame]:
    """固定规则的 walk-forward：每个测试窗口选训练窗口内 metric 最高的候选，拼接测试窗口收益（忽略切换成本）。"""
    rets = pd.DataFrame({k: v.pct_change() for k, v in navs.items()}).dropna(how="all")
    start = rets.index[0] + pd.DateOffset(months=train_months)
    chosen = []
    pieces = []
    t = start
    while t < rets.index[-1]:
        train = rets.loc[t - pd.DateOffset(months=train_months) : t - pd.Timedelta(days=1)]
        test = rets.loc[t : t + pd.DateOffset(months=test_months) - pd.Timedelta(days=1)]
        scores = {k: stats.summarize((1 + train[k].fillna(0)).cumprod())[metric] for k in rets.columns}
        best = max(scores, key=lambda k: -np.inf if not np.isfinite(scores[k]) else scores[k])
        chosen.append({"test_start": t.date().isoformat(), "chosen": best, "train_score": scores[best]})
        pieces.append(test[best])
        t = t + pd.DateOffset(months=test_months)
    stitched = pd.concat(pieces).fillna(0)
    return (1 + stitched).cumprod(), pd.DataFrame(chosen)


def summary_row(
    result: BacktestResult, benchmarks: dict[str, pd.Series], *, usdt_yield: float = 0.0
) -> dict[str, Any]:
    return build_summary(result, benchmarks, usdt_yield=usdt_yield)
