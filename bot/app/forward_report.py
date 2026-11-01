"""demo / live 前向运行周报（dev_plan M7-3，Q14）：`python -m bot forward [--since YYYY-MM-DD]`。

报告内容：
- 运行情况：应运行天数、实际快照天数、漏跑日期、失败 / 跳过次数；
- 执行质量：成交笔数、实际手续费率、成交价相对当日开盘价的偏移（回测假设：手续费 0.1% + 滑点 0.05%）；
- 与研究引擎同期模拟的对比：同一资产集合、同一配置，从首个快照前一天开始模拟；
  前向账户的单位净值包含超出 max_capital_usdt 的闲置 USDT，所以同时给出"按部署比例折算"的引擎收益。
前向数据是保留集之后的新数据，只用于观察与检验，不用于调参（D-018）。
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd

from bot import paths
from bot.backtest.engine import EngineOptions, run_backtest
from bot.config import AppConfig, load_assets
from bot.data.store import MarketStore
from bot.persistence.repo import StateRepo
from bot.strategy.indicators import MarketPanel, column_symbol
from bot.strategy.momentum_v1 import MomentumV1

FORWARD_DIR = Path("doc/forward")


def _daily_navu(repo: StateRepo, cfg: AppConfig) -> pd.Series:
    snaps = repo.list_nav_snapshots(cfg.strategy_id, cfg.account_id, kind="PRE_TRADE")
    s = pd.Series({pd.Timestamp(r.time.date()): float(r.nav_per_unit) for r in snaps})
    return s[~s.index.duplicated(keep="first")].sort_index()


def build_forward_report(cfg: AppConfig, since: date | None = None) -> str:
    repo = StateRepo.open(paths.state_db(cfg.env.value))
    store = MarketStore(paths.MARKET_DB)
    navu = _daily_navu(repo, cfg)
    if navu.empty:
        return f"# 前向运行周报（{cfg.env.value}）\n\n还没有任何 PRE_TRADE 快照。\n"
    if since is not None:
        navu = navu[navu.index >= pd.Timestamp(since)]
    start, end = navu.index[0].date(), navu.index[-1].date()
    expected = pd.date_range(start, end, freq="D")
    missed = [d.date().isoformat() for d in expected if d not in navu.index]
    events = repo.list_events(cfg.strategy_id)
    ev_counts: dict[str, int] = {}
    for e in events:
        if since is None or e.time.date() >= since:
            ev_counts[e.type] = ev_counts.get(e.type, 0) + 1

    # ---- 执行质量 ----
    fills = [
        f for f in repo.list_fills(cfg.strategy_id, cfg.account_id) if since is None or f.time.date() >= since
    ]
    quote = sum((f.quote_qty for f in fills), Decimal(0))
    fee_usdt = Decimal(0)
    slips: list[float] = []
    for f in fills:
        if f.commission_asset == f.quote_asset:
            fee_usdt += f.commission
        elif f.commission_asset == f.base_asset:
            fee_usdt += f.commission * f.price
        raw = store.read_raw(
            f.symbol, f.time.date().isoformat(), (f.time.date() + timedelta(days=1)).isoformat()
        )
        if not raw.empty:
            o = float(raw["open"].iloc[0])
            px = float(f.price)
            slips.append((px / o - 1) if f.side == "BUY" else (o / px - 1))
    fee_rate = float(fee_usdt / quote) if quote > 0 else float("nan")
    slip = sum(slips) / len(slips) if slips else float("nan")

    # ---- 研究引擎同期模拟 ----
    assets = load_assets(cfg.universe.assets_file)
    allowed = {f"{a}USDT" for a in assets.approved_assets}
    syms = sorted(allowed | {cfg.regime.symbol})
    end_excl = (end + timedelta(days=1)).isoformat()
    frames = {s: store.read_symbol(s, end_exclusive=end_excl) for s in syms}
    frames = {s: f for s, f in frames.items() if not f.empty}
    panel = MarketPanel.from_frames(frames, gap_days=cfg.universe.segment_gap_days)
    strat = MomentumV1(cfg, panel, asset_ok=lambda c: column_symbol(c) in allowed)
    first_nav = repo.list_nav_snapshots(cfg.strategy_id, cfg.account_id, kind="PRE_TRADE")[0].nav_usdt
    deployed = float(min(first_nav, Decimal(cfg.max_capital_usdt)) / first_nav) if first_nav > 0 else 1.0
    lines = [
        f"# 前向运行周报（{cfg.env.value}，{start} ～ {end}）",
        "",
        "> 前向数据只用于观察与检验，不用于调参（D-018）。",
        "",
        "## 运行情况",
        "",
        f"- 快照天数 {len(navu)} / 应运行 {len(expected)} 天；漏跑：{', '.join(missed) or '无'}",
        f"- 事件统计：{json.dumps(ev_counts, ensure_ascii=False) if ev_counts else '无'}",
        "",
        "## 执行质量",
        "",
        f"- 成交 {len(fills)} 笔，成交额 {float(quote):.2f} USDT",
        f"- 实际手续费率 {fee_rate:.4%}（回测假设 0.1000%）",
        f"- 成交价相对当日开盘价的平均偏移 {slip:.4%}（回测假设滑点 0.05%；正数表示比开盘价更差）",
    ]
    if len(navu) >= 2:
        eng = run_backtest(
            cfg,
            panel,
            start - timedelta(days=1),
            end,
            opts=EngineOptions.from_config(cfg, initial_capital=1000.0),
            strategy=strat,
        )
        eng_nav = eng.nav.reindex(navu.index)
        eng_nav = eng_nav / eng_nav.iloc[0]
        f_ret = navu.iloc[-1] / navu.iloc[0] - 1
        e_ret = float(eng_nav.iloc[-1] - 1)
        corr = navu.pct_change().corr(eng_nav.pct_change())
        lines += [
            "",
            "## 与研究引擎同期模拟对比",
            "",
            f"- 前向单位净值累计收益 {f_ret:.2%}",
            f"- 研究引擎累计收益 {e_ret:.2%}；按部署比例 {deployed:.0%} 折算后 {e_ret * deployed:.2%}",
            f"- 日收益相关系数 {corr:.2f}（样本 {len(navu) - 1} 天）",
            "- 偏差主要来源：资金上限之外的闲置 USDT、最小下单额与取整、实际成交价、漏跑。",
        ]
    return "\n".join(lines) + "\n"


def cmd_forward(cfg: AppConfig, args: argparse.Namespace) -> int:
    since = date.fromisoformat(args.since) if args.since else None
    text = build_forward_report(cfg, since)
    FORWARD_DIR.mkdir(parents=True, exist_ok=True)
    out = FORWARD_DIR / f"forward_{cfg.env.value}_{date.today().isoformat()}.md"
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"已写入 {out}")
    return 0
