from __future__ import annotations

import socket
from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from bot.domain.types import (
    AccountType,
    Balance,
    InstrumentKey,
    OrderRequest,
    OrderType,
    Side,
    TargetPortfolio,
    TimeInForce,
)

SPOT_BTC = InstrumentKey(AccountType.SPOT, "BTCUSDT")
PERP_BTC = InstrumentKey(AccountType.USDM_FUTURES, "BTCUSDT")


def _limit(cid: str = "mom1-261102-3f9a1c2b-B007-1", **kw) -> OrderRequest:  # type: ignore[no-untyped-def]
    args = dict(
        symbol="BTCUSDT",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("0.001"),
        client_order_id=cid,
        price=Decimal("50000"),
        time_in_force=TimeInForce.IOC,
    )
    args.update(kw)
    return OrderRequest(**args)  # type: ignore[arg-type]


def test_instrument_key_distinguishes_account_types() -> None:
    assert SPOT_BTC != PERP_BTC
    assert len({SPOT_BTC, PERP_BTC, InstrumentKey(AccountType.SPOT, "BTCUSDT")}) == 2


def test_balance_total() -> None:
    assert Balance("USDT", Decimal("1.5"), Decimal("2.25")).total == Decimal("3.75")


def test_order_request_valid() -> None:
    assert _limit().client_order_id.endswith("-1")


@pytest.mark.parametrize(
    "kw",
    [
        {"quantity": Decimal("0")},
        {"price": None},
        {"time_in_force": None},
        {"order_type": OrderType.MARKET, "price": None},  # MARKET 不接受 time_in_force
    ],
)
def test_order_request_invalid(kw) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        _limit(**kw)


@pytest.mark.parametrize("cid", ["", "a" * 37, "has space", "中文", "a.b"])
def test_client_order_id_rejected(cid: str) -> None:
    with pytest.raises(ValueError, match="clientOrderId"):
        _limit(cid=cid)


@given(st.from_regex(r"^[A-Za-z0-9_-]{1,36}$", fullmatch=True))
def test_client_order_id_accepts_allowed_charset(cid: str) -> None:
    assert _limit(cid=cid).client_order_id == cid


def test_target_portfolio_rejects_negative_spot_weight() -> None:
    with pytest.raises(ValueError, match="不能为负"):
        TargetPortfolio("mom1", date(2026, 11, 1), {"spot-main": {SPOT_BTC: Decimal("-0.1")}})


def test_target_portfolio_rejects_spot_sum_above_one() -> None:
    eth = InstrumentKey(AccountType.SPOT, "ETHUSDT")
    with pytest.raises(ValueError, match="超过 1"):
        TargetPortfolio(
            "mom1", date(2026, 11, 1), {"spot-main": {SPOT_BTC: Decimal("0.6"), eth: Decimal("0.5")}}
        )


def test_target_portfolio_total_weight() -> None:
    tp = TargetPortfolio("mom1", date(2026, 11, 1), {"spot-main": {SPOT_BTC: Decimal("0.4")}})
    assert tp.total_weight("spot-main") == Decimal("0.4")
    assert tp.total_weight("other") == Decimal(0)


@pytest.mark.filterwarnings("ignore:A test tried to use socket")
def test_network_is_disabled_in_unit_tests() -> None:
    # pytest-socket：单元测试中任何网络访问都必须失败（doc/DECISIONS.md D-023）
    with pytest.raises(Exception, match=r"(?i)socket"):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
