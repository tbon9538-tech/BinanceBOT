"""回测报告（spec §9.4）：summary.json / summary.md、daily.csv、trades.csv、equity.png。"""

from __future__ import annotations

import json
import re
import shutil
from datetime import date
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from bot.backtest import stats
from bot.backtest.engine import BacktestResult, rebalance_stats

HALF_SPLIT = pd.Timestamp("2022-10-01")  # 前后半段分界（strategy_research.md §0.2）


def regime_intervals(result: BacktestResult, btc_close: pd.Series) -> pd.DataFrame:
    """每个 Risk-On 区间：起止日期、天数、策略收益、BTC 收益。"""
    reg = result.regime["state"]
    nav = result.nav
    rows = []
    start = None
    for t, s in reg.items():
        if s == "RISK_ON" and start is None:
            start = t
        elif s != "RISK_ON" and start is not None:
            rows.append((start, t))
            start = None
    if start is not None:
        rows.append((start, reg.index[-1]))
    out = []
    for a, b in rows:
        out.append(
            {
                "start": pd.Timestamp(a).date().isoformat(),
                "end": pd.Timestamp(b).date().isoformat(),
                "days": (pd.Timestamp(b) - pd.Timestamp(a)).days,
                "strategy": float(nav.loc[b] / nav.loc[a] - 1),
                "btc": float(btc_close.loc[b] / btc_close.loc[a] - 1),
            }
        )
    return pd.DataFrame(out)


def build_summary(
    result: BacktestResult, benchmarks: dict[str, pd.Series], *, usdt_yield: float = 0.0
) -> dict[str, Any]:
    """usdt_yield：年化 USDT 理财收益假设，用于"扣除 USDT 理财收益"的参考夏普（spec §1.2）。"""
    nav = result.nav
    d = result.daily
    summ: dict[str, Any] = stats.summarize(nav)
    summ.update(
        {
            "usdt_yield_annual": usdt_yield,
            "sharpe_net_usdt_yield": stats.sharpe(stats.daily_returns(nav), usdt_yield),
            "start": nav.index[0].date().isoformat(),
            "end": nav.index[-1].date().isoformat(),
            "avg_gross": float(d["gross"].mean()),
            "cash_days_ratio": float((d["gross"] < 0.05).mean()),
            "turnover_per_year": float(d["turnover"].sum() / (len(d) / 365)),
            "total_cost_ratio": float(d["cost"].sum() / d["nav"].iloc[0]),
            "delist_exits": len(result.delist_exits),
            "stop_events": len(result.stop_events),
            "yearly": stats.yearly_returns(nav),
            "drawdowns": stats.drawdown_episodes(nav),
        }
    )
    for name, part in (("h1", nav.loc[: HALF_SPLIT - pd.Timedelta(days=1)]), ("h2", nav.loc[HALF_SPLIT:])):
        if len(part) > 30:
            s = stats.summarize(part)
            summ[f"{name}_sharpe"], summ[f"{name}_cagr"], summ[f"{name}_mdd"] = (
                s["sharpe"],
                s["cagr"],
                s["mdd"],
            )
    summ["benchmarks"] = {}
    for name, b in benchmarks.items():
        bs = stats.summarize(b)
        bs["yearly"] = stats.yearly_returns(b)
        summ["benchmarks"][name] = bs
    return summ


def _fmt(x: Any) -> str:
    if isinstance(x, float):
        return f"{x:.3f}"
    return str(x)


