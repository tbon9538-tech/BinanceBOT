from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bot.cli import COMMANDS, EXIT_CONFIG_ERROR, EXIT_HOLDOUT_VIOLATION, EXIT_NOT_IMPLEMENTED, main
from tests.conftest import CONFIG_PATH


def test_help_lists_all_subcommands(capsys) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(SystemExit) as ei:
        main(["--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    for name in COMMANDS:
        assert name in out


def test_spec_subcommands_are_all_registered() -> None:
    # spec §11.1 列出的全部子命令
    expected = {
        "run", "backtest", "experiment", "walkforward", "bootstrap", "holdout", "dsr",
        "kill", "pause", "resume", "status", "capital", "adopt", "assets", "data", "forward", "dashboard",
    }  # fmt: skip
    assert set(COMMANDS) == expected


def test_every_command_has_a_handler() -> None:
    import bot.cli as cli

    assert set(cli.HANDLERS) == set(COMMANDS)


def test_bad_config_returns_config_error(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["--config", str(tmp_path / "missing.yaml"), "status"]) == EXIT_CONFIG_ERROR
    assert "找不到配置文件" in capsys.readouterr().err


def test_research_command_end_in_holdout_is_rejected(capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["--config", str(CONFIG_PATH), "backtest", "--end", "2026-11-01"]) == EXIT_HOLDOUT_VIOLATION
    assert "保留集" in capsys.readouterr().err


def test_holdout_requires_final_and_today(capsys, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # 绝不能在测试中真的运行保留集：用替身代替真实命令，运行记录写到临时目录
    import bot.cli as cli
    from bot.data.holdout import HoldoutLog

    monkeypatch.setitem(cli.HANDLERS, "holdout", lambda cfg, args: EXIT_NOT_IMPLEMENTED)
    monkeypatch.setattr(cli, "HoldoutLog", lambda: HoldoutLog(tmp_path / "holdout_runs.log"))
    assert main(["--config", str(CONFIG_PATH), "holdout"]) == EXIT_HOLDOUT_VIOLATION
    assert (
        main(["--config", str(CONFIG_PATH), "holdout", "--final", "--confirm", "2000-01-01"])
        == EXIT_HOLDOUT_VIOLATION
    )
    today = datetime.now(UTC).date().isoformat()
    assert (
        main(["--config", str(CONFIG_PATH), "holdout", "--final", "--confirm", today]) == EXIT_NOT_IMPLEMENTED
    )
