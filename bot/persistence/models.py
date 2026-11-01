"""state.db 的表结构（spec §8）。SQLite + SQLAlchemy 2.x。

- 金额与数量用 TEXT 存十进制字符串（DecimalText），时间用 UTC ISO 8601（UtcDateTime），日期用 YYYY-MM-DD。
- 表结构变更只能通过 migrations/ 中的版本号迁移完成；这里的模型必须与迁移后的结构一致（有测试检查）。
- 相对 spec §8 的补充字段见 doc/decisions_m5.md。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class DecimalText(TypeDecorator[Decimal]):
    """Decimal ↔ TEXT（不经过 float，不用科学计数法）。"""

    impl = String
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if not isinstance(value, Decimal):
            raise TypeError(f"金额/数量字段只接受 Decimal，实际为 {type(value).__name__}")
        if not value.is_finite():
            raise ValueError(f"金额/数量字段不接受 {value}")
        return format(value, "f")

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        return None if value is None else Decimal(str(value))


class UtcDateTime(TypeDecorator[dt.datetime]):
    """带时区的 datetime ↔ UTC ISO 8601 文本。拒绝 naive datetime。"""

    impl = String
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if not isinstance(value, dt.datetime) or value.tzinfo is None:
            raise ValueError("时间字段必须是带时区的 datetime（一律 UTC）")
        return value.astimezone(dt.UTC).isoformat(timespec="microseconds")

    def process_result_value(self, value: Any, dialect: Dialect) -> dt.datetime | None:
        return None if value is None else dt.datetime.fromisoformat(str(value))


class IsoDate(TypeDecorator[dt.date]):
    """date ↔ YYYY-MM-DD。"""

    impl = String
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, dt.datetime) or not isinstance(value, dt.date):
            raise TypeError("日期字段只接受 datetime.date")
        return value.isoformat()

    def process_result_value(self, value: Any, dialect: Dialect) -> dt.date | None:
        return None if value is None else dt.date.fromisoformat(str(value))


class Base(DeclarativeBase):
    pass


class AccountRow(Base):
    __tablename__ = "accounts"
    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    venue: Mapped[str] = mapped_column(String)
    account_type: Mapped[str] = mapped_column(String)  # SPOT / USDM_FUTURES
    env: Mapped[str] = mapped_column(String)


class StrategyRow(Base):
    __tablename__ = "strategies"
    strategy_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    version: Mapped[str] = mapped_column(String)
    config_hash: Mapped[str] = mapped_column(String)


class RunRow(Base):
    __tablename__ = "runs"
    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String)
    account_id: Mapped[str] = mapped_column(String)
    run_date: Mapped[dt.date] = mapped_column(IsoDate)
    signal_date: Mapped[dt.date | None] = mapped_column(IsoDate, nullable=True)
    status: Mapped[str] = mapped_column(String)  # STARTED/PLANNED/EXECUTING/DONE/FAILED/SKIPPED
    started_at: Mapped[dt.datetime] = mapped_column(UtcDateTime)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class RegimeStateRow(Base):
    __tablename__ = "regime_state"
    strategy_id: Mapped[str] = mapped_column(String, primary_key=True)
    date: Mapped[dt.date] = mapped_column(IsoDate, primary_key=True)
    symbol: Mapped[str] = mapped_column(String)
    close: Mapped[Decimal] = mapped_column(DecimalText)
    sma: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    state: Mapped[str] = mapped_column(String)  # RISK_ON / RISK_OFF
    switched: Mapped[bool] = mapped_column(Boolean)


class DailyTargetRow(Base):
    __tablename__ = "daily_targets"
    strategy_id: Mapped[str] = mapped_column(String, primary_key=True)
    sleeve: Mapped[str] = mapped_column(String, primary_key=True)
    date: Mapped[dt.date] = mapped_column(IsoDate, primary_key=True)
    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, primary_key=True)  # D / S / W
    weight: Mapped[Decimal] = mapped_column(DecimalText)


class StopEventRow(Base):
    __tablename__ = "stop_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(String)
    account_id: Mapped[str] = mapped_column(String)
    symbol: Mapped[str] = mapped_column(String)
    date: Mapped[dt.date] = mapped_column(IsoDate)
    close: Mapped[Decimal] = mapped_column(DecimalText)
    stop_line: Mapped[Decimal] = mapped_column(DecimalText)
    cooldown_until: Mapped[dt.date] = mapped_column(IsoDate)


class OrderIntentRow(Base):
    """下单意图（write-ahead）。每次尝试一行，同一意图的各次尝试共享 (strategy_id, run_date, seq)。"""

    __tablename__ = "order_intents"
    cid: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(String)
    strategy_id: Mapped[str] = mapped_column(String)
    account_id: Mapped[str] = mapped_column(String)
    symbol: Mapped[str] = mapped_column(String)
    side: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)  # S / B / K
    run_date: Mapped[dt.date] = mapped_column(IsoDate)
    seq: Mapped[int] = mapped_column(Integer)
    attempt: Mapped[int] = mapped_column(Integer)
    intended_qty: Mapped[Decimal] = mapped_column(DecimalText)
    limit_price: Mapped[Decimal] = mapped_column(DecimalText)
    status: Mapped[str] = mapped_column(String)  # IntentStatus
    exchange_order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    executed_qty: Mapped[Decimal] = mapped_column(DecimalText)
    cumm_quote_qty: Mapped[Decimal] = mapped_column(DecimalText)
    base_asset: Mapped[str] = mapped_column(String)
    base_balance_before: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[dt.datetime] = mapped_column(UtcDateTime)


class OrderRow(Base):
    """交易所返回的原始订单。"""

    __tablename__ = "orders"
    cid: Mapped[str] = mapped_column(String, primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String)
    account_id: Mapped[str] = mapped_column(String)
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    symbol: Mapped[str] = mapped_column(String)
    side: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String)
    executed_qty: Mapped[Decimal] = mapped_column(DecimalText)
    cumm_quote_qty: Mapped[Decimal] = mapped_column(DecimalText)
    expiry_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(UtcDateTime)


class FillRow(Base):
    __tablename__ = "fills"
    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    trade_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cid: Mapped[str | None] = mapped_column(String, nullable=True)
    strategy_id: Mapped[str] = mapped_column(String)
    account_id: Mapped[str] = mapped_column(String)
    order_id: Mapped[int] = mapped_column(Integer)
    side: Mapped[str] = mapped_column(String)
    base_asset: Mapped[str] = mapped_column(String)
    quote_asset: Mapped[str] = mapped_column(String)
    price: Mapped[Decimal] = mapped_column(DecimalText)
    qty: Mapped[Decimal] = mapped_column(DecimalText)
    quote_qty: Mapped[Decimal] = mapped_column(DecimalText)
    commission: Mapped[Decimal] = mapped_column(DecimalText)
    commission_asset: Mapped[str] = mapped_column(String)
    time: Mapped[dt.datetime] = mapped_column(UtcDateTime)  # 交易所成交时间
    recorded_at: Mapped[dt.datetime] = mapped_column(UtcDateTime)  # 本地写入时间（对账用，与本地时钟比较）


class PositionRow(Base):
    __tablename__ = "positions"
    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String, primary_key=True)
    asset: Mapped[str] = mapped_column(String, primary_key=True)
    managed: Mapped[bool] = mapped_column(Boolean)
    qty: Mapped[Decimal] = mapped_column(DecimalText)
    entry_date: Mapped[dt.date | None] = mapped_column(IsoDate, nullable=True)
    highest_close: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    dust: Mapped[bool] = mapped_column(Boolean)
    frozen: Mapped[bool] = mapped_column(Boolean)
    updated_at: Mapped[dt.datetime] = mapped_column(UtcDateTime)


class CapitalFlowRow(Base):
    __tablename__ = "capital_flows"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String)
    strategy_id: Mapped[str] = mapped_column(String)
    time: Mapped[dt.datetime] = mapped_column(UtcDateTime)
    kind: Mapped[str] = mapped_column(String)  # DEPOSIT / WITHDRAW / ADJUST（第二阶段 FUNDING）
    amount_usdt: Mapped[Decimal] = mapped_column(DecimalText)
    nav_per_unit: Mapped[Decimal] = mapped_column(DecimalText)
    units_delta: Mapped[Decimal] = mapped_column(DecimalText)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class NavSnapshotRow(Base):
    __tablename__ = "nav_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(String)
    account_id: Mapped[str] = mapped_column(String)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    time: Mapped[dt.datetime] = mapped_column(UtcDateTime)
    kind: Mapped[str] = mapped_column(String)  # PRE_TRADE / POST_TRADE
    nav_usdt: Mapped[Decimal] = mapped_column(DecimalText)
    units: Mapped[Decimal] = mapped_column(DecimalText)
    nav_per_unit: Mapped[Decimal] = mapped_column(DecimalText)
    gross_exposure: Mapped[Decimal] = mapped_column(DecimalText)
    details_json: Mapped[str] = mapped_column(Text)


class SystemStateRow(Base):
    __tablename__ = "system_state"
    strategy_id: Mapped[str] = mapped_column(String, primary_key=True)
    state: Mapped[str] = mapped_column(String)  # RUNNING / PAUSED / HALTED / KILLED
    since: Mapped[dt.datetime] = mapped_column(UtcDateTime)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ExternalBalanceRow(Base):
    """外部基线：策略接手之前账户里已有的数量（D-062）。"""

    __tablename__ = "external_balances"
    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String, primary_key=True)
    asset: Mapped[str] = mapped_column(String, primary_key=True)
    qty: Mapped[Decimal] = mapped_column(DecimalText)
    recorded_at: Mapped[dt.datetime] = mapped_column(UtcDateTime)


class AssetReviewRow(Base):
    __tablename__ = "asset_review"
    asset: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String)  # PENDING / NOTIFIED
    first_seen: Mapped[dt.datetime] = mapped_column(UtcDateTime)
    last_notified: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    info_json: Mapped[str] = mapped_column(Text)


class EventRow(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    time: Mapped[dt.datetime] = mapped_column(UtcDateTime)
    level: Mapped[str] = mapped_column(String)  # INFO / WARN / CRITICAL
    type: Mapped[str] = mapped_column(String)
    strategy_id: Mapped[str] = mapped_column(String)
    account_id: Mapped[str] = mapped_column(String)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text)


class IdSequenceRow(Base):
    __tablename__ = "id_sequences"
    strategy_id: Mapped[str] = mapped_column(String, primary_key=True)
    run_date: Mapped[dt.date] = mapped_column(IsoDate, primary_key=True)
    next_seq: Mapped[int] = mapped_column(Integer)
