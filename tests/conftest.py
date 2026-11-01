from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "config.yaml"
ASSETS_PATH = ROOT / "config" / "assets.yaml"


@pytest.fixture
def base_cfg() -> dict[str, Any]:
    """仓库自带 config.yaml 的原始字典（每个测试拿到独立副本）。"""
    return copy.deepcopy(yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")))


@pytest.fixture
def write_yaml(tmp_path: Path):  # type: ignore[no-untyped-def]
    def _write(data: dict[str, Any], name: str = "config.yaml") -> Path:
        p = tmp_path / name
        p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        return p

    return _write
