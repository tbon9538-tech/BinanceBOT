"""保留集守卫（spec §3.6，doc/DECISIONS.md D-011、D-018）。

- 研究类命令（backtest / experiment / walkforward / bootstrap）只能通过 HoldoutGuard 读取数据，
  `date >= holdout_start` 的行一律被过滤，请求的结束日期落在保留集内则直接报错。
- 只有 `holdout --final` 可以读取保留集；每次运行都追加记录到 doc/research/holdout_runs.log。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from bot.domain.errors import BotError

DEFAULT_HOLDOUT_LOG = Path("doc/research/holdout_runs.log")


class HoldoutViolation(BotError):
    """研究代码试图读取保留集。"""


@dataclass(frozen=True, slots=True)
class HoldoutGuard:
    holdout_start: date

    def check_end(self, end: date) -> None:
        if end >= self.holdout_start:
            raise HoldoutViolation(
                f"研究命令的结束日期 {end} 落在保留集内（≥ {self.holdout_start}）。"
                "保留集只能由 `python -m bot holdout --final` 读取（spec §3.6）。"
            )

    @property
    def max_date_str(self) -> str:
        """SQL 过滤用：date < holdout_start。"""
        return self.holdout_start.isoformat()

    def filter_frame(self, df: pd.DataFrame) -> pd.DataFrame:
        """过滤以日期为索引（DatetimeIndex）或含 date 列（YYYY-MM-DD）的数据。"""
        if isinstance(df.index, pd.DatetimeIndex):
            return df.loc[df.index < pd.Timestamp(self.holdout_start)]
        if "date" in df.columns:
            return df.loc[df["date"] < self.max_date_str]
        raise ValueError("无法识别日期：需要 DatetimeIndex 或 date 列")

    def assert_clean(self, df: pd.DataFrame) -> None:
        """断言数据中不含保留集（防御性检查）。"""
        if len(self.filter_frame(df)) != len(df):
            raise HoldoutViolation("数据中包含保留集日期，研究代码不得使用")


class HoldoutLog:
    """保留集运行记录（追加写，一行一条 JSON）。"""

    def __init__(self, path: Path = DEFAULT_HOLDOUT_LOG) -> None:
        self.path = path

    def previous_runs(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def authorize(self, *, final: bool, confirm: date | None, today: date, not_first_run: bool) -> bool:
        """检查 `holdout --final` 的启动条件。返回 True 表示这是首次运行。"""
        if not final:
            raise HoldoutViolation("读取保留集必须显式加 --final")
        if confirm != today:
            raise HoldoutViolation(f"--confirm 必须等于当天日期 {today.isoformat()}")
        first = not self.previous_runs()
        if not first and not not_first_run:
            raise HoldoutViolation(
                "保留集已经运行过。再次运行必须加 --i-know-this-is-not-the-first-run，"
                "并且报告中会醒目标注\"非首次运行\"（spec §3.6）"
            )
        return first

    def append(self, *, git_commit: str, config_hash: str, summary: dict[str, Any], first_run: bool) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "time": datetime.now(UTC).isoformat(timespec="seconds"),
            "git_commit": git_commit,
            "config_hash": config_hash,
            "first_run": first_run,
            "summary": summary,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
