"""日志：按天滚动的文件 + 控制台；统一的密钥过滤器（spec §12）。

所有日志记录都经过 SecretFilter：环境变量中的密钥、Telegram token 以完整值出现时会被替换；
API Key 只保留前 4 位与后 4 位。
"""

from __future__ import annotations

import logging
import os
import re
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

SECRET_ENV_VARS = ("BINANCE_API_SECRET", "TELEGRAM_BOT_TOKEN", "HEARTBEAT_URL")
MASKED_ENV_VARS = ("BINANCE_API_KEY",)
_SIGNATURE_RE = re.compile(r"(signature=)[0-9A-Za-z%+/=_-]+")
_TG_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]{20,}")


def mask(value: str) -> str:
    return f"{value[:4]}…{value[-4:]}" if len(value) > 8 else "****"


class SecretFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self.replacements: list[tuple[str, str]] = []
        for name in SECRET_ENV_VARS:
            v = os.environ.get(name, "")
            if len(v) >= 8:
                self.replacements.append((v, "****"))
        for name in MASKED_ENV_VARS:
            v = os.environ.get(name, "")
            if len(v) >= 8:
                self.replacements.append((v, mask(v)))

    def scrub(self, text: str) -> str:
        for secret, rep in self.replacements:
            text = text.replace(secret, rep)
        text = _SIGNATURE_RE.sub(r"\1****", text)
        return _TG_TOKEN_RE.sub("bot****", text)

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        clean = self.scrub(msg)
        if clean != msg:
            record.msg, record.args = clean, None
        return True


def setup_logging(level: str = "INFO", log_dir: Path | None = None, retention_days: int = 90) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    secret = SecretFilter()
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    console.addFilter(secret)
    root.addHandler(console)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = TimedRotatingFileHandler(
            log_dir / "bot.log", when="midnight", backupCount=retention_days, encoding="utf-8", utc=True
        )
        fh.setFormatter(fmt)
        fh.addFilter(secret)
        root.addHandler(fh)
    logging.getLogger("httpx").setLevel(logging.WARNING)
