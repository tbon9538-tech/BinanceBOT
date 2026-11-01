"""短线研究（R11）：5 分钟数据加载、成交时点与成本、网格成交规则。不访问网络。"""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import date
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest

from bot.backtest.intraday import (
    Costs,
    breakout_signals,
    dip_signals,
    run_grid,
    run_signals,
    win_day_ratio,
)
from bot.data.intraday import ChecksumMismatch, load_5m, parse_csv

FREE = Costs(taker_fee=0.0, taker_slippage=0.0, maker_fee=0.0, maker_trade_through=0.0)


def _bars(closes: list[float], *, start: str = "2024-01-01", spread: float = 0.0) -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(closes), freq="5min")
    c = np.array(closes, dtype=float)
    return pd.DataFrame({"open": c, "high": c * (1 + spread), "low": c * (1 - spread), "close": c}, index=idx)


def test_signal_executes_next_open_and_charges_costs() -> None:
    bars = _bars([100, 100, 110, 110, 120])
    bars.loc[bars.index[2], "open"] = 105.0  # 第 2 根开盘价与收盘价不同
    enter = np.array([True, False, False, False, False])
    exit_ = np.array([False, True, False, False, False])
    r = run_signals(bars, enter, exit_, capital=1000, costs=FREE)
    # 第 0 根收盘发出买入 → 第 1 根开盘 100 买入；第 1 根收盘发出卖出 → 第 2 根开盘 105 卖出
    assert r.trades == 2 and r.equity.iloc[-1] == pytest.approx(1050.0)
    costs = Costs(taker_fee=0.001, taker_slippage=0.0005)
    rc = run_signals(_bars([100] * 5), enter, exit_, capital=1000, costs=costs)
    expected = 1000 * (1 - 0.001) / 1.0005 * 0.9995 * (1 - 0.001)
    assert rc.equity.iloc[-1] == pytest.approx(expected)
    assert rc.fees + rc.slippage == pytest.approx(1000 - expected)
    assert rc.gross_pnl == pytest.approx(0.0, abs=1e-9)


def test_max_hold_forces_exit() -> None:
    bars = _bars([100] * 10)
    enter = np.zeros(10, dtype=bool)
    enter[0] = True
    r = run_signals(bars, enter, np.zeros(10, dtype=bool), capital=1000, costs=FREE, max_hold=3)
    assert r.trades == 2 and r.bars_in_market == 3


def test_signals_do_not_look_ahead() -> None:
    rng = np.random.default_rng(1)
    bars = _bars(list(100 * np.cumprod(1 + rng.normal(0, 0.003, 400))), spread=0.001)
    e1, x1 = dip_signals(bars, 2.5, 12)
    b1, s1 = breakout_signals(bars, 24 * 12 // 4)
    future = bars.copy()
    future.iloc[300:] *= 1.5  # 改动第 300 根之后的数据
    e2, x2 = dip_signals(future, 2.5, 12)
    b2, s2 = breakout_signals(future, 24 * 12 // 4)
    for a, b in ((e1, e2), (x1, x2), (b1, b2), (s1, s2)):
        assert (a[:300] == b[:300]).all()


def test_grid_round_trip_profit_and_trade_through() -> None:
    step = 0.01
    # 锚点 100：第 1 档买入价 99，卖出价 99.99
    swing = [100, 98.9, 100.1, 98.9, 100.1]
    bars = _bars(swing, spread=0.0)
    bars["high"], bars["low"] = bars["close"], bars["close"]
    r = run_grid(bars, step=step, capital=1000, costs=FREE, unit_frac=0.1, max_units=10)
    assert r.trades == 4  # 两买两卖
    per_cycle = 100 * (1.01 - 1)  # 每份 100 USDT 赚 1%
    assert r.equity.iloc[-1] == pytest.approx(1000 + 2 * per_cycle, rel=1e-6)
    # 要求穿过挂单价 0.05%：只碰到 99.0 不成交
    touch = _bars([100, 99.0, 100.2])
    touch["high"], touch["low"] = touch["close"], touch["close"]
    strict = Costs(taker_fee=0.0, taker_slippage=0.0, maker_fee=0.0, maker_trade_through=0.0005)
    assert run_grid(touch, step=step, capital=1000, costs=strict).trades == 0


def test_grid_anchor_follows_price_when_flat() -> None:
    bars = _bars([100, 110, 120, 118.7])  # 一直上涨后小幅回落 1.1%
    bars["high"], bars["low"] = bars["close"], bars["close"]
    r = run_grid(bars, step=0.01, capital=1000, costs=FREE)
    assert r.trades == 1  # 空仓时锚点跟到 120，回落到 118.8 以下时买入第一档


def test_win_day_ratio() -> None:
    nav = pd.Series([1.0, 1.01, 1.01, 1.0, 1.02], index=pd.date_range("2024-01-01", periods=5))
    assert win_day_ratio(nav) == pytest.approx(0.5)


# ---- 数据加载 ----


def _zip_csv(rows: list[str], header: bool) -> bytes:
    text = (
        "open_time,open,high,low,close,volume,close_time,quote_volume,count,tbv,tbqv,ignore\n"
        if header
        else ""
    )
    text += "\n".join(rows) + "\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", text)
    return buf.getvalue()


def test_parse_csv_handles_header_and_microseconds() -> None:
    ms = 1735689600000  # 2025-01-01 00:00 UTC
    rows = [f"{ms * 1000},1,2,0.5,1.5,10,{ms * 1000 + 299999999},15,3,0,0,0"]
    df = parse_csv(_zip_csv(rows, header=True))
    assert df["open_ms"].iloc[0] == ms and df["close"].iloc[0] == 1.5


def test_load_5m_falls_back_to_daily_files_and_verifies_checksum(tmp_path: Path) -> None:
    ms = 1788220800000  # 2026-09-01 00:00 UTC
    monthly = _zip_csv([f"{ms},1,2,0.5,1.5,10,{ms + 299999},15,3,0,0,0"], header=False)
    oct1 = 1790812800000  # 2026-10-01 00:00 UTC
    daily = _zip_csv([f"{oct1},2,3,1.5,2.5,10,{oct1 + 299999},15,3,0,0,0"], header=True)
    files = {
        "data/spot/monthly/klines/BTCUSDT/5m/BTCUSDT-5m-2026-09.zip": monthly,
        "data/spot/daily/klines/BTCUSDT/5m/BTCUSDT-5m-2026-10-01.zip": daily,
    }

    def handler(req: httpx.Request) -> httpx.Response:
        key = req.url.path.lstrip("/")
        if key.endswith(".CHECKSUM"):
            body = files.get(key.removesuffix(".CHECKSUM"))
            return (
                httpx.Response(404)
                if body is None
                else httpx.Response(200, text=hashlib.sha256(body).hexdigest() + "  x")
            )
        return httpx.Response(200, content=files[key])

    df = load_5m(
        "BTCUSDT", date(2026, 9, 1), date(2026, 10, 1), tmp_path, transport=httpx.MockTransport(handler)
    )
    assert list(df["close"]) == [1.5, 2.5]
    assert df.index[-1] == pd.Timestamp("2026-10-01")

    def bad(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith(".CHECKSUM"):
            return httpx.Response(200, text="0" * 64)
        return httpx.Response(200, content=monthly)

    with pytest.raises(ChecksumMismatch):
        load_5m("ETHUSDT", date(2026, 9, 1), date(2026, 9, 30), tmp_path, transport=httpx.MockTransport(bad))
