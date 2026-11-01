"""研究 R10：资金费率套利袖子与它和当前策略的组合（预注册见 doc/research/log.md "R10 预注册"）。

只做研究，不写交易代码。运行：uv run python scripts/research_r10.py
- 只下载、只读取研究截止日（backtest.end）及之前的数据，保留集守卫检查截止日并核对所有数据（D-085）；
- 预注册的 7 个试验追加到 doc/research/trials.csv，摘要写入 doc/research/reports/R10/summary.md；
- 成本翻倍只用于判定第 4 条（以及趋势成本也翻倍的观察），不计入试验。
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import paths
from bot.app.research_commands import build_context
from bot.backtest import stats
from bot.backtest.carry import CarryParams, CarryResult, combine, run_carry
from bot.backtest.report import HALF_SPLIT
from bot.backtest.research import DOC_REPORTS_DIR, TRIALS_FILE, Variant, append_trial, git_commit
from bot.config import config_hash, load_config
from bot.data.futures import load_funding, load_perp_daily
from bot.data.holdout import HoldoutGuard
from bot.data.store import MarketStore, ResearchMarketView
from bot.data.vision import VisionClient

CONFIG = Path("config/config.yaml")
SYMBOLS = ("BTCUSDT", "ETHUSDT")
HURDLES: tuple[float | None, ...] = (None, 0.0, 0.05, 0.10)
MAIN_HURDLE = 0.05
WEIGHTS = (0.3, 0.5)
MAIN_W = 0.5
TREND_YIELD = 0.04
WARMUP_DAYS = 30  # 7 日均值的预热


def hname(h: float | None) -> str:
    return "always" if h is None else f"h{round(h * 100)}"


def metrics(nav: pd.Series, split: date) -> dict[str, Any]:
    cut = pd.Timestamp(split)
    out: dict[str, Any] = {"full": stats.summarize(nav)}
    for name, part in (("A", nav.loc[nav.index < cut]), ("B", nav.loc[nav.index >= cut])):
        out[name] = stats.summarize(part)
    h1, h2 = nav.loc[: HALF_SPLIT - pd.Timedelta(days=1)], nav.loc[HALF_SPLIT:]
    out["h1_sharpe"] = stats.summarize(h1)["sharpe"] if len(h1) > 30 else float("nan")
    out["h2_sharpe"] = stats.summarize(h2)["sharpe"] if len(h2) > 30 else float("nan")
    out["yearly"] = stats.yearly_returns(nav)
    return out


def carry_extra(r: CarryResult) -> dict[str, Any]:
    years = len(r.nav) / 365
    return {
        "hold_share": float(r.holding.any(axis=1).mean()),
        "avg_deployed": float(r.deployed.mean()),
        "total_cost": float(r.cost.sum()),
        "funding_income": float(r.funding.sum()),
        "entries_per_year": float((r.holding.astype(int).diff().abs().sum().sum()) / 2 / years),
        "liquidations": len(r.events),
    }


def trial_row(
    name: str,
    m: dict[str, Any],
    start: date,
    end: date,
    chash: str,
    overrides: dict[str, Any],
    **extra: float,
) -> dict[str, Any]:
    f = m["full"]
    return {
        "time": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_commit": git_commit(),
        "experiment": "R10",
        "variant": name,
        "config_hash": hashlib.sha1(json.dumps([chash, overrides], sort_keys=True).encode()).hexdigest()[:10],
        "overrides": json.dumps(overrides, ensure_ascii=False),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "cagr": f["cagr"],
        "vol": f["vol"],
        "sharpe": f["sharpe"],
        "mdd": f["mdd"],
        "calmar": f["calmar"],
        "h1_sharpe": m["h1_sharpe"],
        "h2_sharpe": m["h2_sharpe"],
        **extra,
    }


def fmt_row(name: str, m: dict[str, Any]) -> str:
    f, a, b = m["full"], m["A"], m["B"]
    return (
        f"| {name} | {f['cagr']:.1%} | {f['sharpe']:.2f} | {f['mdd']:.1%} | {f['calmar']:.2f} | "
        f"{a['cagr']:.1%} | {a['sharpe']:.2f} | {a['mdd']:.1%} | {b['cagr']:.1%} | {b['sharpe']:.2f} | {b['mdd']:.1%} |"
    )


def main() -> int:
    cfg = load_config(CONFIG)
    start, end, split = cfg.backtest.start, cfg.backtest.end, cfg.research.segment_split
    guard = HoldoutGuard(cfg.research.holdout_start)
    guard.check_end(end)
    warm = start - timedelta(days=WARMUP_DAYS)

    print(f"下载 / 读取永续数据：{warm} ～ {end}（只到研究截止日）")
    client = VisionClient(paths.RAW_DIR)
    funding = {s: load_funding(client, s, warm, end) for s in SYMBOLS}
    perp = {s: load_perp_daily(client, s, warm, end) for s in SYMBOLS}
    view = ResearchMarketView(MarketStore(paths.MARKET_DB), guard)
    spot = {s: view.read_symbol(s, start=warm, end=end)["close"] for s in SYMBOLS}
    for s in SYMBOLS:  # 防御性检查：没有任何保留集数据
        guard.assert_clean(perp[s])
        guard.assert_clean(funding[s].to_frame())
        guard.assert_clean(spot[s].to_frame())
        print(f"  {s}：资金费 {funding[s].index.min().date()} ～ {funding[s].index.max().date()}，"
              f"永续日线 {len(perp[s])} 根，现货日线 {len(spot[s])} 根")  # fmt: skip

    carry = {h: run_carry(spot, perp, funding, start, end, CarryParams(hurdle_annual=h)) for h in HURDLES}
    ctx = build_context(cfg, CONFIG, start, end)
    _, trend = ctx.run(Variant("trend_y4", {"backtest.cash_yield_annual": TREND_YIELD}))
    fee2 = {"fee": cfg.backtest.fee * 2, "slippage": cfg.backtest.slippage * 2}
    _, trend_c2 = ctx.run(Variant("trend_y4_cost_x2", {"backtest.cash_yield_annual": TREND_YIELD}, fee2))
    combos = {w: combine(trend.nav, carry[MAIN_HURDLE].nav, w) for w in WEIGHTS}
    carry_c2 = run_carry(
        spot, perp, funding, start, end, CarryParams(hurdle_annual=MAIN_HURDLE).with_costs_scaled(2)
    )
    combo_c2 = combine(trend.nav, carry_c2.nav, MAIN_W)
    combo_c2_all = combine(trend_c2.nav, carry_c2.nav, MAIN_W)

    m_carry = {h: metrics(r.nav, split) for h, r in carry.items()}
    m_trend = metrics(trend.nav.loc[combos[MAIN_W].index], split)
    m_combo = {w: metrics(n, split) for w, n in combos.items()}
    m_carry_c2, m_combo_c2, m_combo_c2_all = (
        metrics(carry_c2.nav, split),
        metrics(combo_c2, split),
        metrics(combo_c2_all, split),
    )

    chash = config_hash(cfg)
    for h, r in carry.items():
        ex = carry_extra(r)
        p = CarryParams(hurdle_annual=h)
        append_trial(TRIALS_FILE, trial_row(f"carry_{hname(h)}", m_carry[h], start, end, chash, {"sleeve": "carry", **vars(p)},
                                            avg_gross=ex["avg_deployed"], total_cost_ratio=ex["total_cost"]))  # fmt: skip
    for w in WEIGHTS:
        append_trial(TRIALS_FILE, trial_row(f"combo_w{round(w * 100)}", m_combo[w], start, end, chash,
                                            {"combo_carry_weight": w, "carry_hurdle": MAIN_HURDLE, "trend_cash_yield": TREND_YIELD}))  # fmt: skip
    append_trial(
        TRIALS_FILE,
        trial_row(
            "control_trend_y4", m_trend, start, end, chash, {"backtest.cash_yield_annual": TREND_YIELD}
        ),
    )

    head = "| 方案 | 全期年化 | 全期夏普 | 全期回撤 | 全期 Calmar | 段A年化 | 段A夏普 | 段A回撤 | 段B年化 | 段B夏普 | 段B回撤 |"
    sep = "|---" * 11 + "|"
    lines = [
        "# R10：资金费率套利袖子与组合",
        "",
        f'- 区间 {start} ～ {end}；段 A < {split} ≤ 段 B；git {git_commit()}；预注册见 doc/research/log.md "R10 预注册"',
        f"- 标的 {', '.join(SYMBOLS)}，各占套利袖子一半；永续杠杆 2；7 日均资金费（年化）高于门槛才持有",
        "",
        "## 套利袖子",
        "",
        head,
        sep,
        *[fmt_row(f"carry_{hname(h)}", m) for h, m in m_carry.items()],
        fmt_row("carry_h5（成本 ×2）", m_carry_c2),
        "",
        "| 方案 | 持有天数占比 | 平均部署比例 | 年均开平仓次数 | 累计成本 | 累计资金费收入 | 爆仓次数 |",
        "|---|---|---|---|---|---|---|",
        *[
            f"| carry_{hname(h)} | {e['hold_share']:.0%} | {e['avg_deployed']:.2f} | {e['entries_per_year']:.1f} | "
            f"{e['total_cost']:.2%} | {e['funding_income']:.1%} | {e['liquidations']} |"
            for h, e in ((h, carry_extra(r)) for h, r in carry.items())
        ],
        "",
        "## 组合（套利门槛 5%）",
        "",
        head,
        sep,
        fmt_row("对照：当前策略 + 闲置 4% 计息", m_trend),
        *[fmt_row(f"组合 w = {w}", m) for w, m in m_combo.items()],
        fmt_row("组合 w = 0.5（套利成本 ×2）", m_combo_c2),
        fmt_row("组合 w = 0.5（套利与趋势成本都 ×2，观察）", m_combo_c2_all),
        "",
        "## 分年度收益",
        "",
        "| 方案 | " + " | ".join(sorted(m_trend["yearly"])) + " |",
        "|---" * (1 + len(m_trend["yearly"])) + "|",
    ]
    for name, m in (("对照", m_trend), ("套利 h5", m_carry[MAIN_HURDLE]), ("组合 w = 0.5", m_combo[MAIN_W])):
        lines.append(
            f"| {name} | "
            + " | ".join(f"{m['yearly'].get(y, float('nan')):.1%}" for y in sorted(m_trend["yearly"]))
            + " |"
        )
    out = DOC_REPORTS_DIR / "R10"
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    fig, ax = plt.subplots(figsize=(11, 5.5))
    for label, nav in (("trend + 4% cash", trend.nav.loc[combos[MAIN_W].index]), ("carry h5", carry[MAIN_HURDLE].nav),
                       ("combo w=0.5", combos[MAIN_W])):  # fmt: skip
        ax.plot(nav.index, nav / nav.iloc[0], label=label)
    ax.set_yscale("log")
    ax.axvline(pd.Timestamp(split), color="grey", linestyle="--", linewidth=1)
    ax.legend()
    ax.set_title("R10 carry sleeve and combination")
    fig.tight_layout()
    fig.savefig(out / "equity.png", dpi=110)
    plt.close(fig)

    print("\n".join(lines[6:]))
    flags = [n for n, m in [*((f"carry_{hname(h)}", m) for h, m in m_carry.items()), *((f"combo_w{w}", m) for w, m in m_combo.items())]
             if m["full"]["sharpe"] > 3]  # fmt: skip
    if flags:
        print(f"\n注意：以下试验夏普 > 3，按 CLAUDE.md 第 4 条需要先排查再解释：{flags}")
    print(f"\n摘要：{out / 'summary.md'}；试验已追加到 {TRIALS_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
