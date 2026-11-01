"""项目统一的异常类型。"""

from __future__ import annotations


class BotError(Exception):
    """本项目所有异常的基类。"""


class ConfigError(BotError):
    """配置文件缺失、格式错误或校验失败。"""


class ExchangeError(BotError):
    """交易所返回的业务错误（例如 -1013 过滤器校验失败、-2010 拒单）。"""

    def __init__(self, code: int, message: str, http_status: int | None = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.http_status = http_status


class RateLimitedError(BotError):
    """收到 429/418。整个进程必须停止发送请求（spec §3.5）。"""

    def __init__(self, http_status: int, retry_after_sec: float | None) -> None:
        super().__init__(f"HTTP {http_status}，Retry-After={retry_after_sec}")
        self.http_status = http_status
        self.retry_after_sec = retry_after_sec


class ExchangeUnavailableError(BotError):
    """网络错误或交易所 5xx，重试后仍失败（只用于查询类请求，或下单请求尚未发出之前）。"""


class UnknownOrderStatus(BotError):
    """下单超时、网络中断、5xx 或 -1007：订单执行状态未知（spec §6.6）。

    遇到这个异常时绝不能换一个新 clientOrderId 重下同一个意图。
    """

    def __init__(self, symbol: str, client_order_id: str, reason: str) -> None:
        super().__init__(f"{symbol} {client_order_id}: {reason}")
        self.symbol = symbol
        self.client_order_id = client_order_id
        self.reason = reason
