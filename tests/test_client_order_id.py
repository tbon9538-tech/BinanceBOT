"""newClientOrderId（spec §6.4，dev_plan M5-5）与 id_sequences（M5-4）。"""

from __future__ import annotations

import hashlib
import threading
from datetime import date
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from bot.domain.types import CLIENT_ORDER_ID_RE
from bot.execution.client_order_id import (
    MAX_LEN,
    OrderKind,
    is_strategy_order,
    make_client_order_id,
    parse_client_order_id,
    symbol_hash8,
)
from bot.persistence.repo import StateRepo

RUN = date(2026, 11, 2)


def test_format_matches_spec_example() -> None:
    cid = make_client_order_id("mom1", RUN, "BTCUSDT", OrderKind.BUY, 7, 1)
    assert cid == f"mom1-261102-{hashlib.sha1(b'BTCUSDT').hexdigest()[:8]}-B007-1"
    assert len(cid) == 27 and CLIENT_ORDER_ID_RE.match(cid)
    p = parse_client_order_id(cid)
    assert p is not None and (p.strategy_id, p.yymmdd, p.kind, p.seq, p.attempt) == (
        "mom1",
        "261102",
        OrderKind.BUY,
        7,
        1,
    )
    assert parse_client_order_id("web_abc123") is None


def test_unicode_symbol_is_hashed() -> None:
    sym = "币安人生USDT"
    cid = make_client_order_id("mom1", RUN, sym, OrderKind.SELL, 1, 2)
    assert symbol_hash8(sym) == hashlib.sha1(sym.encode("utf-8")).hexdigest()[:8]
    assert CLIENT_ORDER_ID_RE.match(cid) and cid.isascii() and "-S001-2" in cid


def test_worst_case_length_within_limit() -> None:
    cid = make_client_order_id("abcdef", RUN, "X" * 40, OrderKind.KILL, 99999, 5)
    assert len(cid) <= MAX_LEN


@pytest.mark.parametrize(
    ("strategy", "seq", "attempt"),
    [("MOM1", 1, 1), ("toolong7", 1, 1), ("", 1, 1), ("mom1", 0, 1), ("mom1", 1, 0)],
)
def test_invalid_inputs(strategy: str, seq: int, attempt: int) -> None:
    with pytest.raises(ValueError):
        make_client_order_id(strategy, RUN, "BTCUSDT", OrderKind.BUY, seq, attempt)


def test_too_long_id_rejected() -> None:
    with pytest.raises(ValueError, match="36"):
        make_client_order_id("abcdef", RUN, "BTCUSDT", OrderKind.BUY, 10**12, 99)


@given(
    st.text(min_size=1, max_size=50),
    st.sampled_from(list(OrderKind)),
    st.integers(1, 9999),
    st.integers(1, 5),
)
def test_any_symbol_gives_legal_id(symbol: str, kind: OrderKind, seq: int, attempt: int) -> None:
    cid = make_client_order_id("mom1", RUN, symbol, kind, seq, attempt)
    assert CLIENT_ORDER_ID_RE.match(cid) and len(cid) <= MAX_LEN
    assert parse_client_order_id(cid) is not None


def test_strategy_prefix() -> None:
    assert is_strategy_order("mom1-261102-3f9a1c2b-B007-1", "mom1")
    assert not is_strategy_order("mom12-261102-3f9a1c2b-B007-1", "mom1")
    assert not is_strategy_order("web_x", "mom1")


def test_seq_continues_across_restarts_and_resets_per_day(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    repo = StateRepo.open(db)
    assert [repo.allocate_seq("mom1", RUN) for _ in range(3)] == [1, 2, 3]
    repo.close()
    repo = StateRepo.open(db)  # "重启"
    assert repo.allocate_seq("mom1", RUN) == 4
    assert repo.allocate_seq("mom1", date(2026, 11, 3)) == 1
    assert repo.allocate_seq("other", RUN) == 1
    ids = {
        make_client_order_id("mom1", RUN, "BTCUSDT", OrderKind.BUY, repo.allocate_seq("mom1", RUN), 1)
        for _ in range(5)
    }
    assert len(ids) == 5
    repo.close()


def test_seq_unique_under_concurrency(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    StateRepo.open(db).close()
    repos = [StateRepo.open(db) for _ in range(4)]  # 模拟多个进程（各自的引擎与连接池）
    got: list[int] = []
    lock = threading.Lock()

    def worker(r: StateRepo) -> None:
        for _ in range(25):
            v = r.allocate_seq("mom1", RUN)
            with lock:
                got.append(v)

    threads = [threading.Thread(target=worker, args=(repos[i % 4],)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(got) == list(range(1, 201))
    for r in repos:
        r.close()
