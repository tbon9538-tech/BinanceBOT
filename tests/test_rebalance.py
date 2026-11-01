from __future__ import annotations

from decimal import Decimal

import pytest

from bot.config import RebalanceConfig
from bot.strategy.rebalance import SkipReason, should_skip

ABS = RebalanceConfig(threshold_mode="absolute", threshold=0.02, min_trade_usdt=Decimal("10"))
REL = RebalanceConfig(threshold_mode="relative", threshold_rel=0.25, min_trade_usdt=Decimal("10"))


@pytest.mark.parametrize(
    ("cfg", "t", "c", "expected"),
    [
        (ABS, 0.035, 0.020, SkipReason.TOLERANCE_BAND),  # 差 0.015 < 0.02：卫星单币的典型情况
        (REL, 0.035, 0.020, None),  # 0.015 ≥ 0.25 × 0.035 → 交易
        (REL, 0.035, 0.030, SkipReason.TOLERANCE_BAND),  # 0.005 < 0.25 × 0.035
        (ABS, 0.30, 0.25, None),
        (REL, 0.30, 0.25, SkipReason.TOLERANCE_BAND),  # 0.05 < 0.075
    ],
)
def test_tolerance_band_modes(cfg: RebalanceConfig, t: float, c: float, expected: SkipReason | None) -> None:
    cap = 10_000.0
    assert should_skip(t, c, (t - c) * cap, c * cap, 5.0, cfg) is expected


def test_min_trade_applies_in_both_modes() -> None:
    for cfg in (ABS, REL):
        # 权重差很大，但资金小：金额 9 < max(10, 5.5)
        assert should_skip(0.09, 0.0, 9.0, 0.0, 5.0, cfg) is SkipReason.BELOW_MIN_TRADE
        # 最小下单额 × 1.1 更高时以它为准
        assert should_skip(0.5, 0.0, 12.0, 0.0, 11.0, cfg) is SkipReason.BELOW_MIN_TRADE


def test_zero_target_sells_all_or_is_dust() -> None:
    assert should_skip(0.0, 0.01, -50.0, 50.0, 5.0, REL) is None
    assert should_skip(0.0, 0.0001, -3.0, 3.0, 5.0, REL) is SkipReason.DUST
    assert should_skip(0.0, 0.0, 0.0, 0.0, 5.0, REL) is SkipReason.BELOW_MIN_TRADE


def test_ignore_band_for_force_rebalance() -> None:
    assert should_skip(0.035, 0.030, 50.0, 300.0, 5.0, REL, ignore_band=True) is None
    assert should_skip(0.035, 0.034, 1.0, 340.0, 5.0, REL, ignore_band=True) is SkipReason.BELOW_MIN_TRADE
