#!/usr/bin/env bash
# 一条命令运行 lint、格式检查、类型检查和测试（Linux / WSL / Git Bash）。
set -euo pipefail
cd "$(dirname "$0")/.."
if command -v uv >/dev/null 2>&1; then UV=(uv); else UV=(python3 -m uv); fi
"${UV[@]}" run ruff check .
"${UV[@]}" run ruff format --check .
"${UV[@]}" run mypy
"${UV[@]}" run pytest
echo "全部检查通过"
