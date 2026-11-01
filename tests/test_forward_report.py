"""前向运行周报 `forward`（D-068）：漏跑、执行质量、与研究引擎同期模拟的对比。"""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from bot.app import forward_report
from bot.app.forward_report import build_forward_report, cmd_forward
from bot.backtest.sim_env import ALTS, sim_config, synthetic_market
from bot.config import AppConfig, AssetsConfig
from bot.data.store import MarketStore
from bot.domain.types import Fill, Side
from bot.persistence.repo import StateRepo
from tests.conftest import ASSETS_PATH

D = Decimal
DAYS = [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 5)]  # 11-04 漏跑
ASSETS = AssetsConfig(approved_assets=["BTC", *ALTS])


def _at(d: date, hh: int = 0, mm: int = 10) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=UTC)


@pytest.fixture
def setup(tmp_path: Path, base_cfg: dict[str, Any]) -> tuple[AppConfig, StateRepo, MarketStore]:
    cfg = sim_config(base_cfg)
    store = MarketStore(tmp_path / "market.db")
    for sym, raw in synthetic_market(DAYS[-1]).items():
        store.upsert_klines(sym, raw, "vision")
    repo = StateRepo.open(tmp_path / "state.db")
    for d, navu in zip(DAYS, ("1.00", "1.01", "1.02"), strict=True):
        repo.append_nav_snapshot(
            strategy_id="mom1", account_id="spot-main", kind="PRE_TRADE", nav_usdt=D("1000") * D(navu),
            units=D("1000"), nav_per_unit=D(navu), gross_exposure=D("0.5"), now=_at(d),
        )  # fmt: skip
        # 同一天的第二个快照不应被重复计数
        repo.append_nav_snapshot(
            strategy_id="mom1", account_id="spot-main", kind="PRE_TRADE", nav_usdt=D("1"), units=D("1000"),
            nav_per_unit=D("0.5"), gross_exposure=D("0.5"), now=_at(d, 12),
        )  # fmt: skip

    def open_px(sym: str, d: date) -> Decimal:
        raw = store.read_raw(sym, d.isoformat(), (d + timedelta(days=1)).isoformat())
        return D(raw["open"].iloc[0])

    # 买单比开盘价贵 0.1%，手续费以 USDT 计；卖单比开盘价便宜（o/px − 1 = 0.2%），手续费以基础资产计
    buy_px = open_px("BTCUSDT", DAYS[0]) * D("1.001")
    buy = Fill(
        "BTCUSDT", 1, 11, Side.BUY, buy_px, D("1"), buy_px, buy_px * D("0.001"), "USDT", _at(DAYS[0], 0, 11)
    )
    sell_px = open_px("A1USDT", DAYS[1]) / D("1.002")
    sell = Fill(
        "A1USDT", 2, 12, Side.SELL, sell_px, D("10"), sell_px * 10, D("0.01"), "A1", _at(DAYS[1], 0, 11)
    )
    for f, base in ((buy, "BTC"), (sell, "A1")):
        repo.upsert_fills(
            [f], cid=None, strategy_id="mom1", account_id="spot-main", base_asset=base, quote_asset="USDT"
        )
    repo.append_event(
        "REGIME_SWITCH",
        "WARN",
        {"state": "RISK_ON"},
        strategy_id="mom1",
        account_id="spot-main",
        now=_at(DAYS[1]),
    )
    return cfg, repo, store


def test_report_counts_runs_costs_and_compares_with_engine(setup) -> None:  # type: ignore[no-untyped-def]
    cfg, repo, store = setup
    text = build_forward_report(cfg, repo, store, ASSETS)
    assert "2026-11-02 ～ 2026-11-05" in text
    assert "快照天数 3 / 应运行 4 天；漏跑：2026-11-04" in text
    assert '"REGIME_SWITCH": 1' in text
    assert "成交 2 笔" in text
    assert "实际手续费率 0.1000%" in text  # 两笔都按成交额的 0.1% 收费
    assert "平均偏移 0.1500%" in text  # (0.1% + 0.2%) / 2
    assert "前向单位净值累计收益 2.00%" in text  # 只取每天第一个快照
    assert "## 与研究引擎同期模拟对比" in text and "日收益相关系数" in text


def test_since_filters_snapshots_fills_and_events(setup) -> None:  # type: ignore[no-untyped-def]
    cfg, repo, store = setup
    text = build_forward_report(cfg, repo, store, ASSETS, since=DAYS[1])
    assert "快照天数 2 / 应运行 3 天" in text
    assert "成交 1 笔" in text
    later = build_forward_report(cfg, repo, store, ASSETS, since=date(2026, 12, 1))
    assert "2026-12-01 之后还没有任何 PRE_TRADE 快照" in later


def test_cmd_forward_writes_report_without_snapshots(
    tmp_path: Path,
    base_cfg: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)  # 状态库、行情库、报告目录都是相对路径
    base_cfg["universe"]["assets_file"] = str(ASSETS_PATH)
    cfg = sim_config(base_cfg)
    monkeypatch.setattr(forward_report, "FORWARD_DIR", tmp_path / "doc" / "forward")
    assert cmd_forward(cfg, argparse.Namespace(since=None)) == 0
    [out] = list((tmp_path / "doc" / "forward").iterdir())
    assert out.name.startswith(f"forward_{cfg.env.value}_")
    assert "还没有任何 PRE_TRADE 快照" in out.read_text(encoding="utf-8")
    assert "已写入" in capsys.readouterr().out
