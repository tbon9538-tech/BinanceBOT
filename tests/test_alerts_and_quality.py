"""告警推送（spec §12）、漏跑检查、数据质量检查（spec §3.3）、大盘重放核对（spec §4.2）、日报内容。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from bot.app.daemon import run_daemon
from bot.app.run_once import DailyRunner
from bot.backtest.sim_env import build_sim_deps, sim_config
from bot.data.klines import DAY_MS
from bot.data.quality import drop_invalid_bars, invalid_bar_dates, missing_days, price_jumps
from bot.domain.types import SymbolStatus

D = Decimal
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
    deps = replace(deps, notifier=inbox)
    return deps, acct, market, clock, inbox


def _next_day(market, clock) -> None:  # type: ignore[no-untyped-def]
    """给模拟行情补上当前运行日那一根日线（价格不变），再把时钟推进一天，使下一次运行能拿到新的 t 日数据。"""
    new_t = clock.t.date()
    for sym, df in market.frames.items():
        last = df.iloc[-1].copy()
        last["date"] = new_t.isoformat()
        last["open_time_ms"] = int(last["open_time_ms"]) + DAY_MS
        last["close_time_ms"] = int(last["close_time_ms"]) + DAY_MS
        market.frames[sym] = pd.concat([df, last.to_frame().T], ignore_index=True)
    clock.t += timedelta(days=1)


def _bars(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    idx = pd.date_range("2026-10-01", periods=len(rows), freq="D")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


# ---- 纯函数 ----


def test_invalid_bars_and_missing_days() -> None:
    df = _bars([(10, 11, 9, 10.5), (10, 9.5, 9, 9.2), (10, 11, 0, 10), (10, 11, 10.2, 10.5), (10, 11, 9, 10)])
    assert invalid_bar_dates(df) == [
        date(2026, 10, 2),
        date(2026, 10, 3),
        date(2026, 10, 4),
    ]  # high 太低、low=0、low 太高
    clean, bad = drop_invalid_bars(df)
    assert len(clean) == 2 and len(bad) == 3
    assert missing_days(clean, date(2026, 10, 5), 5) == [
        date(2026, 10, 2),
        date(2026, 10, 3),
        date(2026, 10, 4),
    ]
    assert missing_days(df, date(2026, 10, 5), 5) == []


def test_price_jumps() -> None:
    close = pd.DataFrame(
        {"A": [10.0, 17.0], "B": [10.0, 10.5], "C": [10.0, 3.0]}, index=pd.date_range("2026-10-01", periods=2)
    )
    assert price_jumps(close, close.index[1], 0.6) == {"A": pytest.approx(0.7), "C": pytest.approx(-0.7)}
    assert price_jumps(close, close.index[0], 0.6) == {}


# ---- 告警推送 ----


def test_executor_warn_events_are_pushed(env) -> None:  # type: ignore[no-untyped-def]
    deps, acct, _, _, inbox = env
    acct.inject_place_fault("timeout", executed=False, symbol="BTCUSDT", times=10)  # 一直查不到
    assert DailyRunner(deps).run_once().status == "DONE"
    [(level, text)] = inbox.of("ORDER_STATUS_UNKNOWN")[:1]
    assert level == "WARN" and "BTCUSDT" in text


def test_unexplained_change_and_held_symbol_not_trading_are_pushed(env) -> None:  # type: ignore[no-untyped-def]
    deps, acct, market, clock, inbox = env
    assert DailyRunner(deps).run_once().status == "DONE"
    held = next(a for a, b in acct.get_balances().items() if a not in ("USDT", "BTC") and b.total > 0)
    acct.adjust_balance("USDT", D("-50"))  # 无法解释的余额变化
    sym = f"{held}USDT"
    acct.rules[sym] = replace(acct.rules[sym], status=SymbolStatus.BREAK)  # 持仓币停止交易
    _next_day(market, clock)
    DailyRunner(deps).run_once()
    assert [lv for lv, _ in inbox.of("UNEXPLAINED_BALANCE_CHANGE")] == ["WARN"]
    [(level, text)] = inbox.of("HELD_SYMBOL_NOT_TRADING")
    assert level == "CRITICAL" and sym in text


# ---- 漏跑 ----


def test_missed_run_alerts_once_per_gap(env) -> None:  # type: ignore[no-untyped-def]
    deps, _, _, clock, inbox = env
    runner = DailyRunner(deps)
    assert runner.check_missed_run() is False  # 从未成功运行过
    assert runner.run_once().status == "DONE"
    clock.t += timedelta(hours=29)
    assert runner.check_missed_run() is False
    clock.t += timedelta(hours=2)  # 距上次成功运行 31 小时
    assert runner.check_missed_run() is True
    assert runner.check_missed_run() is False  # 同一段空档只告警一次
    assert len(inbox.of("MISSED_RUN")) == 1
    clock.t += timedelta(days=1)
    runner.run_once()  # 运行开始时的检查不再重复告警；运行成功后空档结束
    assert len(inbox.of("MISSED_RUN")) == 1
    assert runner.check_missed_run() is False


def test_daemon_checks_missed_run_while_waiting(env) -> None:  # type: ignore[no-untyped-def]
    deps, _, _, clock, inbox = env
    DailyRunner(deps).run_once()
    clock.t = datetime(2026, 11, 3, 8, 0, tzinfo=UTC) + timedelta(
        days=1
    )  # 上次成功运行之后约 56 小时，离下次运行还有十几个小时
    deps.kill_file.write_text("x")  # 让常驻进程在第一次等待检查后退出
    run_daemon(DailyRunner(deps), "00:10", now=clock.now, sleep=clock.sleep)
    assert len(inbox.of("MISSED_RUN")) == 1


# ---- 数据质量 ----


def test_btc_bad_bar_in_sma_window_aborts_run(env) -> None:  # type: ignore[no-untyped-def]
    deps, acct, market, _, inbox = env
    btc = market.frames["BTCUSDT"].copy()
    i = len(btc) - 10
    btc.loc[btc.index[i], "low"] = str(float(btc["high"].iloc[i]) * 2)  # low > high：无效日线
    market.frames["BTCUSDT"] = btc
    r = DailyRunner(deps).run_once()
    assert r.status == "SKIPPED" and acct.placed == []
    [ev] = [e for e in deps.repo.list_events("mom1", type_="DATA_NOT_READY") if '"reason":' in e.payload_json]
    assert "缺少 1 天" in ev.payload_json
    assert inbox.of("缺少 1 天")


# ---- 大盘重放核对 ----


def test_regime_replay_mismatch_is_reported(env) -> None:  # type: ignore[no-untyped-def]
    deps, _, market, clock, inbox = env
    DailyRunner(deps).run_once()
    [row] = deps.repo.list_regime_states("mom1")
    flipped = "RISK_OFF" if row.state == "RISK_ON" else "RISK_ON"
    deps.repo.upsert_regime_state(
        "mom1", row.date, symbol=row.symbol, close=row.close, sma=row.sma, state=flipped, switched=False
    )
    _next_day(market, clock)
    DailyRunner(deps).run_once()
    [ev] = deps.repo.list_events("mom1", type_="REGIME_REPLAY_MISMATCH")
    assert flipped in ev.payload_json and len(inbox.of("REGIME_REPLAY_MISMATCH")) == 1
    assert deps.repo.get_regime_state("mom1", row.date).state == flipped  # 旧记录保留，只告警
    assert deps.repo.list_regime_states("mom1")[-1].date > row.date  # 新的运行写入当天的重放结果


# ---- 日报 ----


def test_daily_report_has_required_fields(env) -> None:  # type: ignore[no-untyped-def]
    deps, _, market, clock, inbox = env
    DailyRunner(deps).run_once()
    _next_day(market, clock)
    DailyRunner(deps).run_once()
    report = [tx for lv, tx in inbox.sent if "日报" in tx][-1]
    for part in ("单位净值", "当日", "回撤", "核心", "卫星", "大盘", "持仓", "成交额", "手续费"):
        assert part in report, part
