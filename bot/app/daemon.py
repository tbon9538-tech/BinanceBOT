"""常驻模式（spec §0、§5.5）：每天在 run_time_utc 执行一次 run_once；每 60 秒检查一次 KILL 文件。

推荐部署方式仍是由 cron / 容器调度触发 `run --once`；常驻模式供 Docker `restart: unless-stopped` 使用。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from filelock import FileLock, Timeout

from bot.app.run_once import DailyRunner

log = logging.getLogger(__name__)

KILL_CHECK_SEC = 60.0


def next_run_at(now: datetime, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


def run_daemon(
    runner: DailyRunner,
    run_time_utc: str,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    max_cycles: int | None = None,
) -> int:
    """返回退出码。KILL 后退出；max_cycles 只用于测试。"""
    cycles = 0
    log.info("常驻模式启动：每天 %s UTC 运行", run_time_utc)
    while max_cycles is None or cycles < max_cycles:
        target = next_run_at(now(), run_time_utc)
        while now() < target:
            if runner.d.kill_file.exists():
                lock = FileLock(str(runner.d.lock_file))
                try:
                    lock.acquire(timeout=0)
                except Timeout:
                    sleep(KILL_CHECK_SEC)
                    continue
                try:
                    runner.kill(reason="常驻模式检测到 KILL 文件")
                finally:
                    lock.release()
                return 0
            sleep(min(KILL_CHECK_SEC, max((target - now()).total_seconds(), 0.0)))
        res = runner.run_once()
        log.info("每日运行完成：%s %s", res.run_id, res.status)
        if res.status == "KILLED":
            return 0
        cycles += 1
    return 0
