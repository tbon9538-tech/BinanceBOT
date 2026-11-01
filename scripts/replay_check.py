"""执行回放一致性检查（dev_plan M6-6）。只使用研究集数据。

用法（项目根目录）：  uv run python scripts/replay_check.py 2025-03-01 2025-05-31
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import paths
from bot.backtest.replay import run_replay
from bot.config import load_assets, load_config
from bot.data.holdout import HoldoutGuard
from bot.data.store import MarketStore


def main() -> int:
    start, end = date.fromisoformat(sys.argv[1]), date.fromisoformat(sys.argv[2])
    cfg_path = Path("config/config.yaml")
    cfg = load_config(cfg_path)
    HoldoutGuard(cfg.research.holdout_start).check_end(end)
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        res = run_replay(
            raw, MarketStore(paths.MARKET_DB), load_assets(cfg.universe.assets_file), start, end, Path(d)
        )
    print(f"区间 {start} ～ {end}：回放订单 {res.orders} 笔")
    print(
        f"期末净值：回放 {res.replay_nav.iloc[-1]:.4f}，研究引擎 {res.engine_nav.iloc[-1]:.4f}，差异 {res.final_rel_diff:.2%}"
    )
    print(f"逐日最大差异 {res.max_rel_diff:.2%}（验收标准：期末差异 < 1%）")
    return 0 if res.final_rel_diff < 0.01 else 1


if __name__ == "__main__":
    sys.exit(main())
