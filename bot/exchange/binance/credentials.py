"""API 凭据加载与签名（spec §6.1、§10.2、§13）。

- 凭据只从环境变量读取：BINANCE_API_KEY、BINANCE_KEY_TYPE（hmac | ed25519）、
  BINANCE_API_SECRET（hmac）、BINANCE_PRIVATE_KEY_PATH（ed25519，PEM）。
- `.env` 只在调用处（load_credentials 的 dotenv_path 参数）加载，import 本模块没有任何副作用。
- 日志与异常信息里绝不出现密钥和签名；API Key 只显示前 4 位和后 4 位。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from bot.domain.errors import ConfigError

log = logging.getLogger(__name__)

ENV_API_KEY = "BINANCE_API_KEY"
ENV_KEY_TYPE = "BINANCE_KEY_TYPE"
ENV_API_SECRET = "BINANCE_API_SECRET"
ENV_PRIVATE_KEY_PATH = "BINANCE_PRIVATE_KEY_PATH"

_SIGNATURE_RE = re.compile(r"(signature=)[^&\s\"']+", re.IGNORECASE)
_APIKEY_HEADER_RE = re.compile(r"(X-MBX-APIKEY['\"]?\s*[:=]\s*['\"]?)([A-Za-z0-9]+)", re.IGNORECASE)


class CredentialsError(ConfigError):
    """凭据缺失或无效。信息中不含任何密钥内容。"""


class KeyType(StrEnum):
    HMAC = "hmac"
    ED25519 = "ed25519"


class Signer(Protocol):
    def sign(self, payload: str) -> str:
        """对已经 percent-encode 的 payload（纯 ASCII）签名，返回原始签名字符串（hex 或 base64）。"""
        ...


class HmacSigner:
    """HMAC-SHA256，返回小写 hex（官方已标为 deprecated，仍支持）。"""

    __slots__ = ("_secret",)

    def __init__(self, secret: str) -> None:
        if not secret:
            raise CredentialsError("HMAC 密钥为空")
        self._secret = secret.encode("utf-8")

    def sign(self, payload: str) -> str:
        # encode("ascii")：没有 percent-encode 的非 ASCII payload 会直接报错，而不是签出 -1022
        return hmac.new(self._secret, payload.encode("ascii"), hashlib.sha256).hexdigest()

    def __repr__(self) -> str:
        return "HmacSigner(***)"


class Ed25519Signer:
    """Ed25519，返回 base64（放进 query string 前还要再 percent-encode）。"""

    __slots__ = ("_key",)

    def __init__(self, key: Ed25519PrivateKey) -> None:
        self._key = key

    @classmethod
    def from_pem(cls, data: bytes, password: bytes | None = None) -> Ed25519Signer:
        try:
            key = load_pem_private_key(data, password=password)
        except (ValueError, TypeError) as e:
            raise CredentialsError(f"无法解析 Ed25519 私钥（PEM）：{type(e).__name__}") from e
        if not isinstance(key, Ed25519PrivateKey):
            raise CredentialsError(f"私钥类型是 {type(key).__name__}，不是 Ed25519")
        return cls(key)

    def sign(self, payload: str) -> str:
        return base64.b64encode(self._key.sign(payload.encode("ascii"))).decode("ascii")

    def __repr__(self) -> str:
        return "Ed25519Signer(***)"


def mask_api_key(key: str) -> str:
    """API Key 只显示前 4 位和后 4 位（spec §12）。"""
    if len(key) < 12:
        return "****"
    return f"{key[:4]}…{key[-4:]}"


def redact_secrets(text: str) -> str:
    """去掉文本中的签名参数与 X-MBX-APIKEY 的值，供日志与异常信息使用。"""
    text = _SIGNATURE_RE.sub(r"\1***", text)
    return _APIKEY_HEADER_RE.sub(lambda m: m.group(1) + mask_api_key(m.group(2)), text)


@dataclass(frozen=True, repr=False)
class ApiCredentials:
    api_key: str
    key_type: KeyType
    signer: Signer = field(compare=False)

    def masked_key(self) -> str:
        return mask_api_key(self.api_key)

    def __repr__(self) -> str:
        return f"ApiCredentials(key_type={self.key_type.value}, api_key={self.masked_key()})"


def credentials_present(environ: Mapping[str, str] | None = None) -> bool:
    """只检查环境变量是否齐全，不读取、不打印内容（spec §8 启动流程第 2 步）。"""
    env = os.environ if environ is None else environ
    if not env.get(ENV_API_KEY, "").strip():
        return False
    kt = _clean(env.get(ENV_KEY_TYPE, "")) or KeyType.HMAC.value
    if kt == KeyType.ED25519.value:
        return bool(env.get(ENV_PRIVATE_KEY_PATH, "").strip())
    return bool(env.get(ENV_API_SECRET, "").strip())


def load_credentials(
    environ: Mapping[str, str] | None = None, *, dotenv_path: str | Path | None = None
) -> ApiCredentials:
    """从环境变量构造凭据。dotenv_path 不为 None 时先加载该 .env（不覆盖已有环境变量）。"""
    if dotenv_path is not None:
        from dotenv import load_dotenv

        load_dotenv(dotenv_path, override=False)
    env = os.environ if environ is None else environ
    api_key = env.get(ENV_API_KEY, "").strip()
    if not api_key:
        raise CredentialsError(f"缺少环境变量 {ENV_API_KEY}")
    raw_type = _clean(env.get(ENV_KEY_TYPE, "")) or KeyType.HMAC.value
    try:
        key_type = KeyType(raw_type)
    except ValueError as e:
        raise CredentialsError(f"{ENV_KEY_TYPE} 只能是 hmac 或 ed25519，实际为 {raw_type!r}") from e

    signer: Signer
    if key_type is KeyType.HMAC:
        secret = env.get(ENV_API_SECRET, "").strip()
        if not secret:
            raise CredentialsError(f"{ENV_KEY_TYPE}=hmac 时必须设置 {ENV_API_SECRET}")
        signer = HmacSigner(secret)
    else:
        raw_path = env.get(ENV_PRIVATE_KEY_PATH, "").strip()
        if not raw_path:
            raise CredentialsError(f"{ENV_KEY_TYPE}=ed25519 时必须设置 {ENV_PRIVATE_KEY_PATH}")
        path = Path(raw_path)
        try:
            data = path.read_bytes()
        except OSError as e:
            raise CredentialsError(f"无法读取私钥文件 {path}：{type(e).__name__}") from e
        _warn_if_key_file_too_open(path)
        signer = Ed25519Signer.from_pem(data)
    creds = ApiCredentials(api_key=api_key, key_type=key_type, signer=signer)
    log.info("已加载 API 凭据：%r", creds)
    return creds


def _clean(value: str) -> str:
    # 容忍 .env 中 `hmac   # 注释` 这类写法
    return value.split("#", 1)[0].strip().lower()


def _warn_if_key_file_too_open(path: Path) -> None:
    """spec §13：只在 Linux 上检查私钥文件权限，宽于 600 时警告。"""
    if not sys.platform.startswith("linux"):
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return
    if mode & 0o077:
        log.warning("私钥文件 %s 的权限为 %o，宽于 600，建议执行 chmod 600", path, mode)
