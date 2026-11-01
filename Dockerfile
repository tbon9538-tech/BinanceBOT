# 币安现货趋势–动量机器人：生产镜像（spec §16、dev_plan M7）
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    TZ=UTC

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY bot ./bot
COPY config ./config
COPY scripts ./scripts

# 运行时数据、日志、报告放在卷里；容器重启后状态不丢失
VOLUME ["/app/data", "/app/logs", "/app/reports"]

# 以非 root 用户运行
RUN useradd --create-home --uid 10001 bot && mkdir -p /app/data /app/logs /app/reports && chown -R bot /app
USER bot

ENTRYPOINT ["uv", "run", "--no-sync", "python", "-m", "bot"]
CMD ["run", "--daemon"]
