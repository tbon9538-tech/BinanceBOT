"""newClientOrderId（spec §6.4）。

格式：`{strategy_id}-{运行日 yymmdd}-{sha1(symbol)[:8]}-{S|B|K}{seq:03d}-{attempt}`，
≤ 36 个字符，字符集 [A-Za-z0-9_-]，与时钟无关。

- seq 是当日意图序号，从 state.db 的 id_sequences 表按 (strategy_id, 运行日) 分配，重启后继续递增；
- attempt 是同一意图的第几次尝试（从 1 开始）；每次尝试的 ID 都全局唯一。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from bot.domain.types import CLIENT_ORDER_ID_RE

STRATEGY_ID_RE = re.compile(r"^[a-z0-9]{1,6}$")
MAX_LEN = 36
_CID_RE = re.compile(
    r"^(?P<strategy>[a-z0-9]{1,6})-(?P<yymmdd>\d{6})-(?P<hash>[0-9a-f]{8})-"
    r"(?P<kind>[SBK])(?P<seq>\d{3,})-(?P<attempt>[1-9]\d*)$"
)


class OrderKind(StrEnum):
    SELL = "S"
    BUY = "B"
    KILL = "K"  # kill switch 的清仓卖单（spec §5.5）


@dataclass(frozen=True, slots=True)
class ParsedClientOrderId:
    strategy_id: str
    yymmdd: str
    symbol_hash: str
    kind: OrderKind
    seq: int
    attempt: int


def symbol_hash8(symbol: str) -> str:
    """sha1(symbol 的 UTF-8 编码) 的前 8 位十六进制。symbol 可能含非 ASCII 字符，不能直接放进 ID。"""
    return hashlib.sha1(symbol.encode("utf-8")).hexdigest()[:8]


def make_client_order_id(
    strategy_id: str, run_date: date, symbol: str, kind: OrderKind, seq: int, attempt: int
) -> str:
    if not STRATEGY_ID_RE.match(strategy_id):
        raise ValueError(f"strategy_id 必须是 1~6 位小写字母或数字：{strategy_id!r}")
    if seq < 1:
        raise ValueError(f"seq 必须 ≥ 1：{seq}")
    if attempt < 1:
        raise ValueError(f"attempt 必须 ≥ 1：{attempt}")
    cid = f"{strategy_id}-{run_date:%y%m%d}-{symbol_hash8(symbol)}-{OrderKind(kind).value}{seq:03d}-{attempt}"
    if len(cid) > MAX_LEN or not CLIENT_ORDER_ID_RE.match(cid):
        raise ValueError(f"clientOrderId 超出 {MAX_LEN} 个字符或含非法字符：{cid!r}")
    return cid


def parse_client_order_id(cid: str) -> ParsedClientOrderId | None:
    """解析本系统生成的 ID；格式不符时返回 None。"""
    m = _CID_RE.match(cid)
    if m is None:
        return None
    return ParsedClientOrderId(
        strategy_id=m["strategy"],
        yymmdd=m["yymmdd"],
        symbol_hash=m["hash"],
        kind=OrderKind(m["kind"]),
        seq=int(m["seq"]),
        attempt=int(m["attempt"]),
    )


def strategy_prefix(strategy_id: str) -> str:
    return f"{strategy_id}-"


def is_strategy_order(cid: str, strategy_id: str) -> bool:
    """按前缀识别本策略的订单（spec §5.5、§6.7），包括人工或旧版本按同样前缀下的单。"""
    return cid.startswith(strategy_prefix(strategy_id))
