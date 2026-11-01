"""state.db 的读写接口（spec §8）。

- SQLite，WAL 模式，synchronous=FULL（write-ahead 意图必须在下单前真正落盘）。
- 每个方法在自己的事务里完成；返回的 ORM 对象是已脱离会话的快照，修改它们不会写回数据库，
  需要修改时调用对应的 update/upsert 方法。
- 金额一律 Decimal；时间一律带时区的 UTC datetime。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, delete, event, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from bot import paths
from bot.domain.types import Fill, IntentStatus, OrderResult, Side
from bot.persistence.migrations import migrate
from bot.persistence.models import (
    AccountRow,
    CapitalFlowRow,
    DailyTargetRow,
    EventRow,
    FillRow,
    IdSequenceRow,
    NavSnapshotRow,
    OrderIntentRow,
    OrderRow,
    PositionRow,
    RegimeStateRow,
    RunRow,
    StrategyRow,
    SystemStateRow,
)

_ZERO = Decimal(0)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return format(o, "f")
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Enum):
        return o.value
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    raise TypeError(f"无法序列化为 JSON：{type(o).__name__}")


def to_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=_json_default)


def create_state_engine(path: str | Path = paths.STATE_DB) -> Engine:
    """创建 SQLite 引擎：WAL、synchronous=FULL、busy_timeout。不执行迁移。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{p.as_posix()}", connect_args={"check_same_thread": False, "timeout": 30}
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn: Any, _record: Any) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=FULL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    return engine


@dataclass(frozen=True, slots=True)
class NewIntent:
    """create_intents 的输入：一次下单尝试。"""

    cid: str
    run_id: str
    strategy_id: str
    account_id: str
    symbol: str
    side: Side
    kind: str
    run_date: date
    seq: int
    attempt: int
    intended_qty: Decimal
    limit_price: Decimal
    base_asset: str
    base_balance_before: Decimal | None


@dataclass(frozen=True, slots=True)
class TargetWeight:
    sleeve: str
    symbol: str
    kind: str  # D / S / W
    weight: Decimal


