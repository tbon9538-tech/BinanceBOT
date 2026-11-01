from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import date
from pathlib import Path

import httpx
import pytest

from bot.data.klines import parse_vision_csv
from bot.data.vision import ChecksumMismatch, VisionClient, VisionFile
from tests.helpers import csv_bytes, kline_row

D1, D2 = date(2024, 12, 31), date(2025, 1, 1)


def test_parse_mixed_ms_and_microsecond_timestamps() -> None:
    # 2024-12 文件为毫秒，2025-01 起为微秒（spec §9.2）
    df = parse_vision_csv(csv_bytes([kline_row(D1, 1.0), kline_row(D2, 2.0, micro=True)]))
    assert list(df["date"]) == ["2024-12-31", "2025-01-01"]
    assert df["open_time_ms"].iloc[1] == df["open_time_ms"].iloc[0] + 86_400_000
    assert df["close_time_ms"].iloc[1] < 10**14


def test_parse_skips_header_and_dedupes() -> None:
    rows = [kline_row(D1, 1.0), kline_row(D1, 1.5)]
    df = parse_vision_csv(csv_bytes(rows, header=True))
    assert len(df) == 1
    assert df["close"].iloc[0] == "1.50000000"


def test_parse_rejects_non_midnight_open() -> None:
    r = kline_row(D1, 1.0)
    r[0] = int(r[0]) + 3_600_000
    with pytest.raises(ValueError, match="UTC 00:00"):
        parse_vision_csv(csv_bytes([r]))


def _zip(content: bytes, name: str = "X.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, content)
    return buf.getvalue()


def _list_xml(prefixes: list[str], keys: list[str], truncated: bool, next_marker: str | None) -> bytes:
    ns = "http://s3.amazonaws.com/doc/2006-03-01/"
    parts = [f'<ListBucketResult xmlns="{ns}"><IsTruncated>{"true" if truncated else "false"}</IsTruncated>']
    if next_marker:
        parts.append(f"<NextMarker>{next_marker}</NextMarker>")
    parts += [f"<CommonPrefixes><Prefix>{p}</Prefix></CommonPrefixes>" for p in prefixes]
    parts += [f"<Contents><Key>{k}</Key></Contents>" for k in keys]
    parts.append("</ListBucketResult>")
    return "".join(parts).encode()


def test_s3_listing_paginates_with_next_marker_and_last_key() -> None:
    base = "data/spot/monthly/klines/"
    pages = {
        "": _list_xml([base + "AUSDT/", base + "ABNB/"], [], True, base + "ABNB/"),
        base + "ABNB/": _list_xml([base + "BUSDT/"], [], True, None),  # 无 NextMarker：用最后一项
        base + "BUSDT/": _list_xml([base + "CUSDT/"], [], False, None),
    }
    markers = []

    def handler(req: httpx.Request) -> httpx.Response:
        m = req.url.params.get("marker", "")
        markers.append(m)
        return httpx.Response(200, content=pages[m])

    vc = VisionClient(Path("unused"), transport=httpx.MockTransport(handler), sleep=lambda s: None)
    assert vc.list_usdt_symbols() == ["AUSDT", "BUSDT", "CUSDT"]
    assert markers == ["", base + "ABNB/", base + "BUSDT/"]


def test_daily_listing_uses_month_prefix() -> None:
    prefixes = []

    def handler(req: httpx.Request) -> httpx.Response:
        prefixes.append(req.url.params["prefix"])
        return httpx.Response(200, content=_list_xml([], [], False, None))

    vc = VisionClient(Path("unused"), transport=httpx.MockTransport(handler), sleep=lambda s: None)
    vc.list_daily_files("BTCUSDT", "2026-10")
    assert prefixes == ["data/spot/daily/klines/BTCUSDT/1d/BTCUSDT-1d-2026-10"]


def test_vision_file_period() -> None:
    assert VisionFile("data/spot/monthly/klines/BTCUSDT/1d/BTCUSDT-1d-2024-12.zip").period == "2024-12"
    assert VisionFile("data/spot/daily/klines/BTCUSDT/1d/BTCUSDT-1d-2026-10-30.zip").period == "2026-10-30"
    assert (
        VisionFile("data/spot/monthly/klines/1000SATSUSDT/1d/1000SATSUSDT-1d-2024-12.zip").period == "2024-12"
    )


def _fetch_handler(zip_bytes: bytes, checksum: str, counter: list[str]):  # type: ignore[no-untyped-def]
    def handler(req: httpx.Request) -> httpx.Response:
        counter.append(req.url.path)
        if req.url.path.endswith(".CHECKSUM"):
            return httpx.Response(200, text=f"{checksum}  file.zip\n")
        return httpx.Response(200, content=zip_bytes)

    return handler


def test_fetch_verifies_checksum_and_resumes(tmp_path: Path) -> None:
    z = _zip(csv_bytes([kline_row(D1, 1.0)]))
    sha = hashlib.sha256(z).hexdigest()
    f = VisionFile("data/spot/monthly/klines/XUSDT/1d/XUSDT-1d-2024-12.zip")
    calls: list[str] = []
    vc = VisionClient(
        tmp_path, transport=httpx.MockTransport(_fetch_handler(z, sha, calls)), sleep=lambda s: None
    )
    df, got = vc.fetch(f)
    assert got == sha and len(df) == 1
    assert (tmp_path / f.key).read_bytes() == z
    calls.clear()
    vc.fetch(f)  # 本地文件校验通过：只请求 CHECKSUM，不重新下载
    assert calls == ["/" + f.key + ".CHECKSUM"]


def test_fetch_rejects_checksum_mismatch(tmp_path: Path) -> None:
    z = _zip(csv_bytes([kline_row(D1, 1.0)]))
    f = VisionFile("data/spot/monthly/klines/XUSDT/1d/XUSDT-1d-2024-12.zip")
    vc = VisionClient(
        tmp_path, transport=httpx.MockTransport(_fetch_handler(z, "0" * 64, [])), sleep=lambda s: None
    )
    with pytest.raises(ChecksumMismatch):
        vc.fetch(f)
    assert not (tmp_path / f.key).exists()
