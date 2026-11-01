"""state.db 的版本号迁移（spec §8：schema_version 表 + 本目录；v1 不引入 Alembic）。

- 每个迁移是一个模块，提供 DESCRIPTION 和 upgrade(conn)；在 MIGRATIONS 中按版本号登记。
- migrate() 只执行版本号大于当前版本的迁移；每个迁移在自己的事务里执行，并写入 schema_version。
- 迁移语句必须可以重复执行（IF NOT EXISTS 等），这样中途失败后重跑也是安全的。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from bot.persistence.migrations import v001_initial


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    upgrade: Callable[[Connection], None]


MIGRATIONS: tuple[Migration, ...] = (Migration(1, v001_initial.DESCRIPTION, v001_initial.upgrade),)

LATEST_VERSION = MIGRATIONS[-1].version

_CREATE_VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    description TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    PRIMARY KEY (version)
)
"""


def current_version(conn: Connection) -> int:
    conn.exec_driver_sql(_CREATE_VERSION_TABLE)
    v = conn.execute(text("SELECT COALESCE(MAX(version), 0) FROM schema_version")).scalar_one()
    return int(v)


def migrate(engine: Engine) -> int:
    """把数据库升级到最新版本，返回升级后的版本号。可以重复调用。"""
    with engine.begin() as conn:
        version = current_version(conn)
    if version > LATEST_VERSION:
        raise RuntimeError(f"state.db 的版本 {version} 比程序支持的 {LATEST_VERSION} 新，请升级程序")
    for m in MIGRATIONS:
        if m.version <= version:
            continue
        with engine.begin() as conn:
            m.upgrade(conn)
            conn.execute(
                text("INSERT INTO schema_version (version, description, applied_at) VALUES (:v, :d, :t)"),
                {"v": m.version, "d": m.description, "t": datetime.now(UTC).isoformat()},
            )
        version = m.version
    return version
