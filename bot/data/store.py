"""行情缓存 data/market.db（spec §3.2）。

- MarketStore：完整读写，供数据同步与实盘使用。
- ResearchMarketView：研究专用只读视图，所有读取都经过 HoldoutGuard 过滤（spec §3.6）。
  研究引擎只能使用 ResearchMarketView。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import (
    Boolean,
    Column,
    Engine,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    event,
    func,
    select,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from bot.data.holdout import HoldoutGuard
from bot.data.klines import KLINE_COLUMNS, empty_frame, to_float_frame

metadata = MetaData()

klines_1d = Table(
    "klines_1d",
    metadata,
    Column("symbol", String, primary_key=True),
    Column("open_time_ms", Integer, primary_key=True),
    Column("date", String, nullable=False),
    Column("open", String, nullable=False),
    Column("high", String, nullable=False),
    Column("low", String, nullable=False),
    Column("close", String, nullable=False),
    Column("volume", String, nullable=False),
    Column("quote_volume", String, nullable=False),
    Column("trades", Integer, nullable=False),
    Column("close_time_ms", Integer, nullable=False),
    Column("source", String, nullable=False),  # 'vision' | 'rest'
    Column("ingested_at", String, nullable=False),
)

vision_files = Table(
    "vision_files",
    metadata,
    Column("key", String, primary_key=True),
    Column("sha256", String, nullable=False),
    Column("rows", Integer, nullable=False),
    Column("ingested_at", String, nullable=False),
)

symbols_meta = Table(
    "symbols_meta",
    metadata,
    Column("symbol", String, primary_key=True),
    Column("base_asset", String, nullable=False),
    Column("quote_asset", String, nullable=False),
    Column("status", String, nullable=False),  # exchangeInfo 状态；不在 exchangeInfo 中为 NOT_IN_EXCHANGEINFO
    Column("is_spot_trading_allowed", Boolean, nullable=False),
    Column("updated_at", String, nullable=False),
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _enable_wal(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn: Any, _record: Any) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


class MarketStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{self.path.as_posix()}")
        _enable_wal(self.engine)
        metadata.create_all(self.engine)

    # ---- 写入 ----

    def upsert_klines(self, symbol: str, raw: pd.DataFrame, source: str) -> int:
        """幂等写入。同一 (symbol, open_time_ms) 只保存一行；REST 数据不覆盖 vision 数据。"""
        if source not in ("vision", "rest"):
            raise ValueError(f"未知数据来源：{source}")
        if raw.empty:
            return 0
        now = _now_iso()
        records = [
            {**{c: row[c] for c in KLINE_COLUMNS}, "symbol": symbol, "source": source, "ingested_at": now}
            for row in raw.to_dict("records")
        ]
        for r in records:
            r["open_time_ms"] = int(r["open_time_ms"])
            r["trades"] = int(r["trades"])
            r["close_time_ms"] = int(r["close_time_ms"])
        stmt = sqlite_insert(klines_1d)
        update_cols = {c: stmt.excluded[c] for c in [*KLINE_COLUMNS[1:], "source", "ingested_at"]}
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "open_time_ms"],
            set_=update_cols,
            where=(stmt.excluded.source == "vision") | (klines_1d.c.source == "rest"),
        )
        with self.engine.begin() as conn:
            conn.execute(stmt, records)
        return len(records)

    def mark_file_ingested(self, key: str, sha256: str, rows: int) -> None:
        stmt = sqlite_insert(vision_files).values(key=key, sha256=sha256, rows=rows, ingested_at=_now_iso())
        stmt = stmt.on_conflict_do_update(
            index_elements=["key"], set_={"sha256": sha256, "rows": rows, "ingested_at": _now_iso()}
        )
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def ingested_files(self, prefix: str = "") -> set[str]:
        q = select(vision_files.c.key)
        if prefix:
            q = q.where(vision_files.c.key.like(prefix + "%"))
        with self.engine.connect() as conn:
            return {r[0] for r in conn.execute(q)}

    def upsert_symbol_meta(self, records: Iterable[Mapping[str, Any]]) -> None:
        now = _now_iso()
        rows = [{**dict(r), "updated_at": now} for r in records]
        if not rows:
            return
        stmt = sqlite_insert(symbols_meta)
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol"],
            set_={
                c: stmt.excluded[c]
                for c in ["base_asset", "quote_asset", "status", "is_spot_trading_allowed", "updated_at"]
            },
        )
        with self.engine.begin() as conn:
            conn.execute(stmt, rows)

    # ---- 读取（完整访问，研究代码不得使用；研究请用 ResearchMarketView）----

    def last_date(self, symbol: str) -> str | None:
        q = select(func.max(klines_1d.c.date)).where(klines_1d.c.symbol == symbol)
        with self.engine.connect() as conn:
            v = conn.execute(q).scalar()
        return str(v) if v is not None else None

    def symbols(self) -> list[str]:
        with self.engine.connect() as conn:
            return [
                r[0] for r in conn.execute(select(klines_1d.c.symbol).distinct().order_by(klines_1d.c.symbol))
            ]

    def symbol_meta(self) -> pd.DataFrame:
        with self.engine.connect() as conn:
            rows = conn.execute(select(symbols_meta)).mappings().all()
        return pd.DataFrame([dict(r) for r in rows])

    def read_raw(
        self, symbol: str, start: str | None = None, end_exclusive: str | None = None
    ) -> pd.DataFrame:
        """raw frame（十进制字符串），按日期升序。日期为 YYYY-MM-DD 字符串，end 不含。"""
        q = select(*[klines_1d.c[c] for c in KLINE_COLUMNS]).where(klines_1d.c.symbol == symbol)
        if start is not None:
            q = q.where(klines_1d.c.date >= start)
        if end_exclusive is not None:
            q = q.where(klines_1d.c.date < end_exclusive)
        q = q.order_by(klines_1d.c.open_time_ms)
        with self.engine.connect() as conn:
            rows = conn.execute(q).all()
        if not rows:
            return empty_frame()
        return pd.DataFrame([tuple(r) for r in rows], columns=KLINE_COLUMNS)

    def read_symbol(
        self, symbol: str, start: str | None = None, end_exclusive: str | None = None
    ) -> pd.DataFrame:
        """float 数据，DatetimeIndex。"""
        return to_float_frame(self.read_raw(symbol, start, end_exclusive))

    def coverage_by_year(self) -> pd.DataFrame:
        """每年的交易对数与"交易对·天"数（只统计数量，不含价格）。"""
        year = func.substr(klines_1d.c.date, 1, 4)
        q = (
            select(
                year.label("year"),
                func.count(klines_1d.c.symbol.distinct()).label("symbols"),
                func.count().label("rows"),
            )
            .group_by(year)
            .order_by(year)
        )
        with self.engine.connect() as conn:
            rows = conn.execute(q).all()
        return pd.DataFrame([tuple(r) for r in rows], columns=["year", "symbols", "rows"])


class ResearchMarketView:
    """研究专用只读视图：所有读取都过滤掉保留集（spec §3.6）。"""

    def __init__(self, store: MarketStore, guard: HoldoutGuard) -> None:
        self._store = store
        self.guard = guard

    def _end(self, end: date | None) -> str:
        if end is None:
            return self.guard.max_date_str
        self.guard.check_end(end)
        return (end + timedelta(days=1)).isoformat()

    def read_symbol(self, symbol: str, start: date | None = None, end: date | None = None) -> pd.DataFrame:
        """[start, end] 内的 float 数据（end 含当天），永远不包含保留集。"""
        df = self._store.read_symbol(symbol, start.isoformat() if start else None, self._end(end))
        self.guard.assert_clean(df)
        return df

    def symbols(self) -> list[str]:
        return self._store.symbols()

    def symbol_meta(self) -> pd.DataFrame:
        return self._store.symbol_meta()