class StateRepo:
    def __init__(self, engine: Engine, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.engine = engine
        self._sf = sessionmaker(engine, expire_on_commit=False)
        self._clock = clock

    @classmethod
    def open(cls, path: str | Path = paths.STATE_DB, *, clock: Callable[[], datetime] = utc_now) -> StateRepo:
        """打开（必要时创建）state.db 并迁移到最新版本。"""
        engine = create_state_engine(path)
        migrate(engine)
        return cls(engine, clock=clock)

    def close(self) -> None:
        self.engine.dispose()

    def _session(self) -> Session:
        return self._sf()

    def _t(self, now: datetime | None) -> datetime:
        return now if now is not None else self._clock()

    # ---- 账户与策略 ----

    def ensure_account(
        self, account_id: str, *, env: str, venue: str = "binance", account_type: str = "SPOT"
    ) -> None:
        with self._session() as s, s.begin():
            row = s.get(AccountRow, account_id)
            if row is None:
                s.add(AccountRow(account_id=account_id, venue=venue, account_type=account_type, env=env))
            else:
                row.venue, row.account_type, row.env = venue, account_type, env

    def ensure_strategy(self, strategy_id: str, *, name: str, version: str, config_hash: str) -> None:
        with self._session() as s, s.begin():
            row = s.get(StrategyRow, strategy_id)
            if row is None:
                s.add(
                    StrategyRow(strategy_id=strategy_id, name=name, version=version, config_hash=config_hash)
                )
            else:
                row.name, row.version, row.config_hash = name, version, config_hash

    # ---- runs ----

    def start_run(
        self,
        run_id: str,
        *,
        strategy_id: str,
        account_id: str,
        run_date: date,
        signal_date: date | None = None,
        now: datetime | None = None,
    ) -> RunRow:
        """开始（或重新开始）一次运行：状态置为 STARTED。已有的 run 保留 started_at。"""
        t = self._t(now)
        with self._session() as s, s.begin():
            row = s.get(RunRow, run_id)
            if row is None:
                row = RunRow(
                    run_id=run_id,
                    strategy_id=strategy_id,
                    account_id=account_id,
                    run_date=run_date,
                    signal_date=signal_date,
                    status="STARTED",
                    started_at=t,
                    finished_at=None,
                    error=None,
                )
                s.add(row)
            else:
                row.status, row.error, row.finished_at = "STARTED", None, None
                if signal_date is not None:
                    row.signal_date = signal_date
            return row

    def get_run(self, run_id: str) -> RunRow | None:
        with self._session() as s:
            return s.get(RunRow, run_id)

    def set_run_status(
        self,
        run_id: str,
        status: str,
        *,
        error: str | None = None,
        signal_date: date | None = None,
        now: datetime | None = None,
    ) -> RunRow:
        with self._session() as s, s.begin():
            row = s.get(RunRow, run_id)
            if row is None:
                raise KeyError(f"run {run_id} 不存在")
            row.status = status
            row.error = error
            if signal_date is not None:
                row.signal_date = signal_date
            if status in ("DONE", "FAILED", "SKIPPED"):
                row.finished_at = self._t(now)
            return row

    def last_run(self, strategy_id: str, *, status: str | None = None) -> RunRow | None:
        with self._session() as s:
            q = select(RunRow).where(RunRow.strategy_id == strategy_id)
            if status is not None:
                q = q.where(RunRow.status == status)
            rows = list(s.scalars(q))
            return max(rows, key=lambda r: (r.run_date, r.started_at)) if rows else None

    # ---- clientOrderId 序号 ----

    def allocate_seq(self, strategy_id: str, run_date: date) -> int:
        """原子地分配 (strategy_id, 运行日) 的下一个意图序号（从 1 开始；重启后继续递增）。"""
        stmt = (
            sqlite_insert(IdSequenceRow)
            .values(strategy_id=strategy_id, run_date=run_date, next_seq=2)
            .on_conflict_do_update(
                index_elements=["strategy_id", "run_date"],
                set_={"next_seq": IdSequenceRow.next_seq + 1},
            )
            .returning(IdSequenceRow.next_seq)
        )
        with self.engine.begin() as conn:
            nxt = conn.execute(stmt).scalar_one()
        return int(nxt) - 1

    # ---- 下单意图（write-ahead） ----

    def create_intents(self, intents: Sequence[NewIntent], *, now: datetime | None = None) -> None:
        """在一个事务里写入一批 PLANNED 意图。"""
        t = self._t(now)
        with self._session() as s, s.begin():
            for i in intents:
                s.add(
                    OrderIntentRow(
                        cid=i.cid,
                        run_id=i.run_id,
                        strategy_id=i.strategy_id,
                        account_id=i.account_id,
                        symbol=i.symbol,
                        side=i.side.value,
                        kind=i.kind,
                        run_date=i.run_date,
                        seq=i.seq,
                        attempt=i.attempt,
                        intended_qty=i.intended_qty,
                        limit_price=i.limit_price,
                        status=IntentStatus.PLANNED.value,
                        exchange_order_id=None,
                        error_code=None,
                        executed_qty=_ZERO,
                        cumm_quote_qty=_ZERO,
                        base_asset=i.base_asset,
                        base_balance_before=i.base_balance_before,
                        note=None,
                        created_at=t,
                        updated_at=t,
                    )
                )

    def get_intent(self, cid: str) -> OrderIntentRow | None:
        with self._session() as s:
            return s.get(OrderIntentRow, cid)

    def update_intent(
        self,
        cid: str,
        *,
        status: IntentStatus | None = None,
        exchange_order_id: int | None = None,
        error_code: int | None = None,
        executed_qty: Decimal | None = None,
        cumm_quote_qty: Decimal | None = None,
        note: str | None = None,
        now: datetime | None = None,
    ) -> OrderIntentRow:
        with self._session() as s, s.begin():
            row = s.get(OrderIntentRow, cid)
            if row is None:
                raise KeyError(f"意图 {cid} 不存在")
            if status is not None:
                row.status = status.value
            if exchange_order_id is not None:
                row.exchange_order_id = exchange_order_id
            if error_code is not None:
                row.error_code = error_code
            if executed_qty is not None:
                row.executed_qty = executed_qty
            if cumm_quote_qty is not None:
                row.cumm_quote_qty = cumm_quote_qty
            if note is not None:
                row.note = note if not row.note else f"{row.note}; {note}"
            row.updated_at = self._t(now)
            return row

    def list_intents(
        self,
        strategy_id: str,
        account_id: str,
        *,
        statuses: Collection[IntentStatus] | None = None,
        run_id: str | None = None,
        symbol: str | None = None,
    ) -> list[OrderIntentRow]:
        with self._session() as s:
            q = select(OrderIntentRow).where(
                OrderIntentRow.strategy_id == strategy_id, OrderIntentRow.account_id == account_id
            )
            if statuses is not None:
                q = q.where(OrderIntentRow.status.in_([x.value for x in statuses]))
            if run_id is not None:
                q = q.where(OrderIntentRow.run_id == run_id)
            if symbol is not None:
                q = q.where(OrderIntentRow.symbol == symbol)
            rows = list(s.scalars(q))
        return sorted(rows, key=lambda r: (r.created_at, r.run_date, r.seq, r.attempt))

    # ---- 订单与成交 ----

    def upsert_order(
        self,
        result: OrderResult,
        *,
        strategy_id: str,
        account_id: str,
        side: Side | None = None,
        now: datetime | None = None,
    ) -> None:
        raw_side = result.raw.get("side") if result.raw else None
        side_value = side.value if side is not None else (str(raw_side) if raw_side else None)
        with self._session() as s, s.begin():
            row = s.get(OrderRow, result.client_order_id)
            if row is None:
                row = OrderRow(cid=result.client_order_id, strategy_id=strategy_id, account_id=account_id)
                s.add(row)
            row.order_id = result.order_id
            row.symbol = result.symbol
            row.side = side_value
            row.status = result.status.value
            row.executed_qty = result.executed_qty
            row.cumm_quote_qty = result.cumm_quote_qty
            row.expiry_reason = result.expiry_reason
            row.raw_json = to_json(dict(result.raw))
            row.updated_at = self._t(now)

    def upsert_fills(
        self,
        fills: Iterable[Fill],
        *,
        cid: str | None,
        strategy_id: str,
        account_id: str,
        base_asset: str,
        quote_asset: str,
        now: datetime | None = None,
    ) -> int:
        """按 (symbol, trade_id) 写入成交；已存在的成交只补全字段，不改 recorded_at。返回新增条数。"""
        t = self._t(now)
        added = 0
        with self._session() as s, s.begin():
            for f in fills:
                row = s.get(FillRow, (f.symbol, f.trade_id))
                if row is None:
                    s.add(
                        FillRow(
                            symbol=f.symbol,
                            trade_id=f.trade_id,
                            cid=cid,
                            strategy_id=strategy_id,
                            account_id=account_id,
                            order_id=f.order_id,
                            side=f.side.value,
                            base_asset=base_asset,
                            quote_asset=quote_asset,
                            price=f.price,
                            qty=f.qty,
                            quote_qty=f.quote_qty,
                            commission=f.commission,
                            commission_asset=f.commission_asset,
                            time=f.time,
                            recorded_at=t,
                        )
                    )
                    added += 1
                else:
                    # myTrades 给出的 quoteQty / time 比 FULL 响应更准确
                    row.quote_qty, row.time = f.quote_qty, f.time
                    row.commission, row.commission_asset = f.commission, f.commission_asset
                    if cid is not None:
                        row.cid = cid
        return added

    def list_fills(
        self,
        strategy_id: str,
        account_id: str,
        *,
        cid: str | None = None,
        symbol: str | None = None,
        recorded_after: datetime | None = None,
    ) -> list[FillRow]:
        with self._session() as s:
            q = select(FillRow).where(FillRow.strategy_id == strategy_id, FillRow.account_id == account_id)
            if cid is not None:
                q = q.where(FillRow.cid == cid)
            if symbol is not None:
                q = q.where(FillRow.symbol == symbol)
            rows = list(s.scalars(q))
        if recorded_after is not None:
            rows = [r for r in rows if r.recorded_at > recorded_after]
        return sorted(rows, key=lambda r: (r.time, r.trade_id))

    def net_asset_change(
        self, asset: str, *, strategy_id: str, account_id: str, recorded_after: datetime | None
    ) -> Decimal:
        """recorded_after 之后记录的成交对某个资产余额的净影响（含手续费）。"""
        total = _ZERO
        for f in self.list_fills(strategy_id, account_id, recorded_after=recorded_after):
            total += fill_asset_delta(f, asset)
        return total

    # ---- 持仓 ----

    def get_position(self, account_id: str, strategy_id: str, asset: str) -> PositionRow | None:
        with self._session() as s:
            return s.get(PositionRow, (account_id, strategy_id, asset))

    def list_positions(
        self, account_id: str, strategy_id: str, *, managed_only: bool = False
    ) -> list[PositionRow]:
        with self._session() as s:
            q = select(PositionRow).where(
                PositionRow.account_id == account_id, PositionRow.strategy_id == strategy_id
            )
            if managed_only:
                q = q.where(PositionRow.managed.is_(True))
            return sorted(s.scalars(q), key=lambda r: r.asset)

    def upsert_position(
        self,
        account_id: str,
        strategy_id: str,
        asset: str,
        *,
        qty: Decimal | None = None,
        managed: bool | None = None,
        entry_date: date | None = None,
        highest_close: Decimal | None = None,
        clear_entry: bool = False,
        dust: bool | None = None,
        frozen: bool | None = None,
        now: datetime | None = None,
    ) -> PositionRow:
        """新建或更新持仓行。未给出的字段保持不变（新行默认：未受管、数量 0、非灰尘、未冻结）。"""
        t = self._t(now)
        with self._session() as s, s.begin():
            row = s.get(PositionRow, (account_id, strategy_id, asset))
            if row is None:
                row = PositionRow(
                    account_id=account_id,
                    strategy_id=strategy_id,
                    asset=asset,
                    managed=False,
                    qty=_ZERO,
                    entry_date=None,
                    highest_close=None,
                    dust=False,
                    frozen=False,
                    updated_at=t,
                )
                s.add(row)
            if qty is not None:
                row.qty = qty
            if managed is not None:
                row.managed = managed
            if clear_entry:
                row.entry_date, row.highest_close = None, None
            if entry_date is not None:
                row.entry_date = entry_date
            if highest_close is not None:
                row.highest_close = highest_close
            if dust is not None:
                row.dust = dust
            if frozen is not None:
                row.frozen = frozen
            row.updated_at = t
            return row

    def ensure_managed_position(
        self, account_id: str, strategy_id: str, asset: str, *, as_of: datetime
    ) -> tuple[PositionRow, bool]:
        """首次买入时把资产登记为受管（spec §7）。新行的 updated_at 取 as_of（意图创建时间），
        使本次买入的成交在随后的对账中被计入；已有行只把 managed 置为 True。返回 (行, 是否新登记)。"""
        with self._session() as s, s.begin():
            row = s.get(PositionRow, (account_id, strategy_id, asset))
            if row is None:
                row = PositionRow(
                    account_id=account_id,
                    strategy_id=strategy_id,
                    asset=asset,
                    managed=True,
                    qty=_ZERO,
                    entry_date=None,
                    highest_close=None,
                    dust=False,
                    frozen=False,
                    updated_at=as_of,
                )
                s.add(row)
                return row, True
            if not row.managed:
                row.managed = True
                return row, True
            return row, False

    # ---- 事件 ----

    def append_event(
        self,
        type_: str,
        level: str,
        payload: Mapping[str, Any],
        *,
        strategy_id: str,
        account_id: str,
        run_id: str | None = None,
        now: datetime | None = None,
    ) -> int:
        with self._session() as s, s.begin():
            row = EventRow(
                time=self._t(now),
                level=level,
                type=type_,
                strategy_id=strategy_id,
                account_id=account_id,
                run_id=run_id,
                payload_json=to_json(dict(payload)),
            )
            s.add(row)
            s.flush()
            return int(row.id)

    def list_events(
        self, strategy_id: str, *, type_: str | None = None, run_id: str | None = None
    ) -> list[EventRow]:
        with self._session() as s:
            q = select(EventRow).where(EventRow.strategy_id == strategy_id)
            if type_ is not None:
                q = q.where(EventRow.type == type_)
            if run_id is not None:
                q = q.where(EventRow.run_id == run_id)
            return list(s.scalars(q.order_by(EventRow.id)))

    # ---- 系统状态 ----

    def get_system_state(self, strategy_id: str) -> SystemStateRow | None:
        with self._session() as s:
            return s.get(SystemStateRow, strategy_id)

    def set_system_state(
        self, strategy_id: str, state: str, *, reason: str | None = None, now: datetime | None = None
    ) -> SystemStateRow:
        with self._session() as s, s.begin():
            row = s.get(SystemStateRow, strategy_id)
            t = self._t(now)
            if row is None:
                row = SystemStateRow(strategy_id=strategy_id, state=state, since=t, reason=reason)
                s.add(row)
            else:
                if row.state != state:
                    row.since = t
                row.state, row.reason = state, reason
            return row

    # ---- 净值快照 ----

    def append_nav_snapshot(
        self,
        *,
        strategy_id: str,
        account_id: str,
        kind: str,
        nav_usdt: Decimal,
        units: Decimal,
        nav_per_unit: Decimal,
        gross_exposure: Decimal,
        details: Mapping[str, Any] | None = None,
        run_id: str | None = None,
        now: datetime | None = None,
    ) -> int:
        with self._session() as s, s.begin():
            row = NavSnapshotRow(
                strategy_id=strategy_id,
                account_id=account_id,
                run_id=run_id,
                time=self._t(now),
                kind=kind,
                nav_usdt=nav_usdt,
                units=units,
                nav_per_unit=nav_per_unit,
                gross_exposure=gross_exposure,
                details_json=to_json(dict(details or {})),
            )
            s.add(row)
            s.flush()
            return int(row.id)

    def list_nav_snapshots(
        self, strategy_id: str, account_id: str, *, kind: str | None = None
    ) -> list[NavSnapshotRow]:
        with self._session() as s:
            q = select(NavSnapshotRow).where(
                NavSnapshotRow.strategy_id == strategy_id, NavSnapshotRow.account_id == account_id
            )
            if kind is not None:
                q = q.where(NavSnapshotRow.kind == kind)
            return sorted(s.scalars(q), key=lambda r: (r.time, r.id))

    def latest_nav_snapshot(
        self, strategy_id: str, account_id: str, *, kind: str | None = None
    ) -> NavSnapshotRow | None:
        rows = self.list_nav_snapshots(strategy_id, account_id, kind=kind)
        return rows[-1] if rows else None

    def peak_nav_per_unit(self, strategy_id: str, account_id: str) -> Decimal | None:
        """历史最高单位净值（TEXT 字段不能用 SQL MAX，在 Python 中比较）。"""
        rows = self.list_nav_snapshots(strategy_id, account_id)
        return max((r.nav_per_unit for r in rows), default=None)

    # ---- 出入金 ----

    def append_capital_flow(
        self,
        *,
        strategy_id: str,
        account_id: str,
        kind: str,
        amount_usdt: Decimal,
        nav_per_unit: Decimal,
        units_delta: Decimal,
        note: str | None = None,
        now: datetime | None = None,
    ) -> int:
        with self._session() as s, s.begin():
            row = CapitalFlowRow(
                account_id=account_id,
                strategy_id=strategy_id,
                time=self._t(now),
                kind=kind,
                amount_usdt=amount_usdt,
                nav_per_unit=nav_per_unit,
                units_delta=units_delta,
                note=note,
            )
            s.add(row)
            s.flush()
            return int(row.id)

    def list_capital_flows(self, strategy_id: str, account_id: str) -> list[CapitalFlowRow]:
        with self._session() as s:
            q = select(CapitalFlowRow).where(
                CapitalFlowRow.strategy_id == strategy_id, CapitalFlowRow.account_id == account_id
            )
            return sorted(s.scalars(q), key=lambda r: (r.time, r.id))

    # ---- 每日目标（审计） ----

    def write_daily_targets(self, strategy_id: str, as_of: date, rows: Iterable[TargetWeight]) -> int:
        """覆盖写入某一天的目标（先删后写，可重复执行）。"""
        n = 0
        with self._session() as s, s.begin():
            s.execute(
                delete(DailyTargetRow).where(
                    DailyTargetRow.strategy_id == strategy_id, DailyTargetRow.date == as_of
                )
            )
            for r in rows:
                s.add(
                    DailyTargetRow(
                        strategy_id=strategy_id,
                        sleeve=r.sleeve,
                        date=as_of,
                        symbol=r.symbol,
                        kind=r.kind,
                        weight=r.weight,
                    )
                )
                n += 1
        return n

    def get_daily_targets(
        self, strategy_id: str, as_of: date, *, kind: str | None = None
    ) -> list[DailyTargetRow]:
        with self._session() as s:
            q = select(DailyTargetRow).where(
                DailyTargetRow.strategy_id == strategy_id, DailyTargetRow.date == as_of
            )
            if kind is not None:
                q = q.where(DailyTargetRow.kind == kind)
            return sorted(s.scalars(q), key=lambda r: (r.sleeve, r.kind, r.symbol))

    # ---- 大盘状态 ----

    def upsert_regime_state(
        self,
        strategy_id: str,
        d: date,
        *,
        symbol: str,
        close: Decimal,
        sma: Decimal | None,
        state: str,
        switched: bool,
    ) -> None:
        with self._session() as s, s.begin():
            row = s.get(RegimeStateRow, (strategy_id, d))
            if row is None:
                row = RegimeStateRow(strategy_id=strategy_id, date=d)
                s.add(row)
            row.symbol, row.close, row.sma, row.state, row.switched = symbol, close, sma, state, switched

    def get_regime_state(self, strategy_id: str, d: date) -> RegimeStateRow | None:
        with self._session() as s:
            return s.get(RegimeStateRow, (strategy_id, d))


def fill_asset_delta(f: FillRow, asset: str) -> Decimal:
    """一笔成交对某个资产余额的影响：买入加 base、减 quote；卖出相反；手续费从对应资产扣除。"""
    d = _ZERO
    buy = f.side == Side.BUY.value
    if f.base_asset == asset:
        d += f.qty if buy else -f.qty
    if f.quote_asset == asset:
        d += -f.quote_qty if buy else f.quote_qty
    if f.commission_asset == asset:
        d -= f.commission
    return d
