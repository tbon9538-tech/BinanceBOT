"""状态库自动备份（ROADMAP A3）：在线备份、完整性、保留份数、恢复后内容一致。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from bot.persistence.backup import backup_sqlite, list_backups
from bot.persistence.repo import StateRepo

D = Decimal
T0 = datetime(2026, 11, 2, 0, 10, 12, tzinfo=UTC)


def _populated(path: Path) -> StateRepo:
    repo = StateRepo.open(path, clock=lambda: T0)
    repo.start_run("mom1:2026-11-02", strategy_id="mom1", account_id="spot-main", run_date=date(2026, 11, 2))
    repo.set_run_status("mom1:2026-11-02", "DONE")
    repo.set_system_state("mom1", "RUNNING")
    repo.upsert_position("spot-main", "mom1", "BTC", qty=D("0.00302697"), managed=True)
    repo.upsert_position("spot-main", "mom1", "USDC", qty=D("5000"), managed=False)
    repo.append_nav_snapshot(
        strategy_id="mom1", account_id="spot-main", kind="POST_TRADE", nav_usdt=D("4999.98"),
        units=D("5000"), nav_per_unit=D("0.999996"), gross_exposure=D("0.06"), run_id="mom1:2026-11-02",
    )  # fmt: skip
    return repo


def _summary(repo: StateRepo) -> tuple[object, ...]:
    st = repo.get_system_state("mom1")
    last = repo.last_run("mom1")
    snap = repo.latest_nav_snapshot("mom1", "spot-main")
    pos = sorted((p.asset, p.qty, p.managed) for p in repo.list_positions("spot-main", "mom1"))
    assert st and last and snap
    return st.state, last.run_id, last.status, snap.nav_usdt, snap.nav_per_unit, tuple(pos)


def test_backup_restores_identical_state(tmp_path: Path) -> None:
    src = tmp_path / "state-demo.db"
    repo = _populated(src)  # 连接保持打开（WAL 中有已提交但未检查点的内容），模拟运行中的进程
    dest = backup_sqlite(src, tmp_path / "backup", now=T0, keep=30)
    assert dest.name == "state-demo-20261102T001012Z.db"
    assert sorted(p.name for p in dest.parent.iterdir()) == [dest.name]  # 没有 .tmp / -wal / -shm 残留

    restored = tmp_path / "restore" / "state-demo.db"
    restored.parent.mkdir()
    restored.write_bytes(dest.read_bytes())
    copy = StateRepo.open(restored)
    assert _summary(copy) == _summary(repo)
    copy.close()
    repo.close()


def test_keeps_only_latest_n_and_ignores_other_files(tmp_path: Path) -> None:
    src = tmp_path / "state-demo.db"
    _populated(src).close()
    bdir = tmp_path / "backup"
    bdir.mkdir()
    other = [bdir / "state-testnet-20260101T000000Z.db", bdir / "notes.txt", bdir / "state-demo-manual.db"]
    for p in other:
        p.write_text("x")
    made = [backup_sqlite(src, bdir, now=T0 + timedelta(days=i), keep=3) for i in range(5)]
    assert list_backups(bdir, "state-demo") == made[-3:]
    assert all(p.exists() for p in other)  # 只清理本库、本模块命名的备份


def test_missing_source_is_an_error_not_an_empty_backup(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        backup_sqlite(tmp_path / "state-demo.db", tmp_path / "backup", now=T0, keep=30)
    assert not (tmp_path / "state-demo.db").exists()
    assert list_backups(tmp_path / "backup", "state-demo") == []
