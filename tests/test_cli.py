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
        "kill", "pause", "resume", "status", "capital", "adopt", "assets", "data",
    }  # fmt: skip
    assert set(COMMANDS) == expected


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "--once"],
        ["status"],
        ["kill"],
        ["experiment", "--id", "E1"],
        ["backtest", "--end", "2025-06-30"],
    ],
)
def test_placeholder_commands_validate_config_then_exit(argv, capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["--config", str(CONFIG_PATH), *argv]) == EXIT_NOT_IMPLEMENTED
    assert "尚未实现" in capsys.readouterr().err


def test_bad_config_returns_config_error(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["--config", str(tmp_path / "missing.yaml"), "status"]) == EXIT_CONFIG_ERROR
    assert "找不到配置文件" in capsys.readouterr().err


def test_research_command_end_in_holdout_is_rejected(capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["--config", str(CONFIG_PATH), "backtest", "--end", "2025-07-01"]) == EXIT_HOLDOUT_VIOLATION
    assert "保留集" in capsys.readouterr().err


def test_holdout_requires_final_and_today(capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["--config", str(CONFIG_PATH), "holdout"]) == EXIT_HOLDOUT_VIOLATION
    assert (
        main(["--config", str(CONFIG_PATH), "holdout", "--final", "--confirm", "2000-01-01"])
        == EXIT_HOLDOUT_VIOLATION
    )
    today = datetime.now(UTC).date().isoformat()
    assert (
        main(["--config", str(CONFIG_PATH), "holdout", "--final", "--confirm", today]) == EXIT_NOT_IMPLEMENTED
    )
