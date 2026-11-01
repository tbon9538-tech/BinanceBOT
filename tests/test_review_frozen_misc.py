"""D-082 的修复：持仓冻结的完整周期、强制退出在排名阶段排除、待审核资产表与逐资产提醒、
实际费率检查、策略登记、扣除 USDT 理财收益的夏普、版本号一致。"""

from __future__ import annotations

import tomllib
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import select

from bot import __version__
from bot.app.run_once import DailyRunner
from bot.backtest import stats
from bot.backtest.sim_env import ALTS, build_sim_deps, sim_config
from bot.config import AssetsConfig
from bot.data.klines import DAY_MS, to_float_frame
from bot.domain.types import Side, SymbolStatus
from bot.persistence.models import StrategyRow
from bot.strategy.indicators import MarketPanel
from bot.strategy.momentum_v1 import MomentumV1

RUN_DATE = date(2026, 11, 2)


class Inbox:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, text: str, level: str = "INFO") -> bool:
        self.sent.append((level, text))
        return False

    def of(self, kind: str) -> list[tuple[str, str]]:
        return [(lv, tx) for lv, tx in self.sent if kind in tx]


@pytest.fixture
def env(tmp_path: Path, base_cfg: dict[str, Any]):  # type: ignore[no-untyped-def]
    deps, acct, market, clock = build_sim_deps(sim_config(base_cfg), tmp_path, RUN_DATE)
    inbox = Inbox()
    return replace(deps, notifier=inbox), acct, market, clock, inbox


def _next_day(market, clock) -> None:  # type: ignore[no-untyped-def]
    new_t = clock.t.date()
    for sym, df in market.frames.items():
        last = df.iloc[-1].copy()
        last["date"] = new_t.isoformat()
        last["open_time_ms"] = int(last["open_time_ms"]) + DAY_MS
        last["close_time_ms"] = int(last["close_time_ms"]) + DAY_MS
        market.frames[sym] = pd.concat([df, last.to_frame().T], ignore_index=True)
    clock.t += timedelta(days=1)


def test_frozen_position_lifecycle(env) -> None:  # type: ignore[no-untyped-def]
    deps, acct, market, clock, inbox = env
    assert DailyRunner(deps).run_once().status == "DONE"
    held = next(a for a, b in acct.get_balances().items() if a not in ("USDT", "BTC") and b.total > 0)
    sym = f"{held}USDT"
    acct.rules[sym] = replace(acct.rules[sym], status=SymbolStatus.BREAK)
    for _ in range(2):  # 连续两天不可交易：只在冻结当天告警一次
        _next_day(market, clock)
        DailyRunner(deps).run_once()
    pos = deps.repo.get_position("spot-main", "mom1", held)
    assert pos is not None and pos.frozen and pos.qty > 0
    assert len(inbox.of("HELD_SYMBOL_NOT_TRADING")) == 1
    assert any("冻结" in tx for lv, tx in inbox.sent if "日报" in tx)
    acct.rules[sym] = replace(acct.rules[sym], status=SymbolStatus.TRADING)  # 恢复交易：自动卖出并解冻
    n = len(acct.placed)
    _next_day(market, clock)
    DailyRunner(deps).run_once()
    assert any(o.symbol == sym and o.side is Side.SELL for o in acct.placed[n:])
    pos = deps.repo.get_position("spot-main", "mom1", held)
    assert pos is not None and not pos.frozen
    assert deps.repo.list_events("mom1", type_="HELD_SYMBOL_UNFROZEN")


def test_force_exit_is_excluded_at_ranking(env) -> None:  # type: ignore[no-untyped-def]
    deps, *_ = env
    frames = {s: to_float_frame(raw) for s, raw in deps.market.frames.items()}  # type: ignore[attr-defined]
    panel = MarketPanel.from_frames(frames)
    t = panel.dates[-1]
    plain = MomentumV1(deps.cfg, panel).universe(t, ("BTCUSDT",))
    assert "A1USDT" in plain
    excl = MomentumV1(deps.cfg, panel, exclude_symbols={"A1USDT"}).universe(t, ("BTCUSDT",))
    assert "A1USDT" not in excl and set(excl) == set(plain) - {"A1USDT"}


def test_pending_assets_recorded_and_reminded_per_asset(env) -> None:  # type: ignore[no-untyped-def]
    deps, acct, market, clock, inbox = env
    deps = replace(deps, assets=AssetsConfig(approved_assets=["BTC", *ALTS[:-1]]))  # A6 未审核
    DailyRunner(deps).run_once()
    rows = deps.repo.asset_reviews()
    assert set(rows) == {"A6"} and rows["A6"].status == "NOTIFIED"
    assert len(inbox.of("待审核")) == 1 and "A6" in inbox.of("待审核")[0][1]
    assert not any(o.symbol == "A6USDT" for o in acct.placed)  # 待审核资产不参与交易
    _next_day(market, clock)
    DailyRunner(deps).run_once()
    assert len(inbox.of("待审核")) == 1  # 7 天内不重复提醒
    for _ in range(7):
        _next_day(market, clock)
    DailyRunner(deps).run_once()
    assert len(inbox.of("待审核")) == 2
    deps = replace(deps, assets=AssetsConfig(approved_assets=["BTC", *ALTS]))  # 人工审核通过后从表中删除
    _next_day(market, clock)
    DailyRunner(deps).run_once()
    assert deps.repo.asset_reviews() == {}


def test_commission_check_and_strategy_registration(env, base_cfg) -> None:  # type: ignore[no-untyped-def]
    deps, *_ = env
    base_cfg["backtest"]["fee"] = 0.0001  # 回测假设低于模拟交易所的实际费率
    deps = replace(deps, cfg=sim_config(base_cfg))
    r = DailyRunner(deps).run_once()
    assert any("实际 taker 费率" in m for m in r.messages)
    with deps.repo._session() as s:
        [row] = list(s.scalars(select(StrategyRow)))
    assert (row.strategy_id, row.version) == ("mom1", __version__) and len(row.config_hash) == 10


def test_sharpe_net_of_usdt_yield() -> None:
    r = pd.Series([0.01, -0.005, 0.004, 0.002, -0.001] * 20)
    assert stats.sharpe(r, 0.0) == stats.sharpe(r)
    assert stats.sharpe(r, 0.04) < stats.sharpe(r)


def test_version_matches_pyproject() -> None:
    root = Path(__file__).resolve().parents[1]
    assert (
        tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        == __version__
    )
