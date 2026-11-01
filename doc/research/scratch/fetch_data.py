"""一次性探索脚本：下载币安现货 USDT 交易对日线（含已下架交易对）。

仅用于 strategy_review / strategy_research 的初步验证，不是正式回测的数据管道。
数据截止 2025-06-30（2025-07-01 之后为保留集，研究期间不得使用）。

用法（在项目根目录）：
    py -3.13 -I doc/research/scratch/fetch_data.py doc/research/scratch/data
"""
import io
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests

S3_LIST = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
VISION = "https://data.binance.vision"
EXINFO = "https://data-api.binance.vision/api/v3/exchangeInfo"
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
FIRST_MONTH = "2018-01"
LAST_MONTH = "2025-06"  # 保留集之前的最后一个月

COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"]


def s3_list(prefix, delimiter="/"):
    """列出 S3 前缀下的子目录或文件（自动翻页）。"""
    out, marker = [], ""
    while True:
        for attempt in range(4):
            try:
                r = requests.get(S3_LIST, params={"delimiter": delimiter, "prefix": prefix, "marker": marker},
                                 timeout=30)
                r.raise_for_status()
                break
            except requests.RequestException:
                if attempt == 3:
                    raise
                time.sleep(3 * (attempt + 1))
        root = ET.fromstring(r.content)
        items = [e.text for e in root.findall("s3:CommonPrefixes/s3:Prefix", NS)]
        items += [e.text for e in root.findall("s3:Contents/s3:Key", NS)]
        out += items
        if root.findtext("s3:IsTruncated", namespaces=NS) != "true":
            return out
        marker = root.findtext("s3:NextMarker", namespaces=NS) or items[-1]


def fetch_symbol(sym, out_dir):
    dest = out_dir / f"{sym}.csv"
    if dest.exists():
        return sym, "cached"
    keys = [k for k in s3_list(f"data/spot/monthly/klines/{sym}/1d/") if k.endswith(".zip")]
    frames = []
    for k in keys:
        month = k[-11:-4]  # YYYY-MM
        if not (FIRST_MONTH <= month <= LAST_MONTH):
            continue
        for attempt in range(3):
            try:
                r = requests.get(f"{VISION}/{k}", timeout=30)
                r.raise_for_status()
                break
            except requests.RequestException:
                time.sleep(2 * (attempt + 1))
        else:
            return sym, f"failed {k}"
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            raw = z.read(z.namelist()[0]).decode()
        first = raw.split("\n", 1)[0]
        df = pd.read_csv(io.StringIO(raw), header=0 if first.startswith("open") else None, names=COLS)
        frames.append(df)
    if not frames:
        return sym, "no data in range"
    df = pd.concat(frames, ignore_index=True)
    # 2025-01-01 起现货数据时间戳可能为微秒，统一转为毫秒
    df["open_time"] = df["open_time"].astype("int64")
    df.loc[df["open_time"] > 10**14, "open_time"] //= 1000
    df["date"] = pd.to_datetime(df["open_time"], unit="ms").dt.strftime("%Y-%m-%d")
    df = df.drop_duplicates("date").sort_values("date")
    df[["date", "open", "high", "low", "close", "volume", "quote_volume"]].to_csv(dest, index=False)
    return sym, f"{len(df)} rows"


def main():
    out_dir = Path(sys.argv[1])
    kl_dir = out_dir / "klines_1d"
    kl_dir.mkdir(parents=True, exist_ok=True)

    prefixes = s3_list("data/spot/monthly/klines/")
    bucket_syms = sorted(p.rstrip("/").split("/")[-1] for p in prefixes)
    usdt = [s for s in bucket_syms if s.endswith("USDT")]

    info = requests.get(EXINFO, timeout=30).json()
    status = {s["symbol"]: s["status"] for s in info["symbols"]}
    meta = pd.DataFrame({"symbol": usdt})
    meta["status_now"] = meta["symbol"].map(status).fillna("NOT_IN_EXCHANGEINFO")
    meta.to_csv(out_dir / "symbols_usdt.csv", index=False)
    print(f"bucket symbols={len(bucket_syms)} usdt={len(usdt)}")
    print(meta["status_now"].value_counts().to_string())

    with ThreadPoolExecutor(max_workers=12) as ex:
        for sym, msg in ex.map(lambda s: fetch_symbol(s, kl_dir), usdt):
            if not msg.endswith("rows") and msg != "cached":
                print(sym, msg)
    print("done")


if __name__ == "__main__":
    main()
