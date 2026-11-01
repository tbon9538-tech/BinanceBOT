"""资金费率套利研究（R10）：数据解析、资金费归日、无前视、成本守恒、爆仓、组合。不访问网络。"""

from __future__ import annotations

import io
import zipfile
from datetime import date

import pandas as pd
import pytest

from bot.backtest.carry import CarryParams, combine, run_carry
from bot.data.futures import daily_funding, funding_key, kline_key, months, parse_funding_csv

START, END = date(2024, 1, 1), date(2024, 3, 31)


def test_months_and_keys() -> None:
    assert months(date(2019, 12, 2), date(2020, 2, 1)) == ["2019-12", "2020-01", "2020-02"]
    assert funding_key("BTCUSDT", "2020-01") == (
        "data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2020-01.zip"
    )
    assert (
        kline_key("ETHUSDT", "2026-10") == "data/futures/um/monthly/klines/ETHUSDT/1d/ETHUSDT-1d-2026-10.zip"
    )


def test_parse_funding_csv_with_and_without_header() -> None:
    body = "1704067200000,8,0.0001\n1704096000000,8,-0.0002\n"
    with_header = parse_funding_csv(("calc_time,funding_interval_hours,last_funding_rate\n" + body).encode())
    without = parse_funding_csv(body.encode())
    pd.testing.assert_series_equal(with_header, without)
    assert list(with_header) == [0.0001, -0.0002]
    assert str(with_header.index[0]) == "2024-01-01 00:00:00+00:00"


def test_funding_belongs_to_preceding_holding_day() -> None:
    ts = pd.to_datetime(
        ["2024-01-02 00:00:00.005", "2024-01-02 08:00", "2024-01-02 16:00", "2024-01-03 00:00"],
        utc=True,
        format="ISO8601",
    )
    daily = daily_funding(pd.Series([1.0, 2.0, 3.0, 4.0], index=ts))
    # 01-02 00:00（含几毫秒抖动）的结算属于 01-01；01-02 全天持有收到 08:00、16:00 与 01-03 00:00 三次
    assert daily.to_dict() == {pd.Timestamp("2024-01-01"): 1.0, pd.Timestamp("2024-01-02"): 9.0}


def _flat(f_daily: float, symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")):  # type: ignore[no-untyped-def]
    idx = pd.date_range("2023-12-01", END, freq="D")
    spot = {s: pd.Series(100.0, index=idx) for s in symbols}
    perp = {s: pd.DataFrame({"close": 100.0, "high": 101.0}, index=idx) for s in symbols}
    funding = {s: pd.Series(f_daily, index=idx) for s in symbols}
    return spot, perp, funding


def test_always_on_with_constant_funding_matches_closed_form() -> None:
    f = 0.0003  # 每天 0.03%，约年化 11%
    spot, perp, funding = _flat(f)
    p = CarryParams(hurdle_annual=None)
    res = run_carry(spot, perp, funding, START, END, p)
    n_days = len(res.nav)
    entry = (p.spot_cost + p.perp_cost) / 1.5
    assert res.nav.iloc[-1] == pytest.approx((1 - entry) * (1 + f / 1.5) ** n_days, rel=1e-12)
    assert res.cost.sum() == pytest.approx(entry)  # 只开仓一次（成本守恒）
    assert res.holding.all().all()
    assert res.deployed.iloc[-1] == pytest.approx(1 / 1.5, rel=1e-12)  # 按当天开始时的资金计算


def test_hurdle_uses_only_past_funding() -> None:
    spot, perp, funding = _flat(0.0003)  # 年化约 11%，高于 5% 门槛
    p = CarryParams(hurdle_annual=0.05)
    base = run_carry(spot, perp, funding, START, END, p)
    d = pd.Timestamp("2024-02-10")
    for s in funding:
        funding[s].loc[d] = -0.05  # 当天资金费突变
    changed = run_carry(spot, perp, funding, START, END, p)
    assert bool(changed.holding.loc[d].all()) == bool(base.holding.loc[d].all())  # 当天的持仓决定不受影响
    assert changed.nav.loc[d] < base.nav.loc[d]  # 但当天的盈亏照实计入
    assert not changed.holding.loc[d + pd.Timedelta(days=1)].any()  # 次日 7 日均值转负，平仓


def test_below_hurdle_stays_flat_and_costs_nothing() -> None:
    spot, perp, funding = _flat(0.0001)  # 年化约 3.65%，低于 5%
    res = run_carry(spot, perp, funding, START, END, CarryParams(hurdle_annual=0.05))
    assert not res.holding.any().any() and res.nav.iloc[-1] == pytest.approx(1.0) and res.cost.sum() == 0


def test_liquidation_loses_margin_and_closes() -> None:
    spot, perp, funding = _flat(0.0, symbols=("BTCUSDT",))
    d = pd.Timestamp("2024-02-01")
    perp["BTCUSDT"].loc[d, "high"] = 150.0  # 当天最高价相对前一日收盘 +50%
    p = CarryParams(hurdle_annual=None)
    res = run_carry(spot, perp, funding, START, END, p)
    assert res.events == [("2024-02-01", "BTCUSDT", "LIQUIDATION")]
    before = res.nav.loc[d - pd.Timedelta(days=1)]
    assert res.nav.loc[d] == pytest.approx(before - before / 1.5 / 2)  # 损失保证金 n/L
    assert not res.holding.loc[d, "BTCUSDT"]


def test_combine_rebalances_daily() -> None:
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    trend = pd.Series([1.0, 1.10, 0.99], index=idx)  # +10%、−10%
    carry = pd.Series([1.0, 1.01, 1.0201], index=idx)  # +1%、+1%
    nav = combine(trend, carry, 0.5)
    assert nav.iloc[1] == pytest.approx(1.055)
    assert nav.iloc[2] == pytest.approx(1.055 * (1 + 0.5 * -0.1 + 0.5 * 0.01))


def test_zip_member_is_read(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from bot.data.futures import _csv

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("BTCUSDT-fundingRate-2024-01.csv", "calc_time,funding_interval_hours,last_funding_rate\n")
    assert _csv(buf.getvalue()).startswith(b"calc_time")
