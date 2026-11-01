"""v1：初始表结构（spec §8）。

用显式 DDL 而不是 `metadata.create_all`：以后模型增加字段时，新库也必须先建 v1 再逐个迁移，
否则后续迁移中的 ALTER TABLE 会因为字段已存在而失败。全部语句带 IF NOT EXISTS，可重复执行。
"""

from __future__ import annotations

from sqlalchemy.engine import Connection

DESCRIPTION = "初始表结构"

STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS accounts (
        account_id TEXT NOT NULL,
        venue TEXT NOT NULL,
        account_type TEXT NOT NULL,
        env TEXT NOT NULL,
        PRIMARY KEY (account_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS strategies (
        strategy_id TEXT NOT NULL,
        name TEXT NOT NULL,
        version TEXT NOT NULL,
        config_hash TEXT NOT NULL,
        PRIMARY KEY (strategy_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        run_date TEXT NOT NULL,
        signal_date TEXT,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        error TEXT,
        PRIMARY KEY (run_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS regime_state (
        strategy_id TEXT NOT NULL,
        date TEXT NOT NULL,
        symbol TEXT NOT NULL,
        close TEXT NOT NULL,
        sma TEXT,
        state TEXT NOT NULL,
        switched BOOLEAN NOT NULL,
        PRIMARY KEY (strategy_id, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_targets (
        strategy_id TEXT NOT NULL,
        sleeve TEXT NOT NULL,
        date TEXT NOT NULL,
        symbol TEXT NOT NULL,
        kind TEXT NOT NULL,
        weight TEXT NOT NULL,
        PRIMARY KEY (strategy_id, sleeve, date, symbol, kind)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stop_events (
        id INTEGER NOT NULL,
        strategy_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        date TEXT NOT NULL,
        close TEXT NOT NULL,
        stop_line TEXT NOT NULL,
        cooldown_until TEXT NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS order_intents (
        cid TEXT NOT NULL,
        run_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        kind TEXT NOT NULL,
        run_date TEXT NOT NULL,
        seq INTEGER NOT NULL,
        attempt INTEGER NOT NULL,
        intended_qty TEXT NOT NULL,
        limit_price TEXT NOT NULL,
        status TEXT NOT NULL,
        exchange_order_id INTEGER,
        error_code INTEGER,
        executed_qty TEXT NOT NULL,
        cumm_quote_qty TEXT NOT NULL,
        base_asset TEXT NOT NULL,
        base_balance_before TEXT,
        note TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (cid)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_order_intents_status ON order_intents (strategy_id, account_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_order_intents_run ON order_intents (run_id)",
    """
    CREATE TABLE IF NOT EXISTS orders (
        cid TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        order_id INTEGER,
        symbol TEXT NOT NULL,
        side TEXT,
        status TEXT NOT NULL,
        executed_qty TEXT NOT NULL,
        cumm_quote_qty TEXT NOT NULL,
        expiry_reason TEXT,
        raw_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (cid)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fills (
        symbol TEXT NOT NULL,
        trade_id INTEGER NOT NULL,
        cid TEXT,
        strategy_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        order_id INTEGER NOT NULL,
        side TEXT NOT NULL,
        base_asset TEXT NOT NULL,
        quote_asset TEXT NOT NULL,
        price TEXT NOT NULL,
        qty TEXT NOT NULL,
        quote_qty TEXT NOT NULL,
        commission TEXT NOT NULL,
        commission_asset TEXT NOT NULL,
        time TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        PRIMARY KEY (symbol, trade_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_fills_cid ON fills (cid)",
    "CREATE INDEX IF NOT EXISTS ix_fills_recorded ON fills (strategy_id, account_id, recorded_at)",
    """
    CREATE TABLE IF NOT EXISTS positions (
        account_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        asset TEXT NOT NULL,
        managed BOOLEAN NOT NULL,
        qty TEXT NOT NULL,
        entry_date TEXT,
        highest_close TEXT,
        dust BOOLEAN NOT NULL,
        frozen BOOLEAN NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (account_id, strategy_id, asset)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS capital_flows (
        id INTEGER NOT NULL,
        account_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        time TEXT NOT NULL,
        kind TEXT NOT NULL,
        amount_usdt TEXT NOT NULL,
        nav_per_unit TEXT NOT NULL,
        units_delta TEXT NOT NULL,
        note TEXT,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS nav_snapshots (
        id INTEGER NOT NULL,
        strategy_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        run_id TEXT,
        time TEXT NOT NULL,
        kind TEXT NOT NULL,
        nav_usdt TEXT NOT NULL,
        units TEXT NOT NULL,
        nav_per_unit TEXT NOT NULL,
        gross_exposure TEXT NOT NULL,
        details_json TEXT NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS system_state (
        strategy_id TEXT NOT NULL,
        state TEXT NOT NULL,
        since TEXT NOT NULL,
        reason TEXT,
        PRIMARY KEY (strategy_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS asset_review (
        asset TEXT NOT NULL,
        status TEXT NOT NULL,
        first_seen TEXT NOT NULL,
        last_notified TEXT,
        info_json TEXT NOT NULL,
        PRIMARY KEY (asset)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER NOT NULL,
        time TEXT NOT NULL,
        level TEXT NOT NULL,
        type TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        run_id TEXT,
        payload_json TEXT NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_events_type ON events (strategy_id, type, time)",
    """
    CREATE TABLE IF NOT EXISTS id_sequences (
        strategy_id TEXT NOT NULL,
        run_date TEXT NOT NULL,
        next_seq INTEGER NOT NULL,
        PRIMARY KEY (strategy_id, run_date)
    )
    """,
)


def upgrade(conn: Connection) -> None:
    for stmt in STATEMENTS:
        conn.exec_driver_sql(stmt)
