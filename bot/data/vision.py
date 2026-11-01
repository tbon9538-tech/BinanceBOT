"""data.binance.vision 历史日线：列目录、下载、校验、解析（spec §9.2）。

- 目录用 S3 列表接口分页（marker / NextMarker）。
- 每个 zip 都校验同名 .CHECKSUM（sha256），校验通过后才写入本地 data/raw/。
- 已下载且校验通过的文件不再重复下载（断点续传）。
"""

from __future__ import annotations

import hashlib
import io
import logging
import time
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pandas as pd

from bot.data.klines import parse_vision_csv

log = logging.getLogger(__name__)

S3_LIST_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
VISION_URL = "https://data.binance.vision"
ALLOWED_VISION_HOSTS = frozenset({"s3-ap-northeast-1.amazonaws.com", "data.binance.vision"})
_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
RETRY_DELAYS_SEC = (2.0, 5.0, 10.0)

MONTHLY_KLINES = "data/spot/monthly/klines/"
DAILY_KLINES = "data/spot/daily/klines/"


class ChecksumMismatch(Exception):
    pass


@dataclass(frozen=True, slots=True)
class VisionFile:
    key: str  # 例如 data/spot/monthly/klines/BTCUSDT/1d/BTCUSDT-1d-2024-12.zip

    @property
    def period(self) -> str:
        """月文件为 YYYY-MM，日文件为 YYYY-MM-DD。"""
        stem = self.key.rsplit("/", 1)[-1].removesuffix(".zip")
        parts = stem.split("-")
        return "-".join(parts[2:])

    @property
    def is_monthly(self) -> bool:
        return self.key.startswith(MONTHLY_KLINES)


class VisionClient:
    def __init__(
        self,
        raw_dir: Path,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_sec: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        for url in (S3_LIST_URL, VISION_URL):
            if urlparse(url).hostname not in ALLOWED_VISION_HOSTS:  # pragma: no cover - 常量自检
                raise ValueError(url)
        self.raw_dir = raw_dir
        self._http = httpx.Client(timeout=timeout_sec, transport=transport, follow_redirects=False)
        self._sleep = sleep

    def close(self) -> None:
        self._http.close()

    def _get(self, url: str, params: dict[str, str] | None = None) -> httpx.Response:
        for attempt in range(len(RETRY_DELAYS_SEC) + 1):
            try:
                resp = self._http.get(url, params=params)
                if resp.status_code < 500:
                    return resp
                reason = f"HTTP {resp.status_code}"
            except httpx.TransportError as e:
                reason = str(e)
            if attempt == len(RETRY_DELAYS_SEC):
                break
            log.warning("请求 %s 失败（%s），%.0f 秒后重试", url, reason, RETRY_DELAYS_SEC[attempt])
            self._sleep(RETRY_DELAYS_SEC[attempt])
        raise httpx.HTTPError(f"请求 {url} 多次失败：{reason}")

    def list_keys(self, prefix: str, delimiter: str | None = "/") -> list[str]:
        """列出前缀下的子目录（CommonPrefixes）和文件（Contents/Key），自动翻页。"""
        out: list[str] = []
        marker = ""
        while True:
            params = {"prefix": prefix, "marker": marker}
            if delimiter:
                params["delimiter"] = delimiter
            resp = self._get(S3_LIST_URL, params)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            items = [e.text or "" for e in root.findall("s3:CommonPrefixes/s3:Prefix", _NS)]
            items += [e.text or "" for e in root.findall("s3:Contents/s3:Key", _NS)]
            out += items
            if root.findtext("s3:IsTruncated", namespaces=_NS) != "true" or not items:
                return out
            marker = root.findtext("s3:NextMarker", namespaces=_NS) or items[-1]

    def list_usdt_symbols(self) -> list[str]:
        prefixes = self.list_keys(MONTHLY_KLINES)
        syms = {p.rstrip("/").rsplit("/", 1)[-1] for p in prefixes}
        return sorted(s for s in syms if s.endswith("USDT"))

    def list_monthly_files(self, symbol: str) -> list[VisionFile]:
        keys = self.list_keys(f"{MONTHLY_KLINES}{symbol}/1d/")
        return [VisionFile(k) for k in keys if k.endswith(".zip")]

    def list_daily_files(self, symbol: str, month: str) -> list[VisionFile]:
        """只列出某个月（YYYY-MM）的日文件，避免列出全部历史。"""
        keys = self.list_keys(f"{DAILY_KLINES}{symbol}/1d/{symbol}-1d-{month}")
        return [VisionFile(k) for k in keys if k.endswith(".zip")]

    def local_path(self, f: VisionFile) -> Path:
        return self.raw_dir / f.key

    def fetch(self, f: VisionFile) -> tuple[pd.DataFrame, str]:
        """下载（或复用本地已校验的文件）并解析，返回 (raw frame, sha256)。"""
        resp = self._get(f"{VISION_URL}/{f.key}.CHECKSUM")
        resp.raise_for_status()
        expected = resp.text.strip().split()[0].lower()
        path = self.local_path(f)
        content: bytes | None = None
        if path.exists():
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() == expected:
                content = data
        if content is None:
            resp = self._get(f"{VISION_URL}/{f.key}")
            resp.raise_for_status()
            data = resp.content
            actual = hashlib.sha256(data).hexdigest()
            if actual != expected:
                raise ChecksumMismatch(f"{f.key}: 期望 {expected}，实际 {actual}")
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".zip.part")
            tmp.write_bytes(data)
            tmp.replace(path)
            content = data
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            names = [n for n in z.namelist() if n.endswith(".csv")]
            if len(names) != 1:
                raise ValueError(f"{f.key}: zip 中应只有一个 CSV，实际 {names}")
            df = parse_vision_csv(z.read(names[0]))
        return df, expected
