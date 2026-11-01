"""订单计划（spec §6.3）。纯函数：不做 I/O，不读时钟。

流程（由编排层调用）：
1. plan_sells：对 `受管持仓 ∪ W>0` 的每个交易对，用 should_skip 判断是否调仓；
   需要减仓的生成卖单，需要加仓的作为买单候选（BuyCandidate）返回；
2. 执行全部卖单 → 重新查询余额；
3. plan_buys：可用资金 A = USDT_free × (1 − fee_buffer)，买单总额超过 A 时按比例缩小，
   缩小后的金额再交给 should_skip 判断金额下限。

所有"是否调仓"的判断都通过 bot.strategy.rebalance.should_skip（与研究引擎共用，禁止另写一份）；
计划层自己的跳过原因（不可交易、没有报价、被阻断、过滤器不通过）见 PlanSkip。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from bot.config import ExecutionConfig, RebalanceConfig
from bot.domain.filters import FilterViolation, check_order, has_percent_price, limit_price, round_qty
from bot.domain.types import Balance, Side, SymbolRules
from bot.execution.client_order_id import OrderKind
from bot.strategy.rebalance import SkipReason, should_skip

_ZERO = Decimal(0)
_ONE = Decimal(1)
_TWO = Decimal(2)


class PlanSkip(StrEnum):
    """计划层的跳过原因（should_skip 之外的）。"""

    NOT_TRADABLE = "not_tradable"  # 执行环境中不存在或状态不是 TRADING
    BLOCKED = "blocked"  # 本次运行已阻断（例如下单状态未知，spec §6.6）
    NO_QUOTE = "no_quote"  # 没有有效的买一 / 卖一
    NO_REFERENCE_PRICE = "no_reference_price"  # 有 PERCENT_PRICE_BY_SIDE 但缺少 avgPrice
    FILTER_REJECTED = "filter_rejected"  # 取整后过滤器校验不通过（spec §6.3：放弃该笔，记录 WARN）
    ZERO_CAPITAL = "zero_capital"  # 策略可用资金 C ≤ 0


@dataclass(frozen=True, slots=True)
class PlannedOrder:
    """一笔待执行的 LIMIT IOC 订单。"""

    symbol: str
    side: Side
    kind: OrderKind
    quantity: Decimal
    price: Decimal
    slippage: Decimal  # 重试时按同样的滑点重新定价
    target_weight: float
    current_weight: float
    delta_usdt: Decimal  # 目标市值 − 当前市值（卖单为负）
    reference_price: Decimal | None  # avgPrice
    price_clamped: bool = False  # 被 PERCENT_PRICE_BY_SIDE 收窄过
    may_not_fill: bool = False  # 收窄后买价 < ask 或卖价 > bid：照常下单，可能无法成交

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity


@dataclass(frozen=True, slots=True)
class SkippedSymbol:
    symbol: str
    reason: SkipReason | PlanSkip
    detail: str = ""
    target_weight: float = 0.0
    current_weight: float = 0.0
    delta_usdt: Decimal = _ZERO


@dataclass(frozen=True, slots=True)
class BuyCandidate:
    """通过了 should_skip、需要加仓的交易对。买单金额在卖单完成后由 plan_buys 计算。"""

    symbol: str
    target_weight: float
    current_weight: float
    delta_usdt: Decimal  # > 0：计划买入的金额
    position_usdt: Decimal


@dataclass(frozen=True, slots=True)
class SellPlan:
    sells: tuple[PlannedOrder, ...]
    buy_candidates: tuple[BuyCandidate, ...]
    skipped: tuple[SkippedSymbol, ...]


@dataclass(frozen=True, slots=True)
class BuyPlan:
    buys: tuple[PlannedOrder, ...]
    skipped: tuple[SkippedSymbol, ...]
    available_usdt: Decimal  # A = USDT_free × (1 − fee_buffer)
    requested_usdt: Decimal  # 可执行候选的买入金额合计（缩小前）
    scale: Decimal  # 实际使用的缩小比例（≤ 1）


@dataclass(frozen=True, slots=True)
class PricedOrder:
    price: Decimal
    clamped: bool
    may_not_fill: bool


def price_limit_order(
    side: Side,
    bid: Decimal,
    ask: Decimal,
    slippage: Decimal,
    rules: SymbolRules,
    ref_price: Decimal | None,
) -> PricedOrder:
    """卖价 = bid × (1 − slippage) 向上取整；买价 = ask × (1 + slippage) 向下取整；
    再按 PERCENT_PRICE_BY_SIDE（参考价 avgPrice）收窄并重新取整（spec §6.3）。"""
    raw = bid * (_ONE - slippage) if side is Side.SELL else ask * (_ONE + slippage)
    price, clamped = limit_price(raw, side, rules, ref_price)
    may_not_fill = clamped and ((side is Side.BUY and price < ask) or (side is Side.SELL and price > bid))
    return PricedOrder(price, clamped, may_not_fill)


def _violations_text(v: Sequence[FilterViolation]) -> str:
    return "; ".join(str(x) for x in v)


def _quote_ok(book: tuple[Decimal, Decimal] | None) -> bool:
    return book is not None and book[0] > 0 and book[1] > 0 and book[1] >= book[0]


def _reference(
    rules: SymbolRules, avg_prices: Mapping[str, Decimal], symbol: str
) -> tuple[Decimal | None, bool]:
    """返回 (参考价, 是否缺失且必需)。"""
    ref = avg_prices.get(symbol)
    if ref is not None and ref > 0:
        return ref, False
    return None, has_percent_price(rules)


def plan_sells(
    targets: Mapping[str, float],
    capital: Decimal,
    balances: Mapping[str, Balance],
    rules: Mapping[str, SymbolRules],
    books: Mapping[str, tuple[Decimal, Decimal]],
    avg_prices: Mapping[str, Decimal],
    rebalance_cfg: RebalanceConfig,
    exec_cfg: ExecutionConfig,
    *,
    managed_assets: Collection[str],
    ignore_band: bool = False,
    blocked_symbols: Collection[str] = frozenset(),
    quote_asset: str = "USDT",
) -> SellPlan:
    """第一阶段：卖单与买单候选（spec §6.3）。

    - targets：symbol → 目标权重 W（占策略可用资金 C 的比例）；
    - balances：asset → 余额；只有 managed_assets 中的资产计入当前持仓，非受管资产永远不卖（spec §7）；
    - ignore_band：`--force-rebalance` 时忽略容忍带（最小下单额仍然生效）。
    """
    return _plan_sells(
        targets,
        capital,
        balances,
        rules,
        books,
        avg_prices,
        rebalance_cfg,
        managed_assets=managed_assets,
        ignore_band=ignore_band,
        blocked_symbols=blocked_symbols,
        quote_asset=quote_asset,
        slippage=exec_cfg.max_slippage,
        kind=OrderKind.SELL,
    )


def plan_liquidation(
    balances: Mapping[str, Balance],
    rules: Mapping[str, SymbolRules],
    books: Mapping[str, tuple[Decimal, Decimal]],
    avg_prices: Mapping[str, Decimal],
    rebalance_cfg: RebalanceConfig,
    exec_cfg: ExecutionConfig,
    *,
    managed_assets: Collection[str],
    blocked_symbols: Collection[str] = frozenset(),
    quote_asset: str = "USDT",
) -> SellPlan:
    """kill switch / 回撤熔断的清仓计划（spec §5.5）：所有受管资产目标为 0，滑点上限 kill_slippage，
    clientOrderId 类型 K。低于最小下单额的持仓记为灰尘（同样经过 should_skip）。"""
    values = []
    for asset in managed_assets:
        sym = f"{asset}{quote_asset}"
        bal, book = balances.get(asset), books.get(sym)
        if bal is not None and _quote_ok(book) and book is not None:
            values.append(bal.total * (book[0] + book[1]) / _TWO)
    capital = sum(values, _ZERO)
    return _plan_sells(
        {},
        capital if capital > 0 else _ONE,
        balances,
        rules,
        books,
        avg_prices,
        rebalance_cfg,
        managed_assets=managed_assets,
        ignore_band=True,
        blocked_symbols=blocked_symbols,
        quote_asset=quote_asset,
        slippage=exec_cfg.kill_slippage,
        kind=OrderKind.KILL,
    )


def _plan_sells(
    targets: Mapping[str, float],
    capital: Decimal,
    balances: Mapping[str, Balance],
    rules: Mapping[str, SymbolRules],
    books: Mapping[str, tuple[Decimal, Decimal]],
    avg_prices: Mapping[str, Decimal],
    rebalance_cfg: RebalanceConfig,
    *,
    managed_assets: Collection[str],
    ignore_band: bool,
    blocked_symbols: Collection[str],
    quote_asset: str,
    slippage: Decimal,
    kind: OrderKind,
) -> SellPlan:
    if capital < 0:
        raise ValueError(f"策略可用资金不能为负：{capital}")
    if any(w < 0 for w in targets.values()):
        raise ValueError("现货目标权重不能为负")
    managed = set(managed_assets)
    universe = {s for s, w in targets.items() if w > 0}
    for asset in managed:
        bal = balances.get(asset)
        if asset != quote_asset and bal is not None and bal.total > 0:
            universe.add(f"{asset}{quote_asset}")

    sells: list[PlannedOrder] = []
    candidates: list[BuyCandidate] = []
    skipped: list[SkippedSymbol] = []
    for symbol in sorted(universe):
        w = float(targets.get(symbol, 0.0))
        r = rules.get(symbol)
        if r is None or not r.is_trading:
            status = "不存在" if r is None else r.status.value
            skipped.append(SkippedSymbol(symbol, PlanSkip.NOT_TRADABLE, f"执行环境状态：{status}", w))
            continue
        if symbol in blocked_symbols:
            skipped.append(SkippedSymbol(symbol, PlanSkip.BLOCKED, "本次运行已阻断", w))
            continue
        book = books.get(symbol)
        if not _quote_ok(book) or book is None:
            skipped.append(SkippedSymbol(symbol, PlanSkip.NO_QUOTE, f"bookTicker={book}", w))
            continue
        if capital == 0:
            skipped.append(SkippedSymbol(symbol, PlanSkip.ZERO_CAPITAL, "C = 0", w))
            continue
        bid, ask = book
        mid = (bid + ask) / _TWO
        bal = balances.get(r.base_asset) if r.base_asset in managed else None
        qty_total = bal.total if bal is not None else _ZERO
        free = bal.free if bal is not None else _ZERO
        cur_val = qty_total * mid
        tgt_val = Decimal(str(w)) * capital
        cw = float(cur_val / capital)
        delta = tgt_val - cur_val
        reason = should_skip(
            w, cw, float(delta), float(cur_val), float(r.min_notional), rebalance_cfg, ignore_band=ignore_band
        )
        if reason is not None:
            skipped.append(SkippedSymbol(symbol, reason, "", w, cw, delta))
            continue
        if delta > 0:
            candidates.append(BuyCandidate(symbol, w, cw, delta, cur_val))
            continue
        ref, missing_ref = _reference(r, avg_prices, symbol)
        if missing_ref:
            skipped.append(SkippedSymbol(symbol, PlanSkip.NO_REFERENCE_PRICE, "", w, cw, delta))
            continue
        raw_qty = free if tgt_val == 0 else min(-delta / bid, free)
        qty = round_qty(raw_qty, r)
        priced = price_limit_order(Side.SELL, bid, ask, slippage, r, ref)
        violations = check_order(r, Side.SELL, priced.price, qty, ref)
        if violations:
            skipped.append(
                SkippedSymbol(symbol, PlanSkip.FILTER_REJECTED, _violations_text(violations), w, cw, delta)
            )
            continue
        sells.append(
            PlannedOrder(
                symbol=symbol,
                side=Side.SELL,
                kind=kind,
                quantity=qty,
                price=priced.price,
                slippage=slippage,
                target_weight=w,
                current_weight=cw,
                delta_usdt=delta,
                reference_price=ref,
                price_clamped=priced.clamped,
                may_not_fill=priced.may_not_fill,
            )
        )
    return SellPlan(tuple(sells), tuple(candidates), tuple(skipped))


def plan_buys(
    candidates: Sequence[BuyCandidate],
    usdt_free: Decimal,
    rules: Mapping[str, SymbolRules],
    books: Mapping[str, tuple[Decimal, Decimal]],
    avg_prices: Mapping[str, Decimal],
    rebalance_cfg: RebalanceConfig,
    exec_cfg: ExecutionConfig,
    *,
    blocked_symbols: Collection[str] = frozenset(),
) -> BuyPlan:
    """第二阶段：在卖单完成、余额刷新之后计算买单（spec §6.3）。

    books 应是卖单之后重新获取的 bookTicker。被阻断、不可交易、没有报价的候选先剔除，
    剩下的买入金额合计超过 A 时按同一比例缩小；缩小后的金额再经 should_skip 判断金额下限。
    """
    available = max(usdt_free, _ZERO) * (_ONE - exec_cfg.fee_buffer)
    skipped: list[SkippedSymbol] = []
    eligible: list[tuple[BuyCandidate, SymbolRules, tuple[Decimal, Decimal], Decimal | None]] = []
    for c in candidates:
        r = rules.get(c.symbol)
        if r is None or not r.is_trading:
            skipped.append(
                SkippedSymbol(c.symbol, PlanSkip.NOT_TRADABLE, "", c.target_weight, c.current_weight)
            )
            continue
        if c.symbol in blocked_symbols:
            skipped.append(
                SkippedSymbol(c.symbol, PlanSkip.BLOCKED, "本次运行已阻断", c.target_weight, c.current_weight)
            )
            continue
        book = books.get(c.symbol)
        if not _quote_ok(book) or book is None:
            skipped.append(
                SkippedSymbol(
                    c.symbol, PlanSkip.NO_QUOTE, f"bookTicker={book}", c.target_weight, c.current_weight
                )
            )
            continue
        ref, missing_ref = _reference(r, avg_prices, c.symbol)
        if missing_ref:
            skipped.append(
                SkippedSymbol(c.symbol, PlanSkip.NO_REFERENCE_PRICE, "", c.target_weight, c.current_weight)
            )
            continue
        eligible.append((c, r, book, ref))

    requested = sum((c.delta_usdt for c, _, _, _ in eligible), _ZERO)
    scale = _ONE if requested <= available or requested == 0 else available / requested
    buys: list[PlannedOrder] = []
    for c, r, (bid, ask), ref in eligible:
        amount = c.delta_usdt * scale
        reason = should_skip(
            c.target_weight,
            c.current_weight,
            float(amount),
            float(c.position_usdt),
            float(r.min_notional),
            rebalance_cfg,
            ignore_band=True,  # 容忍带已在 plan_sells 中判断过；这里只检查缩小后的金额下限
        )
        if reason is not None:
            skipped.append(
                SkippedSymbol(
                    c.symbol, reason, f"缩小比例 {scale:.4f}", c.target_weight, c.current_weight, amount
                )
            )
            continue
        priced = price_limit_order(Side.BUY, bid, ask, exec_cfg.max_slippage, r, ref)
        qty = round_qty(amount / priced.price, r) if priced.price > 0 else _ZERO
        violations = check_order(r, Side.BUY, priced.price, qty, ref)
        if violations:
            skipped.append(
                SkippedSymbol(
                    c.symbol,
                    PlanSkip.FILTER_REJECTED,
                    _violations_text(violations),
                    c.target_weight,
                    c.current_weight,
                    amount,
                )
            )
            continue
        buys.append(
            PlannedOrder(
                symbol=c.symbol,
                side=Side.BUY,
                kind=OrderKind.BUY,
                quantity=qty,
                price=priced.price,
                slippage=exec_cfg.max_slippage,
                target_weight=c.target_weight,
                current_weight=c.current_weight,
                delta_usdt=amount,
                reference_price=ref,
                price_clamped=priced.clamped,
                may_not_fill=priced.may_not_fill,
            )
        )
    return BuyPlan(tuple(buys), tuple(skipped), available, requested, min(scale, _ONE))


def count_skips(skipped: Sequence[SkippedSymbol]) -> dict[str, int]:
    """按原因计数（写入日报，spec §6.3）。"""
    out: dict[str, int] = {}
    for s in skipped:
        out[s.reason.value] = out.get(s.reason.value, 0) + 1
    return out
