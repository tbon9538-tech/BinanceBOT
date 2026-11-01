"""管理界面（D-075）：`python -m bot dashboard`，浏览器查看运行状态并执行运维操作。

安全设计：
- 整个站点要求 HTTP Basic 登录：用户名 DASHBOARD_USERNAME（默认 admin）、密码 DASHBOARD_PASSWORD（.env）。
  密码至少 8 位，live 环境至少 12 位（D-078）；不满足时拒绝启动。登录失败等待 1 秒再返回。
- 只监听本机地址；远程访问通过 Tailscale 等私有网络转发（不直接暴露到公网）。
- 所有操作都是 POST，带 CSRF 令牌并检查 Origin；kill 需要输入确认词 KILL，其余有副作用的操作需要勾选确认。
- 每次操作写入 DASHBOARD_OP 事件，留下审计记录。
- 页面只加载同源的 /static/app.js、app.css（CSP script-src 'self'），不引用任何外部资源；
  不开脚本时页面仍可用（D-080）。图表数据以 application/json 块内嵌，由 app.js 渲染交互图。
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
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from bot import paths
from bot.app import ops
from bot.app.daemon import alive_path
from bot.app.dashboard import charts
from bot.app.dashboard.data import (
    BEIJING,
    Overview,
    build_overview,
    compare_holdings,
    event_summary,
    headline,
    health_checks,
)
from bot.app.run_once import QUOTE, DailyRunner, RunDeps, _prices_for, managed_assets
from bot.config import AppConfig
from bot.data.store import MarketStore
from bot.domain.errors import ConfigError
from bot.domain.types import Env
from bot.persistence.repo import StateRepo
from bot.risk import ledger

log = logging.getLogger(__name__)

USERNAME_ENV = "DASHBOARD_USERNAME"
PASSWORD_ENV = "DASHBOARD_PASSWORD"
DEFAULT_USERNAME = "admin"
MIN_PASSWORD_LEN = 8
MIN_PASSWORD_LEN_LIVE = 12  # 界面能清仓：实盘时不接受短密码
KILL_WORD = "KILL"
LIVE_CACHE_SEC = 60.0
HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))
STATIC_DIR = HERE / "static"
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; connect-src 'self'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'",
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
        username: str = DEFAULT_USERNAME,
        store: MarketStore | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.cfg, self.repo, self.kill_file, self.store = cfg, repo, kill_file, store
        self.alive_file: Path | None = None  # 常驻进程的存活标记（D-080）；由 cmd_dashboard 设置
        self.username, self.password = username, password
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


def _authorized(header: str | None, username: str, password: str) -> bool:
    if not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    user, sep, given = decoded.partition(":")
    # 两项都比较完再合并结果，不因用户名先错而提前返回
    user_ok = hmac.compare_digest(user.encode(), username.encode())
    pw_ok = hmac.compare_digest(given.encode(), password.encode())
    return bool(sep) and user_ok and pw_ok


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return True  # 不发送 Origin 的旧浏览器：仍有 CSRF 令牌保护
    return urlparse(origin).netloc == request.headers.get("host")


def _auth_middleware(
    username: str, password: str
) -> Callable[[Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]]:
    async def dispatch(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if not _authorized(request.headers.get("authorization"), username, password):
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


def _base_ctx(dash: Dashboard) -> dict[str, Any]:
    return {
        "utc": _fmt_utc,
        "bj": _fmt_bj,
        "csrf": dash.csrf,
        "min_trade": float(dash.cfg.rebalance.min_trade_usdt),
    }


def _chart_data(ov: Overview) -> dict[str, Any]:
    """交给 app.js 的图表数据：紧凑数组，日期为 ISO 字符串。"""
    return {
        "nav": [[p.day.isoformat(), round(p.navu, 6), round(p.drawdown, 6)] for p in ov.nav_history],
        "regime": [
            [p.day.isoformat(), round(p.close, 2), None if p.sma is None else round(p.sma, 2), int(p.risk_on)]
            for p in ov.regime_history
        ],
    }


def create_app(dash: Dashboard) -> Starlette:
    cfg = dash.cfg

    def overview() -> Overview:
        return build_overview(
            cfg, dash.repo, dash.kill_file, store=dash.store, alive_file=dash.alive_file, now=dash.now()
        )

    def live_ctx(ov: Overview) -> dict[str, Any]:
        live = dash.live()
        ctx: dict[str, Any] = {"live": live, "is_live_view": isinstance(live, LiveView), "ov": ov}
        if isinstance(live, LiveView):
            capital, rows = compare_holdings(
                [(a, q, v) for a, q, v, _ in live.rows], ov.targets, live.nav, float(ov.max_capital)
            )
            ctx.update(capital=capital, holdings=rows, idle=live.nav - capital)
        return ctx

    async def index(request: Request) -> Response:
        ov = await run_in_threadpool(overview)
        tg_ok = bool(os.environ.get("TELEGRAM_BOT_TOKEN")) and bool(os.environ.get("TELEGRAM_CHAT_ID"))
        hb_ok = bool(os.environ.get("HEARTBEAT_URL")) and cfg.notify.heartbeat_enabled
        ctx: dict[str, Any] = {
            **_base_ctx(dash),
            "ov": ov,
            "hl": headline(ov),
            "checks": health_checks(ov, telegram=tg_ok, heartbeat=hb_ok),
            "dd_halt": cfg.risk.max_drawdown_halt,
            "chart_data": _chart_data(ov),
            "nav_chart": charts.nav_chart(ov.nav_history),
            "dd_chart": charts.drawdown_chart(ov.nav_history),
            "regime_chart": charts.regime_chart(ov.regime_history),
            "ops_log": list(dash.log),
            "busy": dash.busy.locked(),
            "toast": dash.log[0] if request.query_params.get("op") and dash.log else None,
            "kill_word": KILL_WORD,
            "allow_run": cfg.env is not Env.LIVE,
            "event_summary": event_summary,
            "regime_mode": cfg.regime.mode,
            "sma_period": cfg.regime.sma_period,
            "inline_live": None,
        }
        if request.query_params.get("live") == "1":  # 不开脚本时的后备：在页面里直接渲染实时持仓
            ctx.update(await run_in_threadpool(live_ctx, ov))
            ctx["inline_live"] = True
        return TEMPLATES.TemplateResponse(request, "index.html", ctx)

    async def live_partial(request: Request) -> Response:
        if request.query_params.get("fresh") == "1":  # 页面上的"刷新"按钮：跳过 60 秒缓存
            dash._live = None
        ov = await run_in_threadpool(overview)
        ctx = {**_base_ctx(dash), **await run_in_threadpool(live_ctx, ov)}
        return TEMPLATES.TemplateResponse(request, "_live.html", ctx)

    async def ops_status(request: Request) -> Response:
        return JSONResponse(
            {
                "busy": dash.busy.locked(),
                "log": [
                    {"time": r.time.isoformat(), "op": r.op, "status": r.status, "message": r.message}
                    for r in dash.log
                ],
            }
        )

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
        return RedirectResponse("/?op=1#ops", status_code=303)

    return Starlette(
        routes=[
            Route("/", index),
            Route("/partials/live", live_partial),
            Route("/api/ops", ops_status),
            Route("/ops/{name}", op, methods=["POST"]),
            Mount("/static", app=StaticFiles(directory=STATIC_DIR), name="static"),
        ],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=_auth_middleware(dash.username, dash.password))],
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


def load_login(env: Env) -> tuple[str, str]:
    """从环境变量读取登录用户名与密码；密码不满足长度要求时拒绝启动（D-078）。"""
    username = os.environ.get(USERNAME_ENV, "").strip() or DEFAULT_USERNAME
    pw = os.environ.get(PASSWORD_ENV, "")
    need = MIN_PASSWORD_LEN_LIVE if env is Env.LIVE else MIN_PASSWORD_LEN
    if len(pw) < need:
        raise ConfigError(
            f"请在 .env 中设置 {PASSWORD_ENV}（{env.value} 环境至少 {need} 位），管理界面才能启动"
        )
    return username, pw


def cmd_dashboard(cfg: AppConfig, args: argparse.Namespace) -> int:
    import uvicorn

    from bot.app.ops_commands import build_deps
    from bot.app.run_once import KILL_FILE, LOCK_FILE
    from bot.logging_setup import setup_logging

    load_dotenv(Path(".env"), override=False)
    setup_logging(cfg.logging.level, Path(cfg.logging.dir), cfg.logging.retention_days)
    username, password = load_login(cfg.env)
    dash = Dashboard(
        cfg,
        StateRepo.open(paths.state_db(cfg.env.value)),
        username=username,
        password=password,
        kill_file=KILL_FILE,
        deps_factory=lambda: build_deps(cfg, allow_live=cfg.env is Env.LIVE),
        store=MarketStore(paths.MARKET_DB),
    )
    dash.alive_file = alive_path(LOCK_FILE)
    log.info("管理界面启动：http://%s:%s（%s）", args.host, args.port, cfg.env.value)
    uvicorn.run(create_app(dash), host=args.host, port=args.port, log_level="warning", proxy_headers=False)
    return 0
