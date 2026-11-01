"""调仓跳过判断（spec §6.3）。研究引擎与订单计划必须调用同一个函数，禁止各写一份。"""

from __future__ import annotations

from enum import StrEnum

from bot.config import RebalanceConfig

MIN_NOTIONAL_MARGIN = 1.1  # 调整金额至少为最小下单额的 1.1 倍


class SkipReason(StrEnum):
    TOLERANCE_BAND = "tolerance_band"  # 条件 a：权重差落在容忍带内
    BELOW_MIN_TRADE = "below_min_trade"  # 条件 b：调整金额太小
    DUST = "dust"  # 条件 c：目标为 0，但持仓低于最小下单额，无法卖出


def should_skip(
    target_w: float,
    current_w: float,
    delta_usdt: float,
    position_usdt: float,
    min_notional_usdt: float,
    cfg: RebalanceConfig,
    *,
    ignore_band: bool = False,
) -> SkipReason | None:
    """返回 None 表示应当交易；否则返回跳过原因。

    - target_w / current_w：占策略可用资金的权重；
    - delta_usdt：目标市值 − 当前市值；position_usdt：当前持仓市值；
    - ignore_band：`--force-rebalance` 时忽略容忍带（最小下单额仍然生效）。
    """
    if target_w <= 0:
        if position_usdt <= 0:
            return SkipReason.BELOW_MIN_TRADE
        if position_usdt < min_notional_usdt:
            return SkipReason.DUST
        return None  # 目标为 0：只要能卖就全部卖出
    if not ignore_band:
        diff = abs(target_w - current_w)
        if cfg.threshold_mode == "absolute":
            in_band = diff < cfg.threshold
        else:
            in_band = diff < cfg.threshold_rel * max(target_w, current_w)
        if in_band:
            return SkipReason.TOLERANCE_BAND
    if abs(delta_usdt) < max(float(cfg.min_trade_usdt), min_notional_usdt * MIN_NOTIONAL_MARGIN):
        return SkipReason.BELOW_MIN_TRADE
    return None
