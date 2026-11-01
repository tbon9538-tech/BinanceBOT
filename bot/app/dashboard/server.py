"""管理界面（D-075）：`python -m bot dashboard`，浏览器查看运行状态并执行运维操作。

安全设计：
- 整个站点要求 HTTP Basic 登录，密码来自 .env 的 DASHBOARD_PASSWORD（至少 12 位；用户名任意）；
  没有密码拒绝启动。登录失败等待 1 秒再返回。
- 只监听本机地址；远程访问通过 Tailscale 等私有网络转发（不直接暴露到公网）。
- 所有操作都是 POST，带 CSRF 令牌并检查 Origin；kill 需要输入确认词 KILL，其余有副作用的操作需要勾选确认。
- 每次操作写入 DASHBOARD_OP 事件，留下审计记录。
- 耗时的操作（kill、立即运行、确认余额）在后台线程执行，同一时间只允许一个；结果显示在"操作记录"中。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import hashlib
import hmac
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from bot import paths
from bot.app import ops
from bot.app.dashboard import charts
from bot.app.dashboard.data import BEIJING, Overview, build_overview, event_summary
from bot.app.run_once import QUOTE, DailyRunner, RunDeps, _prices_for, managed_assets
from bot.config import AppConfig
from bot.data.store import MarketStore
from bot.domain.errors import ConfigError
from bot.domain.types import Env
from bot.persistence.repo import StateRepo
from bot.risk import ledger

log = logging.getLogger(__name__)

PASSWORD_ENV = "DASHBOARD_PASSWORD"
MIN_PASSWORD_LEN = 12
KILL_WORD = "KILL"
LIVE_CACHE_SEC = 60.0
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
}


@dataclass
class OpRecord:
    time: datetime
    op: str
    status: str  # 进行中 / 完成 / 失败 / 拒绝
    message: str


@dataclass(frozen=True)
class LiveView:
    time: datetime
    nav: float
    rows: list[tuple[str, float, float, float]]  # 资产、数量、市值（USDT）、占净值
    usdt: float


class Dashboard:
    """界面的运行时状态：配置、状态库、交易所依赖（按需创建）、操作记录、后台操作锁。"""

    def __init__(
        self,
        cfg: AppConfig,
        repo: StateRepo,
        *,
        password: str,
        kill_file: Path,
        deps_factory: Callable[[], RunDeps],
        store: MarketStore | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.cfg, self.repo, self.kill_file, self.store = cfg, repo, kill_file, store
        self.password = password
        self.csrf = hmac.new(password.encode(), b"dashboard-csrf", hashlib.sha256).hexdigest()
        self._deps_factory = deps_factory
        self._deps: RunDeps | None = None
        self.now = now
        self.log: deque[OpRecord] = deque(maxlen=20)
        self.busy = threading.Lock()
        self._live: tuple[float, LiveView | str] | None = None

    # ---- 依赖与实时数据 ----

    def deps(self) -> RunDeps:
        if self._deps is None:
            self._deps = self._deps_factory()
        return self._deps

    def live(self) -> LiveView | str:
        """交易所实时余额与市值（缓存 60 秒）。失败时返回错误说明，页面其余部分照常显示。"""
        if self._live is not None and time.monotonic() - self._live[0] < LIVE_CACHE_SEC:
            return self._live[1]
        result: LiveView | str
        try:
            deps = self.deps()
            view = DailyRunner(deps)._view()
            balances = view.get_balances()
            managed = managed_assets(deps.repo, self.cfg)
            # 交易规则在账户对象里缓存，只有第一次会请求 exchangeInfo；用它过滤掉 USDT 本身和已下架的交易对
            prices = _prices_for(view, managed, view.get_all_rules())
            nb = ledger.compute_nav(balances, prices, managed)
            nav = float(nb.nav)
            rows = [
                (a, float(balances[a].total), float(v), float(v) / nav if nav > 0 else 0.0)
                for a, v in sorted(nb.positions.items(), key=lambda x: -x[1])
            ]
            usdt = float(balances[QUOTE].total) if QUOTE in balances else 0.0
            result = LiveView(self.now(), nav, rows, usdt)
        except Exception as e:  # 交易所不可用、没有 Key 等：只影响"实时持仓"一节
            log.warning("管理界面获取实时余额失败：%s", e)
            result = f"{type(e).__name__}: {e}"
        self._live = (time.monotonic(), result)
        return result

    # ---- 操作 ----

    def _audit(self, rec: OpRecord) -> None:
        try:
            self.repo.append_event(
                "DASHBOARD_OP", "INFO", {"op": rec.op, "status": rec.status, "message": rec.message},
                strategy_id=self.cfg.strategy_id, account_id=self.cfg.account_id,
            )  # fmt: skip
        except Exception:
            log.exception("写入 DASHBOARD_OP 事件失败")

    def record(self, op: str, status: str, message: str) -> None:
        rec = OpRecord(self.now(), op, status, message)
        self.log.appendleft(rec)
        self._audit(rec)

    def run_sync(self, op: str, fn: Callable[[], str]) -> None:
        try:
            self.record(op, "完成", fn())
        except ops.OpRefused as e:
            self.record(op, "拒绝", str(e))

    def run_background(self, op: str, fn: Callable[[], str]) -> threading.Thread | None:
        """在后台线程执行；同一时间只允许一个。返回线程（测试用），被拒绝时返回 None。"""
        if not self.busy.acquire(blocking=False):
            self.record(op, "拒绝", "已有操作在进行，请等它结束")
            return None
        rec = OpRecord(self.now(), op, "进行中", "已开始，完成后刷新页面查看结果")
        self.log.appendleft(rec)

        def work() -> None:
            try:
                rec.status, rec.message = "完成", fn()
            except ops.OpRefused as e:
                rec.status, rec.message = "拒绝", str(e)
            except Exception as e:
                log.exception("管理界面操作 %s 失败", op)
                rec.status, rec.message = "失败", f"{type(e).__name__}: {e}"
            finally:
                self._live = None
                self.busy.release()
            self._audit(rec)

        t = threading.Thread(target=work, name=f"dashboard-{op}", daemon=True)
        t.start()
        return t


# ---- 请求处理 ----


def _authorized(header: str | None, password: str) -> bool:
    if not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    _, sep, given = decoded.partition(":")
    return bool(sep) and hmac.compare_digest(given.encode(), password.encode())


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return True  # 不发送 Origin 的旧浏览器：仍有 CSRF 令牌保护
    return urlparse(origin).netloc == request.headers.get("host")


def _auth_middleware(
    password: str,
) -> Callable[[Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]]:
    async def dispatch(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if not _authorized(request.headers.get("authorization"), password):
            await asyncio.sleep(1.0)
            resp: Response = PlainTextResponse(
                "需要登录",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="binance-bot", charset="UTF-8"'},
            )
        else:
            resp = await call_next(request)
        resp.headers.update(SECURITY_HEADERS)
        return resp

    return dispatch


def create_app(dash: Dashboard) -> Starlette:
    cfg = dash.cfg

    async def index(request: Request) -> Response:
        ov: Overview = await run_in_threadpool(
            lambda: build_overview(cfg, dash.repo, dash.kill_file, store=dash.store, now=dash.now())
        )
        live = await run_in_threadpool(dash.live)
        weights = sorted(((s, w) for m in ov.targets.values() for s, w in m.items()), key=lambda x: -x[1])
        ctx: dict[str, Any] = {
            "ov": ov,
            "live": live,
            "is_live_view": isinstance(live, LiveView),
            "nav_chart": charts.nav_chart(ov.nav_history),
            "regime_chart": charts.regime_chart(ov.regime_history),
            "target_bars": charts.weight_bars(weights, total=1.0),
            "live_bars": charts.weight_bars([(a, w) for a, _, _, w in live.rows], total=1.0)
            if isinstance(live, LiveView)
            else None,
            "ops_log": list(dash.log),
            "busy": dash.busy.locked(),
            "csrf": dash.csrf,
            "kill_word": KILL_WORD,
            "allow_run": cfg.env is not Env.LIVE,
            "utc": _fmt_utc,
            "bj": _fmt_bj,
            "event_summary": event_summary,
            "regime_mode": cfg.regime.mode,
            "tg_ok": bool(os.environ.get("TELEGRAM_BOT_TOKEN")) and bool(os.environ.get("TELEGRAM_CHAT_ID")),
            "hb_ok": bool(os.environ.get("HEARTBEAT_URL")) and cfg.notify.heartbeat_enabled,
        }
        return TEMPLATES.TemplateResponse(request, "index.html", ctx)

    async def op(request: Request) -> Response:
        name = request.path_params["name"]
        form = await request.form()
        if not _same_origin(request) or not hmac.compare_digest(str(form.get("csrf", "")), dash.csrf):
            return PlainTextResponse("请求来源校验失败，请刷新页面后重试", status_code=403)
        checked = form.get("confirm") == "1"
        if name == "pause":
            await run_in_threadpool(
                dash.run_sync, "暂停", lambda: ops.pause(cfg, dash.repo, source="管理界面")
            )
        elif name == "resume":
            after_kill = form.get("after_kill") == "1"
            await run_in_threadpool(
                dash.run_sync, "恢复",
                lambda: ops.resume(cfg, dash.repo, dash.kill_file, after_kill=after_kill, source="管理界面"),
            )  # fmt: skip
        elif name == "run" and checked:
            dash.run_background("立即运行一次", lambda: _run_text(ops.run_now(dash.deps())))
        elif name == "ack" and checked:
            note = str(form.get("note", ""))[:200] or None
            dash.run_background("确认当前余额", lambda: ops.acknowledge(dash.deps(), note=note))
        elif name == "kill" and str(form.get("confirm_text", "")).strip() == KILL_WORD:
            dash.run_background("紧急停止", lambda: ops.kill(dash.deps(), reason="管理界面"))
        elif name == "unkill" and checked:
            await run_in_threadpool(
                dash.run_sync, "删除 KILL 文件", lambda: _remove_kill_file(dash.kill_file)
            )
        elif name in ("run", "ack", "kill", "unkill"):
            dash.record(name, "拒绝", "没有完成确认（勾选确认，kill 需要输入 KILL）")
        else:
            return PlainTextResponse("未知操作", status_code=404)
        return RedirectResponse("/#ops", status_code=303)

    return Starlette(
        routes=[Route("/", index), Route("/ops/{name}", op, methods=["POST"])],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=_auth_middleware(dash.password))],
    )


def _fmt_utc(t: datetime | None) -> str:
    return t.strftime("%Y-%m-%d %H:%M UTC") if t else "—"


def _fmt_bj(t: datetime | None) -> str:
    return (t + BEIJING).strftime("%m-%d %H:%M") if t else "—"


def _run_text(res: Any) -> str:
    return f"{res.run_id}：{res.status}" + ("；" + "；".join(res.messages[:3]) if res.messages else "")


def _remove_kill_file(kill_file: Path) -> str:
    if not kill_file.exists():
        return "KILL 文件不存在"
    kill_file.unlink()
    return "已删除 KILL 文件（取消尚未执行的紧急停止）"


# ---- 命令行入口 ----


def load_password() -> str:
    pw = os.environ.get(PASSWORD_ENV, "")
    if len(pw) < MIN_PASSWORD_LEN:
        raise ConfigError(f"请在 .env 中设置 {PASSWORD_ENV}（至少 {MIN_PASSWORD_LEN} 位），管理界面才能启动")
    return pw


def cmd_dashboard(cfg: AppConfig, args: argparse.Namespace) -> int:
    import uvicorn

    from bot.app.ops_commands import build_deps
    from bot.app.run_once import KILL_FILE
    from bot.logging_setup import setup_logging

    load_dotenv(Path(".env"), override=False)
    setup_logging(cfg.logging.level, Path(cfg.logging.dir), cfg.logging.retention_days)
    dash = Dashboard(
        cfg,
        StateRepo.open(paths.state_db(cfg.env.value)),
        password=load_password(),
        kill_file=KILL_FILE,
        deps_factory=lambda: build_deps(cfg, allow_live=cfg.env is Env.LIVE),
        store=MarketStore(paths.MARKET_DB),
    )
    log.info("管理界面启动：http://%s:%s（%s）", args.host, args.port, cfg.env.value)
    uvicorn.run(create_app(dash), host=args.host, port=args.port, log_level="warning", proxy_headers=False)
    return 0
