"""现货执行器：下单、未知状态处理、撤单、对账（spec §6.5～§6.8）。

关键不变式（dev_plan §5 第 1 条）：
- write-ahead：每次尝试先写意图（PLANNED），发送前改为 SENT，再发 POST；
- 下单状态未知时**绝不**换新 clientOrderId 重下同一意图：按 unknown_status_query_delays_sec 查单，
  全部 -2013 → NOT_FOUND，本次运行不再对该 symbol 下单；下一次运行再查一次并做余额核对；
- 只有在上一次尝试的结果明确（FILLED/PARTIAL/EXPIRED）时，才会用 attempt+1 追剩余数量；
- 持仓数量一律以交易所余额为准（§6.8 对账时用交易所数值覆盖本地记录）。

编排层的调用顺序（spec §6.2）：
    reconcile_pending_intents → cancel_strategy_open_orders → reconcile_positions（第 4 步）
    → execute(卖单) → 刷新余额 → plan_buys → execute(买单) → reconcile_positions（第 10 步）
清仓（kill）请另建一个 should_abort 恒为 False 的执行器，并沿用本次运行的 blocked_symbols。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from bot.config import ExecutionConfig
from bot.domain.errors import ExchangeError, ExchangeUnavailableError, RateLimitedError, UnknownOrderStatus
from bot.domain.filters import check_order, is_dust, round_qty
from bot.domain.types import (
    Balance,
    Fill,
    IntentStatus,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    Side,
    SymbolRules,
    SymbolStatus,
    TimeInForce,
)
from bot.exchange.base import SpotAccount
from bot.execution.client_order_id import OrderKind, is_strategy_order, make_client_order_id
from bot.execution.planner import PlannedOrder, price_limit_order
from bot.persistence.models import OrderIntentRow
from bot.persistence.repo import NewIntent, StateRepo, utc_now

log = logging.getLogger(__name__)

_ZERO = Decimal(0)
KILL_MAX_ATTEMPTS = 3  # spec §5.5：清仓卖单最多尝试 3 次
RECONCILE_ALERT_USDT = Decimal(1)  # spec §6.8：差额市值 > 1 USDT 时告警

PENDING_STATUSES = (IntentStatus.PLANNED, IntentStatus.SENT, IntentStatus.UNKNOWN, IntentStatus.NOT_FOUND)
_STOP_STATUSES = frozenset({IntentStatus.REJECTED, IntentStatus.UNKNOWN, IntentStatus.NOT_FOUND})

# 事件类型（写入 events 表；WARN 及以上由通知模块转发）
EV_ORDER_STATUS_UNKNOWN = "ORDER_STATUS_UNKNOWN"
EV_ORDER_INCOMPLETE = "ORDER_INCOMPLETE"
EV_ORDER_REJECTED = "ORDER_REJECTED"
EV_INTENT_RESOLVED = "INTENT_RESOLVED"
EV_INTENT_CLOSED = "INTENT_CLOSED"
EV_INTENT_UNRESOLVED = "INTENT_UNRESOLVED"
EV_INFERRED_FILL = "INFERRED_FILL"
EV_OPEN_ORDER_CANCELED = "OPEN_ORDER_CANCELED"
EV_RECONCILE_DIFF = "RECONCILE_DIFF"
EV_ASSET_MANAGED = "ASSET_MANAGED"

INFO, WARN = "INFO", "WARN"


class MonotonicClock:
    """严格递增的 UTC 时钟：本地记录（意图创建、成交写入、持仓更新）的先后顺序不受时钟分辨率影响。"""

    def __init__(self, clock: Callable[[], datetime] = utc_now) -> None:
        self._clock = clock
        self._last: datetime | None = None

    def __call__(self) -> datetime:
        t = self._clock()
        if self._last is not None and t <= self._last:
            t = self._last + timedelta(microseconds=1)
        self._last = t
        return t


@dataclass(frozen=True, slots=True)
class ExecEvent:
    type: str
    level: str
    symbol: str | None
    message: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class IntentOutcome:
    """一个意图（可能多次尝试）的执行结果。"""

    symbol: str
    side: Side
    kind: OrderKind
    seq: int
    requested_qty: Decimal
    cids: list[str] = field(default_factory=list)
    status: IntentStatus = IntentStatus.PLANNED  # 最后一次尝试的状态
    executed_qty: Decimal = _ZERO
    cumm_quote_qty: Decimal = _ZERO
    fills: list[Fill] = field(default_factory=list)
    detail: str = ""

    @property
    def complete(self) -> bool:
        return self.executed_qty >= self.requested_qty

    @property
    def remaining_qty(self) -> Decimal:
        return max(self.requested_qty - self.executed_qty, _ZERO)

    def commission(self, asset: str) -> Decimal:
        return sum((f.commission for f in self.fills if f.commission_asset == asset), _ZERO)


@dataclass
class ExecutionReport:
    outcomes: list[IntentOutcome] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (symbol, 原因)
    events: list[ExecEvent] = field(default_factory=list)
    aborted: bool = False
    blocked_symbols: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class PendingResolution:
    cid: str
    symbol: str
    previous_status: IntentStatus
    new_status: IntentStatus
    detail: str


@dataclass
class PendingReport:
    resolved: list[PendingResolution] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)  # 仍无法确认的 cid（其 symbol 本次运行被阻断）
    events: list[ExecEvent] = field(default_factory=list)


@dataclass
class CancelReport:
    canceled: list[OrderResult] = field(default_factory=list)
    foreign_open_orders: list[OrderResult] = field(default_factory=list)  # 非本策略前缀的挂单（不动）
    events: list[ExecEvent] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PositionDiff:
    asset: str
    local_qty: Decimal
    exchange_qty: Decimal
    diff: Decimal
    diff_usdt: Decimal | None
    alert: bool
    initialized: bool = False


@dataclass
class PositionReport:
    diffs: list[PositionDiff] = field(default_factory=list)  # 只含超过 1 个 stepSize 的差额与首次登记
    quantities: dict[str, Decimal] = field(default_factory=dict)  # 对账后的受管资产数量（交易所数值）
    events: list[ExecEvent] = field(default_factory=list)


def intent_status_for(result: OrderResult) -> IntentStatus:
    """交易所订单状态 → 意图状态。未结束的订单返回 UNKNOWN。"""
    s = result.status
    if s is OrderStatus.FILLED:
        return IntentStatus.FILLED
    if s is OrderStatus.REJECTED:
        return IntentStatus.REJECTED
    if s in (OrderStatus.EXPIRED, OrderStatus.EXPIRED_IN_MATCH, OrderStatus.CANCELED):
        return IntentStatus.PARTIAL if result.executed_qty > 0 else IntentStatus.EXPIRED
    return IntentStatus.UNKNOWN


def _base_delta(fills: Iterable[Fill], base: str) -> Decimal:
    d = _ZERO
    for f in fills:
        d += f.qty if f.side is Side.BUY else -f.qty
        if f.commission_asset == base:
            d -= f.commission
    return d


class SpotExecutor:
    """一次运行内的现货执行器。sleep / clock / should_abort 均可注入。"""

    def __init__(
        self,
        account: SpotAccount,
        repo: StateRepo,
        exec_cfg: ExecutionConfig,
        *,
        strategy_id: str,
        account_id: str,
        run_id: str,
        run_date: date,
        should_abort: Callable[[], bool] = lambda: False,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = utc_now,
        quote_asset: str = "USDT",
        blocked_symbols: Iterable[str] = (),
    ) -> None:
        self.account = account
        self.repo = repo
        self.cfg = exec_cfg
        self.strategy_id = strategy_id
        self.account_id = account_id
        self.run_id = run_id
        self.run_date = run_date
        self.quote_asset = quote_asset
        self._should_abort = should_abort
        self._sleep = sleep
        self._now = MonotonicClock(clock)
        self.blocked_symbols: set[str] = set(blocked_symbols)

    # ---- 事件 ----

    def _event(
        self, sink: list[ExecEvent], type_: str, level: str, symbol: str | None, message: str, **payload: Any
    ) -> None:
        log.log(
            logging.WARNING if level != INFO else logging.INFO, "[%s] %s %s", type_, symbol or "", message
        )
        data = {"symbol": symbol, "message": message, **payload}
        self.repo.append_event(
            type_,
            level,
            data,
            strategy_id=self.strategy_id,
            account_id=self.account_id,
            run_id=self.run_id,
            now=self._now(),
        )
        sink.append(ExecEvent(type_, level, symbol, message, data))

    def _block(self, symbol: str, why: str) -> None:
        if symbol not in self.blocked_symbols:
            log.warning("本次运行不再对 %s 下单：%s", symbol, why)
        self.blocked_symbols.add(symbol)

    # ---- 下单（§6.5） ----

    def execute(
        self,
        orders: Sequence[PlannedOrder],
        *,
        max_attempts: int | None = None,
        balances: Mapping[str, Balance] | None = None,
    ) -> ExecutionReport:
        """按顺序执行订单。第 1 次尝试的意图在发送任何订单之前全部写库（PLANNED）。

        balances：执行前的交易所余额（用于记录 base_balance_before，供下次运行做余额核对）；
        不传时查询一次。每笔订单（含重试）之前检查 should_abort（KILL 文件）。
        """
        report = ExecutionReport()
        attempts = max_attempts if max_attempts is not None else self.cfg.max_attempts
        if attempts < 1:
            raise ValueError("max_attempts 必须 ≥ 1")
        symbols = [o.symbol for o in orders]
        if len(symbols) != len(set(symbols)):
            raise ValueError(f"同一批订单中有重复的交易对：{symbols}")
        todo: list[tuple[PlannedOrder, SymbolRules, int, str, Decimal]] = []
        new: list[NewIntent] = []
        if orders and balances is None:
            balances = self.account.get_balances()
        for o in orders:
            if o.symbol in self.blocked_symbols:
                report.skipped.append((o.symbol, "blocked"))
                continue
            rules = self.account.get_rules(o.symbol)
            seq = self.repo.allocate_seq(self.strategy_id, self.run_date)
            cid = make_client_order_id(self.strategy_id, self.run_date, o.symbol, o.kind, seq, 1)
            bal = (balances or {}).get(rules.base_asset)
            before = bal.total if bal is not None else _ZERO
            new.append(self._new_intent(o, rules, seq, 1, cid, o.quantity, o.price, before))
            todo.append((o, rules, seq, cid, before))
        self.repo.create_intents(new, now=self._now())

        for o, rules, seq, cid, before in todo:
            if report.aborted or self._should_abort():
                report.aborted = True
                self.repo.update_intent(cid, status=IntentStatus.CLOSED, note="KILL：未发送", now=self._now())
                report.skipped.append((o.symbol, "aborted"))
                continue
            report.outcomes.append(self._run_intent(o, rules, seq, cid, before, attempts, report))
        report.blocked_symbols = frozenset(self.blocked_symbols)
        return report

    def _new_intent(
        self,
        o: PlannedOrder,
        rules: SymbolRules,
        seq: int,
        attempt: int,
        cid: str,
        qty: Decimal,
        price: Decimal,
        before: Decimal,
    ) -> NewIntent:
        return NewIntent(
            cid=cid,
            run_id=self.run_id,
            strategy_id=self.strategy_id,
            account_id=self.account_id,
            symbol=o.symbol,
            side=o.side,
            kind=o.kind.value,
            run_date=self.run_date,
            seq=seq,
            attempt=attempt,
            intended_qty=qty,
            limit_price=price,
            base_asset=rules.base_asset,
            base_balance_before=before,
        )

    def _run_intent(
        self,
        o: PlannedOrder,
        rules: SymbolRules,
        seq: int,
        first_cid: str,
        base_before: Decimal,
        max_attempts: int,
        report: ExecutionReport,
    ) -> IntentOutcome:
        out = IntentOutcome(o.symbol, o.side, o.kind, seq, o.quantity)
        budget = o.quantity * o.price  # 买单的最大花费：重试时不超过第一次的金额
        cid, qty, price = first_cid, o.quantity, o.price
        residual_unplaceable = False  # 剩余数量低于过滤器下限：只记 INFO，不告警
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                if self._should_abort():
                    report.aborted = True
                    out.detail = "KILL：停止追单"
                    break
                try:
                    bid, ask = self.account.get_book_ticker(o.symbol)
                    ref = self.account.get_avg_price(o.symbol)
                except (ExchangeError, ExchangeUnavailableError) as e:
                    out.detail = f"重新获取报价失败：{e}"
                    break
                priced = price_limit_order(o.side, bid, ask, o.slippage, rules, ref)
                remaining = out.remaining_qty
                if o.side is Side.BUY and priced.price > 0:
                    remaining = min(remaining, (budget - out.cumm_quote_qty) / priced.price)
                qty = round_qty(remaining, rules)
                violations = check_order(rules, o.side, priced.price, qty, ref)
                if violations:
                    residual_unplaceable = True
                    out.detail = f"剩余数量无法下单：{violations[0]}"
                    log.info(
                        "%s 剩余 %s 无法下单（%s），不再追单", o.symbol, out.remaining_qty, violations[0]
                    )
                    break
                price = priced.price
                cid = make_client_order_id(self.strategy_id, self.run_date, o.symbol, o.kind, seq, attempt)
                before = base_before + _base_delta(out.fills, rules.base_asset)
                self.repo.create_intents(
                    [self._new_intent(o, rules, seq, attempt, cid, qty, price, before)], now=self._now()
                )
            out.cids.append(cid)
            status, result, fills, was_unknown = self._send(o, rules, cid, qty, price, report)
            out.status = status
            if result is not None:
                out.executed_qty += result.executed_qty
                out.cumm_quote_qty += result.cumm_quote_qty
                out.fills.extend(fills)
            if status is IntentStatus.FILLED or status in _STOP_STATUSES:
                break
            if was_unknown:
                out.detail = "经历过状态未知，本次运行不再追单"
                break
        if (
            not out.complete
            and out.status in (IntentStatus.PARTIAL, IntentStatus.EXPIRED)
            and not report.aborted
            and not residual_unplaceable
        ):
            self._event(
                report.events,
                EV_ORDER_INCOMPLETE,
                WARN,
                o.symbol,
                f"{o.side.value} 计划 {o.quantity}，成交 {out.executed_qty}，不再追单",
                side=o.side.value,
                requested=o.quantity,
                executed=out.executed_qty,
                cids=list(out.cids),
                detail=out.detail,
            )
        return out

    def _send(
        self,
        o: PlannedOrder,
        rules: SymbolRules,
        cid: str,
        qty: Decimal,
        price: Decimal,
        report: ExecutionReport,
    ) -> tuple[IntentStatus, OrderResult | None, list[Fill], bool]:
        """发送一次尝试。返回 (意图状态, 订单结果, 成交, 是否经历过状态未知)。"""
        req = OrderRequest(o.symbol, o.side, OrderType.LIMIT, qty, cid, price, TimeInForce.IOC)
        self.repo.update_intent(cid, status=IntentStatus.SENT, now=self._now())
        try:
            result = self.account.place_order(req)
        except UnknownOrderStatus as e:
            status, res, fills = self._resolve_unknown(cid, o.symbol, o.side, rules, e.reason, report.events)
            return status, res, fills, True
        except RateLimitedError as e:
            # 429/418：请求未被受理；整个进程停止发送请求（spec §3.5）
            self.repo.update_intent(
                cid,
                status=IntentStatus.REJECTED,
                error_code=e.http_status,
                note=f"HTTP {e.http_status} 限频",
                now=self._now(),
            )
            raise
        except ExchangeUnavailableError as e:
            # 请求发出之前就失败（例如对时失败）：订单肯定没有发出
            self.repo.update_intent(cid, status=IntentStatus.CLOSED, note=f"未发出：{e}", now=self._now())
            raise
        except ExchangeError as e:
            self.repo.update_intent(
                cid, status=IntentStatus.REJECTED, error_code=e.code, note=e.message, now=self._now()
            )
            self._event(
                report.events,
                EV_ORDER_REJECTED,
                WARN,
                o.symbol,
                f"[{e.code}] {e.message}",
                cid=cid,
                side=o.side.value,
                qty=qty,
                price=price,
                code=e.code,
            )
            return IntentStatus.REJECTED, None, [], False
        # 其他异常（含进程被杀）不捕获：意图保持 SENT，下一次运行对账时处理
        was_unknown = False
        if not result.status.is_final:
            # IOC 订单不应停留在挂单状态：等待其结束，必要时撤单
            final = self._finalize(o.symbol, cid, result)
            if final is None:
                status, res, fills = self._mark_unresolved(
                    cid, o.symbol, "订单未结束且无法撤单", report.events
                )
                return status, res, fills, True
            result, was_unknown = final, True
        status, fills = self._record_result(cid, o.side, rules, result)
        return status, result, fills, was_unknown

    def _fills_for(self, result: OrderResult) -> list[Fill]:
        """成交明细：FULL 响应里的 fills 齐全时直接用，否则用 myTrades 补全（spec §6.6）。"""
        fills = list(result.fills)
        if result.executed_qty <= 0:
            return fills
        if sum((f.qty for f in fills), _ZERO) == result.executed_qty:
            return fills
        if result.order_id is None:
            return fills
        try:
            return self.account.get_trades(result.symbol, result.order_id)
        except (ExchangeError, ExchangeUnavailableError) as e:
            log.warning("%s 订单 %s 的成交明细获取失败：%s", result.symbol, result.client_order_id, e)
            return fills

    def _record_result(
        self, cid: str, side: Side, rules: SymbolRules, result: OrderResult
    ) -> tuple[IntentStatus, list[Fill]]:
        status = intent_status_for(result)
        fills = self._fills_for(result)
        self.repo.upsert_order(
            result, strategy_id=self.strategy_id, account_id=self.account_id, side=side, now=self._now()
        )
        if fills:
            self.repo.upsert_fills(
                fills,
                cid=cid,
                strategy_id=self.strategy_id,
                account_id=self.account_id,
                base_asset=rules.base_asset,
                quote_asset=rules.quote_asset,
                now=self._now(),
            )
        self.repo.update_intent(
            cid,
            status=status,
            exchange_order_id=result.order_id,
            executed_qty=result.executed_qty,
            cumm_quote_qty=result.cumm_quote_qty,
            note=f"expiryReason={result.expiry_reason}" if result.expiry_reason else None,
            now=self._now(),
        )
        if side is Side.BUY and result.executed_qty > 0:
            intent = self.repo.get_intent(cid)
            as_of = intent.created_at if intent is not None else self._now()
            _, newly = self.repo.ensure_managed_position(
                self.account_id, self.strategy_id, rules.base_asset, as_of=as_of
            )
            if newly:
                log.info("首次买入 %s，登记为受管资产（spec §7）", rules.base_asset)
        return status, fills

    # ---- 状态未知（§6.6） ----

    def _poll(self, symbol: str, cid: str) -> tuple[OrderResult | None, bool]:
        """按配置的间隔查单。返回 (最后查到的订单, 是否每次都明确返回 -2013)。"""
        last: OrderResult | None = None
        all_not_found = True
        for delay in self.cfg.unknown_status_query_delays_sec:
            self._sleep(delay)
            try:
                res = self.account.get_order(symbol, cid)
            except (ExchangeError, ExchangeUnavailableError) as e:
                all_not_found = False
                log.warning("查询 %s %s 失败：%s", symbol, cid, e)
                continue
            if res is None:
                continue
            all_not_found = False
            last = res
            if res.status.is_final:
                break
        return last, all_not_found and last is None

    def _finalize(self, symbol: str, cid: str, result: OrderResult) -> OrderResult | None:
        """等待未结束的订单结束；仍未结束则撤单。无法确认时返回 None。"""
        found, _ = self._poll(symbol, cid)
        if found is not None and found.status.is_final:
            return found
        try:
            canceled = self.account.cancel_order(symbol, cid)
            if canceled.status.is_final:
                return canceled
        except (UnknownOrderStatus, ExchangeError, ExchangeUnavailableError) as e:
            log.warning("撤销未结束的订单 %s 失败：%s", cid, e)
        try:
            again = self.account.get_order(symbol, cid)
        except (ExchangeError, ExchangeUnavailableError):
            return None
        return again if again is not None and again.status.is_final else None

    def _mark_unresolved(
        self, cid: str, symbol: str, why: str, events: list[ExecEvent]
    ) -> tuple[IntentStatus, None, list[Fill]]:
        self.repo.update_intent(cid, status=IntentStatus.UNKNOWN, note=why, now=self._now())
        self._block(symbol, why)
        self._event(
            events,
            EV_ORDER_STATUS_UNKNOWN,
            WARN,
            symbol,
            f"{cid}：{why}；本次运行不再对该交易对下单",
            cid=cid,
        )
        return IntentStatus.UNKNOWN, None, []

    def _resolve_unknown(
        self, cid: str, symbol: str, side: Side, rules: SymbolRules, reason: str, events: list[ExecEvent]
    ) -> tuple[IntentStatus, OrderResult | None, list[Fill]]:
        """spec §6.6 第 1、2 步：记为 UNKNOWN，按间隔查单。绝不换新 ID 重下。"""
        self.repo.update_intent(cid, status=IntentStatus.UNKNOWN, note=f"状态未知：{reason}", now=self._now())
        log.warning("%s 下单状态未知（%s），开始查单", cid, reason)
        try:
            found, all_not_found = self._poll(symbol, cid)
        except RateLimitedError:
            self._block(symbol, "查单时被限频")
            raise
        if found is not None and not found.status.is_final:
            found = self._finalize(symbol, cid, found)
            if found is None:
                return self._mark_unresolved(cid, symbol, "订单未结束且无法撤单", events)
        if found is not None:
            status, fills = self._record_result(cid, side, rules, found)
            self._event(
                events,
                EV_INTENT_RESOLVED,
                INFO,
                symbol,
                f"{cid} 查单确认为 {found.status.value}",
                cid=cid,
                status=status.value,
                executed=found.executed_qty,
            )
            return status, found, fills
        if all_not_found:
            self.repo.update_intent(
                cid, status=IntentStatus.NOT_FOUND, note="查单全部返回 -2013", now=self._now()
            )
            self._block(symbol, "下单状态未知且查不到订单")
            self._event(
                events,
                EV_ORDER_STATUS_UNKNOWN,
                WARN,
                symbol,
                f"{cid} 状态未知，查单全部返回 -2013；本次运行不再对该交易对下单，下次运行核对余额",
                cid=cid,
                reason=reason,
                status=IntentStatus.NOT_FOUND.value,
            )
            return IntentStatus.NOT_FOUND, None, []
        return self._mark_unresolved(cid, symbol, f"状态未知（{reason}），查单失败", events)

    # ---- 下一次运行：处理遗留意图（§6.6 第 3 步） ----

    def reconcile_pending_intents(self, balances: Mapping[str, Balance] | None = None) -> PendingReport:
        """处理以前运行遗留的 PLANNED / SENT / UNKNOWN / NOT_FOUND 意图（spec §6.2 第 4 步）。

        - 查得到：按查询结果更新，并用 myTrades 补全成交；
        - 仍查不到：余额变化能被已知成交解释 → CLOSED（视为从未下单）；
          不能解释 → 写 INFERRED_FILL 事件并告警，以交易所余额为准，意图同样关闭；
        - 查询失败：保持原状态，本次运行阻断该交易对。
        """
        report = PendingReport()
        pending = self.repo.list_intents(self.strategy_id, self.account_id, statuses=PENDING_STATUSES)
        if not pending:
            return report
        if balances is None:
            balances = self.account.get_balances()
        for intent in pending:
            prev = IntentStatus(intent.status)
            try:
                found = self.account.get_order(intent.symbol, intent.cid)
            except (ExchangeError, ExchangeUnavailableError) as e:
                report.unresolved.append(intent.cid)
                self._block(intent.symbol, "遗留意图查询失败")
                self._event(
                    report.events,
                    EV_INTENT_UNRESOLVED,
                    WARN,
                    intent.symbol,
                    f"遗留意图 {intent.cid}（{prev.value}）查询失败：{e}；本次运行不再对该交易对下单",
                    cid=intent.cid,
                )
                continue
            if found is not None:
                self._resolve_found(intent, prev, found, report)
            else:
                self._resolve_missing(intent, prev, balances, report)
        return report

    def _rules_or_none(self, symbol: str) -> SymbolRules | None:
        try:
            return self.account.get_rules(symbol)
        except (ExchangeError, ExchangeUnavailableError):
            return None

    def _resolve_found(
        self, intent: OrderIntentRow, prev: IntentStatus, found: OrderResult, report: PendingReport
    ) -> None:
        if not found.status.is_final:
            final = self._finalize(intent.symbol, intent.cid, found)
            if final is None:
                report.unresolved.append(intent.cid)
                self._mark_unresolved(intent.cid, intent.symbol, "遗留订单未结束且无法撤单", report.events)
                return
            found = final
        rules = self._rules_or_none(intent.symbol)
        if rules is None:
            # 交易对已不在 exchangeInfo 中：用意图里记录的 base 资产
            rules = _fallback_rules(intent, self.quote_asset)
        status, _ = self._record_result(intent.cid, Side(intent.side), rules, found)
        detail = f"遗留意图（{prev.value}）查单确认为 {found.status.value}，成交 {found.executed_qty}"
        level = WARN if prev is IntentStatus.NOT_FOUND else INFO
        self._event(report.events, EV_INTENT_RESOLVED, level, intent.symbol, detail, cid=intent.cid)
        report.resolved.append(PendingResolution(intent.cid, intent.symbol, prev, status, detail))

    def _resolve_missing(
        self,
        intent: OrderIntentRow,
        prev: IntentStatus,
        balances: Mapping[str, Balance],
        report: PendingReport,
    ) -> None:
        base = intent.base_asset
        bal = balances.get(base)
        actual = bal.total if bal is not None else _ZERO
        known = self.repo.net_asset_change(
            base, strategy_id=self.strategy_id, account_id=self.account_id, recorded_after=intent.created_at
        )
        before = intent.base_balance_before
        rules = self._rules_or_none(intent.symbol)
        tol = rules.step_size if rules is not None and rules.step_size > 0 else _ZERO
        payload: dict[str, Any] = {
            "cid": intent.cid,
            "base_asset": base,
            "balance_before": before,
            "known_change": known,
            "balance_now": actual,
        }
        if before is not None and abs(actual - (before + known)) <= tol:
            detail = "仍查不到订单，余额变化可由已知成交解释：视为从未下单"
            self.repo.update_intent(intent.cid, status=IntentStatus.CLOSED, note=detail, now=self._now())
            self._event(
                report.events, EV_INTENT_CLOSED, INFO, intent.symbol, f"{intent.cid}：{detail}", **payload
            )
            report.resolved.append(
                PendingResolution(intent.cid, intent.symbol, prev, IntentStatus.CLOSED, detail)
            )
            return
        diff = actual - (before + known) if before is not None else None
        detail = (
            f"仍查不到订单，但 {base} 余额变化无法由已知成交解释（差额 {diff}）：推断成交，以交易所余额为准"
        )
        self.repo.update_intent(
            intent.cid, status=IntentStatus.CLOSED, note=f"推断成交：差额 {diff}", now=self._now()
        )
        self._event(
            report.events,
            EV_INFERRED_FILL,
            WARN,
            intent.symbol,
            f"{intent.cid}：{detail}",
            diff=diff,
            **payload,
        )
        report.resolved.append(
            PendingResolution(intent.cid, intent.symbol, prev, IntentStatus.CLOSED, detail)
        )

    # ---- 撤单（§6.7） ----

    def cancel_strategy_open_orders(self, symbols: Iterable[str]) -> CancelReport:
        """撤销指定交易对上本策略前缀的挂单（v1 只用 IOC，正常情况下没有挂单）。

        无法确认是否有挂单（查询失败、撤单状态未知）的交易对，本次运行阻断。
        """
        report = CancelReport()
        for symbol in sorted(set(symbols)):
            try:
                open_orders = self.account.get_open_orders(symbol)
            except (ExchangeError, ExchangeUnavailableError) as e:
                self._block(symbol, f"查询挂单失败：{e}")
                continue
            for o in open_orders:
                if not is_strategy_order(o.client_order_id, self.strategy_id):
                    report.foreign_open_orders.append(o)
                    log.info("%s 上有非本策略的挂单 %s，不处理", symbol, o.client_order_id)
                    continue
                try:
                    res = self.account.cancel_order(symbol, o.client_order_id)
                except UnknownOrderStatus as e:
                    self._block(symbol, f"撤单状态未知：{e.reason}")
                    self._event(
                        report.events,
                        EV_ORDER_STATUS_UNKNOWN,
                        WARN,
                        symbol,
                        f"撤销挂单 {o.client_order_id} 状态未知；本次运行不再对该交易对下单",
                        cid=o.client_order_id,
                    )
                    continue
                except (ExchangeError, ExchangeUnavailableError) as e:
                    # 例如 -2011：订单已在此期间结束
                    log.warning("撤销 %s 失败：%s，改为查单", o.client_order_id, e)
                    try:
                        q = self.account.get_order(symbol, o.client_order_id)
                    except (ExchangeError, ExchangeUnavailableError):
                        q = None
                    if q is None or not q.status.is_final:
                        self._block(symbol, "撤单失败且无法确认挂单状态")
                        continue
                    res = q
                self._record_external(res, symbol)
                report.canceled.append(res)
                self._event(
                    report.events,
                    EV_OPEN_ORDER_CANCELED,
                    WARN,
                    symbol,
                    f"撤销本策略前缀的挂单 {o.client_order_id}"
                    f"（{res.status.value}，已成交 {res.executed_qty}）",
                    cid=o.client_order_id,
                    executed=res.executed_qty,
                )
        return report

    def _record_external(self, res: OrderResult, symbol: str) -> None:
        """记录撤销的挂单及其成交；若是本系统的意图，同时更新意图状态。"""
        rules = self._rules_or_none(symbol)
        intent = self.repo.get_intent(res.client_order_id)
        side_raw = res.raw.get("side") if res.raw else None
        side = Side(intent.side) if intent is not None else (Side(str(side_raw)) if side_raw else None)
        if intent is not None and rules is not None and side is not None:
            self._record_result(res.client_order_id, side, rules, res)
            return
        self.repo.upsert_order(
            res, strategy_id=self.strategy_id, account_id=self.account_id, side=side, now=self._now()
        )
        if rules is not None and res.executed_qty > 0:
            self.repo.upsert_fills(
                self._fills_for(res),
                cid=res.client_order_id,
                strategy_id=self.strategy_id,
                account_id=self.account_id,
                base_asset=rules.base_asset,
                quote_asset=rules.quote_asset,
                now=self._now(),
            )

    # ---- 对账（§6.8） ----

    def reconcile_positions(
        self,
        balances: Mapping[str, Balance] | None = None,
        *,
        prices: Mapping[str, Decimal] | None = None,
        alert_usdt: Decimal = RECONCILE_ALERT_USDT,
    ) -> PositionReport:
        """交易所余额是唯一真相（spec §6.8）。

        对每个受管资产（含 USDT）：本地记录 = 上次对账数量 + 之后记录的成交净额（含手续费），
        与交易所余额比较；差额 > 1 个 stepSize 时写 RECONCILE_DIFF 事件（市值 > alert_usdt 为 WARN），
        然后一律用交易所数值覆盖本地记录，并更新灰尘标记。prices：asset → USDT 价格（不传时查询）。
        USDT 第一次对账时直接登记，不算差额。
        """
        report = PositionReport()
        if balances is None:
            balances = self.account.get_balances()
        positions = {
            p.asset: p for p in self.repo.list_positions(self.account_id, self.strategy_id, managed_only=True)
        }
        assets = sorted(set(positions) | {self.quote_asset})
        rules = {a: self._rules_or_none(f"{a}{self.quote_asset}") for a in assets if a != self.quote_asset}
        if prices is None:
            try:
                raw = self.account.get_prices(
                    [f"{a}{self.quote_asset}" for a in assets if a != self.quote_asset]
                )
            except (ExchangeError, ExchangeUnavailableError) as e:
                log.warning("对账时获取价格失败：%s", e)
                raw = {}
            prices = {s[: -len(self.quote_asset)]: p for s, p in raw.items() if s.endswith(self.quote_asset)}
        for asset in assets:
            bal = balances.get(asset)
            actual = bal.total if bal is not None else _ZERO
            price = Decimal(1) if asset == self.quote_asset else prices.get(asset)
            r = rules.get(asset)
            dust = bool(r is not None and price is not None and is_dust(actual, price, r))
            pos = positions.get(asset)
            now = self._now()
            if pos is None:
                self.repo.upsert_position(
                    self.account_id, self.strategy_id, asset, qty=actual, managed=True, dust=dust, now=now
                )
                report.diffs.append(PositionDiff(asset, _ZERO, actual, actual, None, False, initialized=True))
                report.quantities[asset] = actual
                continue
            known = self.repo.net_asset_change(
                asset, strategy_id=self.strategy_id, account_id=self.account_id, recorded_after=pos.updated_at
            )
            expected = pos.qty + known
            diff = actual - expected
            tol = r.step_size if r is not None and r.step_size > 0 else _ZERO
            if abs(diff) > tol:
                diff_usdt = abs(diff) * price if price is not None else None
                alert = diff_usdt is None or diff_usdt > alert_usdt
                report.diffs.append(PositionDiff(asset, expected, actual, diff, diff_usdt, alert))
                self._event(
                    report.events,
                    EV_RECONCILE_DIFF,
                    WARN if alert else INFO,
                    None,
                    f"{asset} 本地 {expected}，交易所 {actual}，差额 {diff}"
                    f"（约 {diff_usdt} USDT），以交易所为准",
                    asset=asset,
                    local=expected,
                    exchange=actual,
                    diff=diff,
                    diff_usdt=diff_usdt,
                )
            self.repo.upsert_position(
                self.account_id, self.strategy_id, asset, qty=actual, dust=dust, now=now
            )
            report.quantities[asset] = actual
        return report


def _fallback_rules(intent: OrderIntentRow, quote_asset: str) -> SymbolRules:
    return SymbolRules(
        symbol=intent.symbol,
        base_asset=intent.base_asset,
        quote_asset=quote_asset,
        status=SymbolStatus.UNKNOWN,
        tick_size=_ZERO,
        min_price=_ZERO,
        max_price=_ZERO,
        step_size=_ZERO,
        min_qty=_ZERO,
        max_qty=_ZERO,
        min_notional=_ZERO,
    )
