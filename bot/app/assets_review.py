"""`python -m bot assets review`：列出需要人工审核的资产（spec §2.2 第 3 条，dev_plan M1-6）。

输出：
- 终端：待审核资产摘要；
- doc/assets_review/review_<日期>.md：全部候选资产的规则判定与辅助信息；
- doc/assets_review/assets.draft.yaml：建议的审核结果草稿。程序不会修改 config/assets.yaml，
  审核结果须由人工编辑 config/assets.yaml 生效。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from bot.config import AssetsConfig, UniverseConfig
from bot.data.segments import current_segment
from bot.data.store import MarketStore
from bot.strategy.universe import (
    AssetClass,
    annualized_vol,
    classify_asset,
    liquidity_stats,
    passes_liquidity,
    suspected_equity_token,
    suspected_stablecoin,
    tradable_usdt_symbols,
    weekend_move_ratio,
)


@dataclass(frozen=True)
class ReviewResult:
    table: pd.DataFrame  # 全部 TRADING 的 USDT 交易对
    as_of: date
    stale_symbols: list[str]  # 本地数据没有更新到 as_of 的交易对


def build_review(
    store: MarketStore,
    exchange_info: Mapping[str, Any],
    assets: AssetsConfig,
    universe: UniverseConfig,
    as_of: date,
) -> ReviewResult:
    rows = []
    stale = []
    for sym, base in sorted(tradable_usdt_symbols(exchange_info).items()):
        df = store.read_symbol(
            sym, end_exclusive=(pd.Timestamp(as_of) + pd.Timedelta(days=1)).date().isoformat()
        )
        seg = current_segment(df, universe.segment_gap_days)
        if seg.empty or seg.index[-1].date() < as_of:
            stale.append(sym)
        stats = (
            liquidity_stats(seg, as_of, volume_median_days=universe.volume_median_days)
            if not seg.empty
            else None
        )
        ok = passes_liquidity(
            stats,
            min_listing_days=universe.min_listing_days,
            min_median_quote_volume=universe.min_median_quote_volume,
        )
        rows.append(
            {
                "symbol": sym,
                "base": base,
                "class": classify_asset(base, assets).value,
                "listing_date": stats.listing_date.isoformat() if stats else None,
                "listing_age": stats.listing_age if stats else None,
                "medqv_30d_musd": round(stats.median_quote_volume / 1e6, 2)
                if stats and stats.median_quote_volume is not None
                else None,
                "ann_vol_90d": round(v, 3) if (v := annualized_vol(seg, as_of)) is not None else None,
                "weekend_ratio": round(w, 2) if (w := weekend_move_ratio(seg, as_of)) is not None else None,
                "suspected_stable": suspected_stablecoin(seg, as_of) if not seg.empty else False,
                "suspected_equity": suspected_equity_token(seg, as_of, base) if not seg.empty else False,
                "passes_liquidity": ok,
            }
        )
    table = pd.DataFrame(rows)
    if not table.empty:
        passing = table[table["passes_liquidity"]].sort_values("medqv_30d_musd", ascending=False)
        table["liquidity_rank"] = pd.Series(range(1, len(passing) + 1), index=passing.index)
    return ReviewResult(table=table, as_of=as_of, stale_symbols=stale)


def pending_review(result: ReviewResult) -> pd.DataFrame:
    """通过流动性筛选、但既不在 approved 也不在 excluded 中的资产。"""
    t = result.table
    if t.empty:
        return t
    return t[(t["class"] == AssetClass.PENDING.value) & t["passes_liquidity"]].sort_values("liquidity_rank")


def draft_assets(result: ReviewResult, current: AssetsConfig) -> dict[str, Any]:
    """建议草稿：疑似股票/ETF 与疑似稳定币建议排除，其余待审核资产建议批准。仅供人工参考。"""
    pend = pending_review(result)
    equity = sorted(pend.loc[pend["suspected_equity"], "base"])
    stable = sorted(pend.loc[pend["suspected_stable"] & ~pend["suspected_equity"], "base"])
    approve = sorted(set(pend["base"]) - set(equity) - set(stable))
    excluded = {k: list(v) for k, v in current.excluded_assets.items()}
    excluded.setdefault("tokenized_stocks_etf", [])
    excluded["tokenized_stocks_etf"] = sorted(set(excluded["tokenized_stocks_etf"]) | set(equity))
    excluded.setdefault("stablecoins_fiat", [])
    excluded["stablecoins_fiat"] = sorted(set(excluded["stablecoins_fiat"]) | set(stable))
    return {
        "approved_assets": sorted(set(current.approved_assets) | set(approve)),
        "excluded_assets": excluded,
        "excluded_patterns": list(current.excluded_patterns),
        "pattern_exceptions": list(current.pattern_exceptions),
    }


def _md_table(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        lines.append("| " + " | ".join("" if pd.isna(r[c]) else str(r[c]) for c in cols) + " |")
    return "\n".join(lines)


def write_outputs(result: ReviewResult, current: AssetsConfig, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    day = result.as_of.isoformat()
    md_path = out_dir / f"review_{day}.md"
    draft_path = out_dir / "assets.draft.yaml"
    t = result.table
    pend = pending_review(result)
    pattern_hits = t[t["class"] == AssetClass.EXCLUDED_PATTERN.value] if not t.empty else t
    cols = [
        "liquidity_rank", "symbol", "base", "class", "listing_date", "listing_age", "medqv_30d_musd",
        "ann_vol_90d", "weekend_ratio", "suspected_stable", "suspected_equity",
    ]  # fmt: skip
    passing = t[t["passes_liquidity"]].sort_values("liquidity_rank")[cols] if not t.empty else t
    n_pass = int(t["passes_liquidity"].sum()) if not t.empty else 0
    md = [
        f"# 资产审核清单（数据截至 {day}）",
        "",
        "> 由 `python -m bot assets review` 生成。程序不会修改 `config/assets.yaml`；"
        "请人工审核后编辑该文件。",
        "> 辅助标记只用于提示：`suspected_equity` = 周末平均波动不到工作日的 35%（疑似代币化股票/ETF），"
        "`suspected_stable` = 90 日年化波动 < 5% 且价格接近 1。",
        "",
        f"- TRADING 的 USDT 交易对：{len(t)} 个；通过流动性与上市天数筛选：{n_pass} 个；"
        f"其中待审核：{len(pend)} 个。",
        f"- 本地数据未更新到 {day} 的交易对：{len(result.stale_symbols)} 个"
        "（先运行 `python -m bot data sync`）。",
        "",
        "## 被正则命中的资产（确认是否为杠杆代币；误伤的请加入 pattern_exceptions）",
        "",
        _md_table(pattern_hits[["symbol", "base", "listing_date", "medqv_30d_musd"]])
        if not pattern_hits.empty
        else "（无）",
        "",
        "## 通过流动性筛选的全部资产（按成交额排名）",
        "",
        _md_table(passing) if not passing.empty else "（无）",
        "",
    ]
    md_path.write_text("\n".join(md), encoding="utf-8")
    header = (
        "# 资产审核草稿（自动生成，仅供参考）。审核后由人工把结果写入 config/assets.yaml。\n"
        f"# 数据截至 {day}。规则：疑似股票/ETF、疑似稳定币建议排除；\n"
        "# 其余通过流动性筛选的待审核资产建议批准。\n"
    )
    draft_path.write_text(
        header + yaml.safe_dump(draft_assets(result, current), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return md_path, draft_path
