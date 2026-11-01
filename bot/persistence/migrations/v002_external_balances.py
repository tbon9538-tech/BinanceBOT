"""v2：外部基线（D-062）。

记录"策略接手之前账户里已有的数量"。策略视角的余额 = 交易所余额 − 外部基线，
这样账户里原有的资产（testnet 预置资产、用户自己的币）不会被当成策略持仓。
"""

from __future__ import annotations

from sqlalchemy.engine import Connection

DESCRIPTION = "外部基线 external_balances"

STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS external_balances (
        account_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        asset TEXT NOT NULL,
        qty TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        PRIMARY KEY (account_id, strategy_id, asset)
    )
    """,
)


def upgrade(conn: Connection) -> None:
    for stmt in STATEMENTS:
        conn.exec_driver_sql(stmt)
