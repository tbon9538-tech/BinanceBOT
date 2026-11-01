from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from bot.data.holdout import HoldoutGuard, HoldoutLog, HoldoutViolation
from bot.data.segments import current_segment, listing_age, segment_ids, split_segments
from bot.data.store import MarketStore, ResearchMarketView
from tests.helpers import raw_frame

HOLDOUT = date(2025, 7, 1)


@pytest.fixture
def store(tmp_path: Path) -> MarketStore:
    return MarketStore(tmp_path / "market.db")


def test_upsert_is_idempotent_and_keeps_decimal_strings(store: MarketStore) -> None:
    df = raw_frame(date(2024, 1, 1), 5, close=lambda i: 0.00001234 + i * 1e-8)
    assert store.upsert_klines("XUSDT", df, "vision") == 5
    store.upsert_klines("XUSDT", df, "vision")
    raw = store.read_raw("XUSDT")
    assert len(raw) == 5
    assert raw["close"].iloc[0] == "0.00001234"
    assert store.last_date("XUSDT") == "2024-01-05"


def test_rest_does_not_overwrite_vision_but_vision_overwrites_rest(store: MarketStore) -> None:
    v = raw_frame(date(2024, 1, 1), 2, close=lambda i: 100.0)
    r = raw_frame(date(2024, 1, 1), 3, close=lambda i: 200.0)
    store.upsert_klines("XUSDT", v, "vision")
    store.upsert_klines("XUSDT", r, "rest")
    closes = list(store.read_raw("XUSDT")["close"])
    assert closes == ["100.00000000", "100.00000000", "200.00000000"]
    store.upsert_klines("XUSDT", raw_frame(date(2024, 1, 3), 1, close=lambda i: 300.0), "vision")
    assert store.read_raw("XUSDT")["close"].iloc[2] == "300.00000000"


def test_read_symbol_float_and_coverage(store: MarketStore) -> None:
    store.upsert_klines("AUSDT", raw_frame(date(2023, 12, 30), 4), "vision")
    df = store.read_symbol("AUSDT")
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df["close"].dtype == "float64"
    cov = store.coverage_by_year()
    assert list(cov["year"]) == ["2023", "2024"]
    assert list(cov["rows"]) == [2, 2]


def test_ingested_files_and_meta(store: MarketStore) -> None:
    store.mark_file_ingested("data/spot/monthly/klines/A/1d/a.zip", "ab", 30)
    store.mark_file_ingested("data/spot/monthly/klines/A/1d/a.zip", "cd", 31)
    assert store.ingested_files("data/spot/monthly/") == {"data/spot/monthly/klines/A/1d/a.zip"}
    store.upsert_symbol_meta(
        [
            {
                "symbol": "AUSDT",
                "base_asset": "A",
                "quote_asset": "USDT",
                "status": "BREAK",
                "is_spot_trading_allowed": False,
            }
        ]
    )
    assert store.symbol_meta()["status"].tolist() == ["BREAK"]


def test_research_view_never_returns_holdout(store: MarketStore) -> None:
    store.upsert_klines("BTCUSDT", raw_frame(date(2025, 6, 25), 20), "vision")  # 跨越 2025-07-01
    view = ResearchMarketView(store, HoldoutGuard(HOLDOUT))
    df = view.read_symbol("BTCUSDT")
    assert df.index.max() == pd.Timestamp("2025-06-30")
    assert len(view.read_symbol("BTCUSDT", end=date(2025, 6, 27))) == 3
    with pytest.raises(HoldoutViolation):
        view.read_symbol("BTCUSDT", end=HOLDOUT)


def test_holdout_guard_filters_and_asserts() -> None:
    g = HoldoutGuard(HOLDOUT)
    g.check_end(date(2025, 6, 30))
    with pytest.raises(HoldoutViolation):
        g.check_end(date(2026, 1, 1))
    idx = pd.date_range("2025-06-29", periods=4)
    df = pd.DataFrame({"x": range(4)}, index=idx)
    assert len(g.filter_frame(df)) == 2
    assert len(g.filter_frame(pd.DataFrame({"date": ["2025-06-30", "2025-07-01"]}))) == 1
    with pytest.raises(HoldoutViolation):
        g.assert_clean(df)


def test_holdout_log_authorization(tmp_path: Path) -> None:
    log = HoldoutLog(tmp_path / "holdout_runs.log")
    today = date(2026, 12, 1)
    with pytest.raises(HoldoutViolation, match="--final"):
        log.authorize(final=False, confirm=today, today=today, not_first_run=False)
    with pytest.raises(HoldoutViolation, match="--confirm"):
        log.authorize(final=True, confirm=today - timedelta(days=1), today=today, not_first_run=False)
    assert log.authorize(final=True, confirm=today, today=today, not_first_run=False) is True
    log.append(git_commit="abc", config_hash="h", summary={"sharpe": 1.0}, first_run=True)
    rec = json.loads(log.path.read_text(encoding="utf-8").splitlines()[0])
    assert rec["git_commit"] == "abc" and rec["first_run"] is True
    with pytest.raises(HoldoutViolation, match="已经运行过"):
        log.authorize(final=True, confirm=today, today=today, not_first_run=False)
    assert log.authorize(final=True, confirm=today, today=today, not_first_run=True) is False


def test_segments_split_on_gap_over_seven_days() -> None:
    d0 = date(2022, 5, 1)
    days = [d0 + timedelta(days=i) for i in range(10)]
    luna_new = [date(2022, 5, 31) + timedelta(days=i) for i in range(3)]  # 旧 LUNA 停在 5/10，新币 5/31 上市
    segs = split_segments(days + luna_new)
    assert len(segs) == 2
    assert segs[1].start == date(2022, 5, 31) and segs[1].bars == 3
    assert listing_age(days + luna_new)[-1] == 3


@pytest.mark.parametrize(("gap", "n_segments"), [(7, 1), (8, 2)])
def test_segment_gap_boundary(gap: int, n_segments: int) -> None:
    d = [date(2024, 1, 1), date(2024, 1, 1) + timedelta(days=gap)]
    assert len(split_segments(d, gap_days=7)) == n_segments


def test_segment_requires_ascending_dates() -> None:
    with pytest.raises(ValueError):
        segment_ids([date(2024, 1, 2), date(2024, 1, 1)])


def test_current_segment_keeps_last() -> None:
    idx = pd.DatetimeIndex(["2022-05-01", "2022-05-02", "2022-05-31", "2022-06-01"])
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0, 4.0]}, index=idx)
    assert list(current_segment(df)["close"]) == [3.0, 4.0]
