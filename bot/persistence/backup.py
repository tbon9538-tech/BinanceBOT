"""状态库自动备份（ROADMAP A3）。

用 SQLite 在线备份 API 复制状态库：得到一致的快照（包括 WAL 中已提交的内容），不影响正在使用它的连接。
先写临时文件、通过完整性检查后再改名，目录中不会留下半份备份。
备份文件名为 `<库名>-<UTC 时间>.db`，例如 `state-demo-20261102T001012Z.db`；每个库只保留最近 keep 份。

恢复：停掉容器，把备份复制为 `data/state-<env>.db`（同时删除残留的 `-wal`、`-shm` 文件），再启动。
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


def _pattern(stem: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(stem)}-\d{{8}}T\d{{6}}Z\.db$")


def list_backups(dest_dir: Path, stem: str) -> list[Path]:
    """按时间从旧到新返回 stem 对应的备份（只认本模块生成的文件名）。"""
    if not dest_dir.is_dir():
        return []
    pat = _pattern(stem)
    return sorted(p for p in dest_dir.iterdir() if pat.match(p.name))


def backup_sqlite(src: Path, dest_dir: Path, *, now: datetime, keep: int) -> Path:
    """备份 src 到 dest_dir，删除超出 keep 份的旧备份，返回新备份的路径。"""
    if keep < 1:
        raise ValueError("keep 至少为 1")
    if not src.is_file():  # sqlite3.connect 会为不存在的路径新建空库
        raise FileNotFoundError(f"状态库不存在：{src}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{src.stem}-{now:%Y%m%dT%H%M%SZ}.db"
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.unlink(missing_ok=True)
    try:
        with closing(sqlite3.connect(src)) as s, closing(sqlite3.connect(tmp)) as d:
            s.backup(d)
            d.execute("PRAGMA journal_mode=DELETE")  # 备份是单个文件，不带 -wal / -shm
            (result,) = d.execute("PRAGMA integrity_check").fetchone()
        if result != "ok":
            raise sqlite3.DatabaseError(f"备份完整性检查失败：{result}")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    for old in list_backups(dest_dir, src.stem)[:-keep]:
        old.unlink()
    log.info("状态库已备份：%s", dest)
    return dest
