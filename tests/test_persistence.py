"""state.db：迁移、WAL、TEXT 金额、各表的读写（spec §8，dev_plan M5-4）。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import inspect

from bot.domain.types import Fill, IntentStatus, OrderResult, OrderStatus, Side
from bot.persistence.migrations import LATEST_VERSION, current_version, migrate
from bot.persistence.models import Base
from bot.persistence.repo import NewIntent, StateRepo, TargetWeight, create_state_engine, to_json
from tests.exec_helpers import TickClock

D = Decimal
T0 = datetime(2026, 11, 2, 0, 10, tzinfo=UTC)


def test_migrations_are_rerunnable_and_match_models(tmp_path: Path) -> None:
    engine = create_state_engine(tmp_path / "s.db")
    assert migrate(engine) == LATEST_VERSION
    assert migrate(engine) == LATEST_VERSION  # 第二次什么都不做
    with engine.begin() as conn:
        assert current_version(conn) == LATEST_VERSION
        rows = conn.exec_driver_sql("SELECT COUNT(*) FROM schema_version").scalar_one()
    assert rows == LATEST_VERSION
    # v1 的 DDL 中途失败后可以重跑（IF NOT EXISTS）
    from bot.persistence.migrations import v001_initial

    with engine.begin() as conn:
        v001_initial.upgrade(conn)
    insp = inspect(engine)
    for table in Base.metadata.sorted_tables:
        cols = {c["name"]: c for c in insp.get_columns(table.name)}
        assert set(cols) == {c.name for c in table.columns}, table.name
        for c in table.columns:
            assert bool(cols[c.name]["nullable"]) == bool(c.nullable), f"{table.name}.{c.name}"
        pk = insp.get_pk_constraint(table.name)["constrained_columns"]
        assert set(pk) == {c.name for c in table.primary_key.columns}, table.name


def test_wal_mode_and_parent_dir_created(tmp_path: Path) -> None:
    repo = StateRepo.open(tmp_path / "nested" / "state.db")
    with repo.engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower() == "wal"
        assert conn.exec_driver_sql("PRAGMA synchronous").scalar_one() == 2  # FULL
    repo.close()


def _intent(cid: str, **kw: object) -> NewIntent:
    args: dict[str, object] = dict(
        cid=cid,
        run_id="mom1:2026-11-02",
        strategy_id="mom1",
        account_id="spot-main",
        symbol="BTCUSDT",
        side=Side.BUY,
        kind="B",
        run_date=date(2026, 11, 2),
        seq=1,
        attempt=1,
        intended_qty=D("0.00100"),
        limit_price=D("50150.12"),
        base_asset="BTC",
        base_balance_before=D("0"),
    )
    args.update(kw)
    return NewIntent(**args)  # type: ignore[arg-type]


def test_decimal_stored_as_exact_text_and_types_enforced(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    repo = StateRepo.open(db, clock=TickClock())
    repo.create_intents([_intent("c1", intended_qty=D("0.12345678"), limit_price=D("1E+2"))])
    raw = (
        sqlite3.connect(db)
        .execute("SELECT intended_qty, limit_price, created_at FROM order_intents")
        .fetchone()
    )
    assert raw[0] == "0.12345678" and raw[1] == "100"
    assert raw[2].endswith("+00:00")  # UTC ISO 8601
    row = repo.get_intent("c1")
    assert row is not None and row.intended_qty == D("0.12345678") and isinstance(row.limit_price, Decimal)
    with pytest.raises(Exception, match="Decimal"):
        repo.create_intents([_intent("c2", intended_qty=0.1)])
    with pytest.raises(Exception, match="时区"):
        repo.append_event("X", "INFO", {}, strategy_id="mom1", account_id="a", now=datetime(2026, 1, 1))
    repo.close()


def test_intent_lifecycle(tmp_path: Path) -> None:
    repo = StateRepo.open(tmp_path / "s.db", clock=TickClock())
    repo.create_intents([_intent("c1"), _intent("c2", symbol="ETHUSDT", seq=2, base_asset="ETH")])
    repo.update_intent("c1", status=IntentStatus.SENT)
    row = repo.update_intent(
        "c1",
        status=IntentStatus.PARTIAL,
        exchange_order_id=99,
        executed_qty=D("0.0005"),
        cumm_quote_qty=D("25"),
        note="a",
    )
    row = repo.update_intent("c1", note="b", error_code=-1)
    assert row.status == "PARTIAL" and row.exchange_order_id == 99 and row.note == "a; b"
    assert row.updated_at > row.created_at
    pending = repo.list_intents("mom1", "spot-main", statuses=[IntentStatus.PLANNED])
    assert [r.cid for r in pending] == ["c2"]
    assert len(repo.list_intents("mom1", "spot-main", run_id="mom1:2026-11-02")) == 2
    assert [r.cid for r in repo.list_intents("mom1", "spot-main", symbol="ETHUSDT")] == ["c2"]
    with pytest.raises(KeyError):
        repo.update_intent("nope", status=IntentStatus.CLOSED)
    repo.close()


def _fill(trade_id: int, side: Side, qty: str, price: str, commission: str, asset: str) -> Fill:
    return Fill("BTCUSDT", trade_id, 7, side, D(price), D(qty), D(qty) * D(price), D(commission), asset, T0)


def test_orders_fills_and_net_asset_change(tmp_path: Path) -> None:
    clock = TickClock()
    repo = StateRepo.open(tmp_path / "s.db", clock=clock)
    res = OrderResult(
        "BTCUSDT", "c1", OrderStatus.FILLED, D("0.002"), D("100"), 7, raw={"side": "BUY", "x": D("1")}
    )
    repo.upsert_order(res, strategy_id="mom1", account_id="spot-main")
    repo.upsert_order(res, strategy_id="mom1", account_id="spot-main", side=Side.BUY)
    kw = dict(cid="c1", strategy_id="mom1", account_id="spot-main", base_asset="BTC", quote_asset="USDT")
    mark = clock()
    buy = _fill(1, Side.BUY, "0.002", "50000", "0.000002", "BTC")
    assert repo.upsert_fills([buy], **kw) == 1  # type: ignore[arg-type]
    assert repo.upsert_fills([buy], **kw) == 0  # 幂等  # type: ignore[arg-type]
    sell = _fill(2, Side.SELL, "0.001", "51000", "0.051", "USDT")
    repo.upsert_fills([sell], **kw)  # type: ignore[arg-type]
    assert repo.net_asset_change("BTC", strategy_id="mom1", account_id="spot-main", recorded_after=mark) == D(
        "0.002"
    ) - D("0.000002") - D("0.001")
    assert repo.net_asset_change(
        "USDT", strategy_id="mom1", account_id="spot-main", recorded_after=mark
    ) == D("-100") + D("51") - D("0.051")
    later = clock()
    assert repo.net_asset_change("BTC", strategy_id="mom1", account_id="spot-main", recorded_after=later) == 0
    assert len(repo.list_fills("mom1", "spot-main", cid="c1")) == 2
    repo.close()


def test_positions(tmp_path: Path) -> None:
    repo = StateRepo.open(tmp_path / "s.db", clock=TickClock())
    row, new = repo.ensure_managed_position("spot-main", "mom1", "SOL", as_of=T0)
    assert new and row.managed and row.updated_at == T0 and row.qty == 0
    _, new = repo.ensure_managed_position("spot-main", "mom1", "SOL", as_of=T0)
    assert not new
    repo.upsert_position("spot-main", "mom1", "BNB", qty=D("1"))  # 非受管
    _, new = repo.ensure_managed_position("spot-main", "mom1", "BNB", as_of=T0)
    assert new  # 从非受管变为受管
    repo.upsert_position(
        "spot-main",
        "mom1",
        "SOL",
        qty=D("2.5"),
        entry_date=date(2026, 11, 1),
        highest_close=D("101"),
        dust=True,
        frozen=True,
    )
    p = repo.get_position("spot-main", "mom1", "SOL")
    assert p is not None and p.qty == D("2.5") and p.dust and p.frozen and p.entry_date == date(2026, 11, 1)
    p = repo.upsert_position("spot-main", "mom1", "SOL", clear_entry=True)
    assert p.entry_date is None and p.highest_close is None
    assert [x.asset for x in repo.list_positions("spot-main", "mom1", managed_only=True)] == ["BNB", "SOL"]
    repo.close()


def test_runs_events_state_nav_flows_targets_regime(tmp_path: Path) -> None:
    repo = StateRepo.open(tmp_path / "s.db", clock=TickClock())
    repo.ensure_account("spot-main", env="testnet")
    repo.ensure_account("spot-main", env="demo")
    repo.ensure_strategy("mom1", name="momentum", version="1", config_hash="abc")
    repo.ensure_strategy("mom1", name="momentum", version="2", config_hash="def")
    run = repo.start_run(
        "mom1:2026-11-02", strategy_id="mom1", account_id="spot-main", run_date=date(2026, 11, 2)
    )
    assert run.status == "STARTED"
    repo.set_run_status("mom1:2026-11-02", "FAILED", error="boom")
    again = repo.start_run(
        "mom1:2026-11-02",
        strategy_id="mom1",
        account_id="spot-main",
        run_date=date(2026, 11, 2),
        signal_date=date(2026, 11, 1),
    )
    assert again.status == "STARTED" and again.error is None and again.started_at == run.started_at
    done = repo.set_run_status("mom1:2026-11-02", "DONE", signal_date=date(2026, 11, 1))
    assert done.finished_at is not None
    assert repo.last_run("mom1", status="DONE") is not None and repo.last_run("x") is None
    with pytest.raises(KeyError):
        repo.set_run_status("nope", "DONE")
    assert repo.get_run("mom1:2026-11-02") is not None

    eid = repo.append_event(
        "RECONCILE_DIFF",
        "WARN",
        {"diff": D("1.5"), "d": date(2026, 1, 1)},
        strategy_id="mom1",
        account_id="spot-main",
        run_id="r1",
    )
    ev = repo.list_events("mom1", type_="RECONCILE_DIFF", run_id="r1")
    assert ev[0].id == eid and '"diff": "1.5"' in ev[0].payload_json

    assert repo.get_system_state("mom1") is None
    s1 = repo.set_system_state("mom1", "RUNNING")
    s2 = repo.set_system_state("mom1", "PAUSED", reason="manual")
    assert s2.since > s1.since and s2.reason == "manual"
    s3 = repo.set_system_state("mom1", "PAUSED", reason="again")
    assert s3.since == s2.since

    for nav in ("1.0", "1.3", "1.1"):
        repo.append_nav_snapshot(
            strategy_id="mom1",
            account_id="spot-main",
            kind="PRE_TRADE",
            nav_usdt=D("500"),
            units=D("500"),
            nav_per_unit=D(nav),
            gross_exposure=D("0.5"),
            details={"a": 1},
        )
    assert repo.peak_nav_per_unit("mom1", "spot-main") == D("1.3")
    latest = repo.latest_nav_snapshot("mom1", "spot-main", kind="PRE_TRADE")
    assert latest is not None and latest.nav_per_unit == D("1.1")
    assert repo.latest_nav_snapshot("mom1", "spot-main", kind="POST_TRADE") is None
    assert repo.peak_nav_per_unit("x", "y") is None

    repo.append_capital_flow(
        strategy_id="mom1",
        account_id="spot-main",
        kind="DEPOSIT",
        amount_usdt=D("100"),
        nav_per_unit=D("1.1"),
        units_delta=D("90.909"),
        note="n",
    )
    assert repo.list_capital_flows("mom1", "spot-main")[0].amount_usdt == D("100")

    d = date(2026, 11, 1)
    rows = [TargetWeight("core", "BTCUSDT", "D", D("0.4")), TargetWeight("core", "BTCUSDT", "S", D("0.3"))]
    assert repo.write_daily_targets("mom1", d, rows) == 2
    assert repo.write_daily_targets("mom1", d, rows[:1]) == 1  # 覆盖写入
    assert len(repo.get_daily_targets("mom1", d)) == 1
    assert repo.get_daily_targets("mom1", d, kind="S") == []

    repo.upsert_regime_state(
        "mom1", d, symbol="BTCUSDT", close=D("50000"), sma=None, state="RISK_ON", switched=True
    )
    repo.upsert_regime_state(
        "mom1", d, symbol="BTCUSDT", close=D("50001"), sma=D("1"), state="RISK_ON", switched=False
    )
    rs = repo.get_regime_state("mom1", d)
    assert rs is not None and rs.close == D("50001") and not rs.switched
    repo.close()


def test_to_json_handles_domain_values() -> None:
    from bot.domain.types import Side as S

    assert to_json({"a": D("1.10"), "s": S.BUY, "t": T0, "x": frozenset({1})}) == (
        '{"a": "1.10", "s": "BUY", "t": "2026-11-02T00:10:00+00:00", "x": [1]}'
    )
    with pytest.raises(TypeError):
        to_json({"o": object()})


def test_list_fills_recorded_after(tmp_path: Path) -> None:
    clock = TickClock()
    repo = StateRepo.open(tmp_path / "s.db", clock=clock)
    kw = dict(cid=None, strategy_id="mom1", account_id="spot-main", base_asset="BTC", quote_asset="USDT")
    repo.upsert_fills([_fill(1, Side.BUY, "1", "1", "0", "BTC")], **kw)  # type: ignore[arg-type]
    cut = clock() + timedelta(microseconds=0)
    repo.upsert_fills([_fill(2, Side.BUY, "1", "1", "0", "BTC")], **kw)  # type: ignore[arg-type]
    assert [f.trade_id for f in repo.list_fills("mom1", "spot-main", recorded_after=cut)] == [2]
    repo.close()
