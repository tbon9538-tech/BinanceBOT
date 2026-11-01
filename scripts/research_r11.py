"""研究 R11：短线策略（5 分钟 K 线）。预注册见 doc/research/log.md "R11 预注册"，定义见 experiments.yaml 的 R11。

用法（项目根目录）：uv run python scripts/research_r11.py
只读研究集（≤ research.holdout_start 前一天）；24 个试验追加到 doc/research/trials.csv；
摘要写入 doc/research/reports/R11/summary.md。
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import paths
from bot.backtest import stats
from bot.backtest.intraday import (
    Costs,
    SimResult,
    breakout_signals,
    dip_signals,
    run_grid,
    run_signals,
    win_day_ratio,
)
from bot.backtest.report import HALF_SPLIT
from bot.backtest.research import TRIALS_FILE, append_trial, git_commit
from bot.config import load_config
from bot.data.holdout import HoldoutGuard
from bot.data.intraday import load_5m

BARS_PER_HOUR = 12


def run_one(strategy: str, param: float, bars: pd.DataFrame, spec: dict[str, Any], costs: Costs) -> SimResult:
    cap = float(spec["capital_usdt"])
    if strategy == "S1_grid":
        g = spec["S1_grid"]
        return run_grid(
            bars, step=param, capital=cap, costs=costs, unit_frac=g["unit"], max_units=g["max_units"]
        )
    if strategy == "S2_dip":
        s = spec["S2_dip"]
        enter, exit_ = dip_signals(bars, param, s["window_bars"])
        return run_signals(bars, enter, exit_, capital=cap, costs=costs, max_hold=s["max_hold_bars"])
    enter, exit_ = breakout_signals(bars, int(param * BARS_PER_HOUR))
    return run_signals(bars, enter, exit_, capital=cap, costs=costs)


def metrics(res: SimResult, split: pd.Timestamp) -> dict[str, float]:
    nav = res.daily
    s = stats.summarize(nav)
    days = max(len(nav) - 1, 1)
    out = {
        "cagr": s["cagr"],
        "vol": s["vol"],
        "sharpe": s["sharpe"],
        "mdd": s["mdd"],
        "calmar": s["calmar"],
        "win_days": win_day_ratio(nav),
        "avg_daily": float(nav.pct_change().dropna().mean()),
        "trades_per_day": res.trades / days,
        "in_market": res.bars_in_market / max(len(res.equity), 1),
        "fee_share": (res.fees + res.slippage) / res.gross_pnl if res.gross_pnl > 0 else float("nan"),
        "total_cost_ratio": (res.fees + res.slippage) / res.capital,
    }
    for name, part in (("a", nav.loc[nav.index < split]), ("b", nav.loc[nav.index >= split])):
        ps = stats.summarize(part) if len(part) > 30 else {"cagr": float("nan"), "sharpe": float("nan")}
        out[f"seg_{name}_cagr"], out[f"seg_{name}_sharpe"] = ps["cagr"], ps["sharpe"]
    for name, part in (("h1", nav.loc[nav.index < HALF_SPLIT]), ("h2", nav.loc[nav.index >= HALF_SPLIT])):
        out[f"{name}_sharpe"] = stats.summarize(part)["sharpe"] if len(part) > 30 else float("nan")
    return out


def main() -> int:
    cfg = load_config(Path("config/config.yaml"))
    start, end = cfg.backtest.start, cfg.backtest.end
    HoldoutGuard(cfg.research.holdout_start).check_end(end)
    spec = yaml.safe_load(Path("doc/research/experiments.yaml").read_text(encoding="utf-8"))["R11"]
    split = pd.Timestamp(cfg.research.segment_split)
    base = Costs(**spec["costs"])
    commit = git_commit()
    rows: list[dict[str, Any]] = []
    for sym in spec["symbols"]:
        bars = load_5m(sym, start, end, paths.RAW_DIR)
        print(f"{sym}：{len(bars)} 根 5 分钟 K 线，{bars.index[0]} ～ {bars.index[-1]}", flush=True)
        for strat, key in (("S1_grid", "step"), ("S2_dip", "z_entry"), ("S3_breakout", "lookback_hours")):
            sp = spec[strat]
            runs = [(p, base, "") for p in sp[key]] + [
                (sp["main"], base.scaled(spec["cost_stress"]), "_cost1.5")
            ]
            for param, costs, tag in runs:
                res = run_one(strat, param, bars, spec, costs)
                m = metrics(res, split)
                variant = f"{strat}_{param}_{sym}{tag}"
                overrides = {
                    "strategy": strat,
                    "param": param,
                    "symbol": sym,
                    "cost_scale": spec["cost_stress"] if tag else 1,
                }
                rows.append(
                    {
                        "variant": variant,
                        "strategy": strat,
                        "param": param,
                        "symbol": sym,
                        "stress": bool(tag),
                        **m,
                    }
                )
                append_trial(
                    TRIALS_FILE,
                    {
                        "time": datetime.now(UTC).isoformat(timespec="seconds"),
                        "git_commit": commit,
                        "experiment": "R11",
                        "variant": variant,
                        "config_hash": hashlib.sha1(
                            json.dumps(overrides, sort_keys=True).encode()
                        ).hexdigest()[:10],
                        "overrides": json.dumps(overrides, ensure_ascii=False),
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                        "avg_gross": m["in_market"],
                        "turnover_per_year": m["trades_per_day"] * 365,
                        **{
                            k: m[k]
                            for k in (
                                "cagr",
                                "vol",
                                "sharpe",
                                "mdd",
                                "calmar",
                                "h1_sharpe",
                                "h2_sharpe",
                                "total_cost_ratio",
                            )
                        },
                    },
                )
                print(
                    f"{variant:<32} 年化 {m['cagr']:7.1%} 夏普 {m['sharpe']:5.2f} 回撤 {m['mdd']:7.1%} "
                    f"盈利天数 {m['win_days']:5.1%} 段A {m['seg_a_cagr']:7.1%} 段B {m['seg_b_cagr']:7.1%} "
                    f"日均交易 {m['trades_per_day']:5.1f}",
                    flush=True,
                )
    out = Path("doc/research/reports/R11")
    out.mkdir(parents=True, exist_ok=True)
    lines = [
        "# R11：短线策略（5 分钟 K 线）",
        "",
        f"- 区间：{start} ～ {end}；段 A < {split.date()} ≤ 段 B；git {commit}",
        "- 成本：吃单每边 0.1% + 滑点 0.05%；挂单每边 0.1%，须穿过挂单价 0.05% 才成交；`_cost1.5` 为成本 ×1.5",
        "",
        "| 变体 | 年化 | 夏普 | 最大回撤 | 盈利天数 | 平均日收益 | 段A年化 | 段B年化 | 段A夏普 | 段B夏普 | 日均交易 | 在场比例 | 成本/毛利 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['variant']} | {r['cagr']:.1%} | {r['sharpe']:.2f} | {r['mdd']:.1%} | {r['win_days']:.1%} | "
            f"{r['avg_daily']:.3%} | {r['seg_a_cagr']:.1%} | {r['seg_b_cagr']:.1%} | {r['seg_a_sharpe']:.2f} | "
            f"{r['seg_b_sharpe']:.2f} | {r['trades_per_day']:.1f} | {r['in_market']:.0%} | "
            + (f"{r['fee_share']:.0%}" if pd.notna(r["fee_share"]) else "毛利 ≤ 0")
            + " |"
        )
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "results.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1, default=float), encoding="utf-8"
    )
    print(f"摘要：{out / 'summary.md'}")
    if any(r["sharpe"] > 3 for r in rows):
        print("警告：存在夏普 > 3 的试验，按规则先停下排查")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