def summary_markdown(name: str, summ: dict[str, Any], rebal: pd.DataFrame, intervals: pd.DataFrame) -> str:
    keys = ["cagr", "vol", "sharpe", "sharpe_net_usdt_yield", "usdt_yield_annual", "mdd", "calmar", "longest_dd_days", "avg_gross", "cash_days_ratio",
            "turnover_per_year", "total_cost_ratio", "days_le_m10", "days_le_m15", "delist_exits", "stop_events"]  # fmt: skip
    lines = [
        f"# 回测报告：{name}",
        "",
        f"区间 {summ['start']} ～ {summ['end']}",
        "",
        "| 指标 | 值 |",
        "|---|---|",
    ]
    lines += [f"| {k} | {_fmt(summ.get(k))} |" for k in keys]
    for h in ("h1", "h2"):
        if f"{h}_sharpe" in summ:
            lines.append(
                f"| {h}（夏普 / 年化 / 回撤） | {summ[f'{h}_sharpe']:.2f} / {summ[f'{h}_cagr']:.1%} / {summ[f'{h}_mdd']:.1%} |"
            )
    years = sorted(summ["yearly"])
    lines += [
        "",
        "## 分年度收益",
        "",
        "| 序列 | " + " | ".join(years) + " |",
        "|---" * (len(years) + 1) + "|",
    ]
    lines.append("| 策略 | " + " | ".join(f"{summ['yearly'][y]:.1%}" for y in years) + " |")
    for bn, bs in summ["benchmarks"].items():
        lines.append(
            f"| {bn} | " + " | ".join(f"{bs['yearly'].get(y, float('nan')):.1%}" for y in years) + " |"
        )
    lines += ["", "## 基准", "", "| 基准 | 年化 | 夏普 | 最大回撤 | Calmar |", "|---|---|---|---|---|"]
    for bn, bs in summ["benchmarks"].items():
        lines.append(
            f"| {bn} | {bs['cagr']:.1%} | {bs['sharpe']:.2f} | {bs['mdd']:.1%} | {bs['calmar']:.2f} |"
        )
    lines += ["", "## 最大回撤区间", "", "| 峰值 | 谷底 | 恢复 | 幅度 |", "|---|---|---|---|"]
    lines += [
        f"| {d['peak']} | {d['trough']} | {d['recovery'] or '未恢复'} | {d['depth']:.1%} |"
        for d in summ["drawdowns"]
    ]
    if not rebal.empty:
        lines += [
            "",
            "## 调仓统计（按袖子）",
            "",
            "| " + " | ".join(rebal.columns) + " |",
            "|---" * len(rebal.columns) + "|",
        ]
        lines += ["| " + " | ".join(_fmt(v) for v in r) + " |" for r in rebal.itertuples(index=False)]
    if not intervals.empty:
        lines += ["", "## Risk-On 区间", "", "| 开始 | 结束 | 天数 | 策略 | BTC |", "|---|---|---|---|---|"]
        lines += [
            f"| {r.start} | {r.end} | {r.days} | {r.strategy:.1%} | {r.btc:.1%} |"
            for r in intervals.itertuples()
        ]
    return "\n".join(lines) + "\n"


def plot_equity(result: BacktestResult, benchmarks: dict[str, pd.Series], path: Path, title: str) -> None:
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    ax1.plot(result.nav.index, result.nav.to_numpy(), label="strategy", linewidth=1.6, color="#1f4e79")
    palette = ["#999999", "#c0504d", "#9bbb59", "#8064a2"]
    for (name, b), c in zip(benchmarks.items(), palette, strict=False):
        ax1.plot(b.index, b.to_numpy(), label=name, linewidth=1.0, color=c)
    ax1.set_yscale("log")
    ax1.set_title(re.sub(r"[^\x00-\x7f]", "", title).strip() or "equity")  # 镜像中没有中文字体
    ax1.legend(loc="upper left", fontsize=8)
    ax1.grid(alpha=0.3)
    dd = stats.drawdown(result.nav)
    ax2.fill_between(dd.index, dd.to_numpy(), 0, color="#c0504d", alpha=0.4)
    ax2.set_ylabel("drawdown")
    ax2.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def write_report(
    out_dir: Path,
    name: str,
    result: BacktestResult,
    benchmarks: dict[str, pd.Series],
    btc_close: pd.Series,
    *,
    usdt_yield: float = 0.0,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    summ = build_summary(result, benchmarks, usdt_yield=usdt_yield)
    rebal = rebalance_stats(result)
    intervals = regime_intervals(result, btc_close.loc[result.nav.index])
    (out_dir / "summary.json").write_text(
        json.dumps(summ, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (out_dir / "summary.md").write_text(summary_markdown(name, summ, rebal, intervals), encoding="utf-8")
    result.daily.to_csv(out_dir / "daily.csv")
    result.trades.to_csv(out_dir / "trades.csv", index=False)
    plot_equity(result, benchmarks, out_dir / "equity.png", name)
    return summ


def publish(out_dir: Path, doc_dir: Path, files: tuple[str, ...] = ("summary.md", "equity.png")) -> None:
    """把摘要与关键图表复制到 doc/research/reports/<编号>/（D-015）。"""
    doc_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        if (out_dir / f).exists():
            shutil.copy2(out_dir / f, doc_dir / f)


def today() -> date:
    return pd.Timestamp.utcnow().date()
