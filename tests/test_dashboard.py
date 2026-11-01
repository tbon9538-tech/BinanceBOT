"""管理界面（D-075）：登录、CSRF、各项操作的确认与执行、页面内容。不访问网络。"""

from __future__ import annotations

import base64
import json
import re
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from bot.app.dashboard import server
from bot.app.dashboard.server import Dashboard, create_app, load_login
from bot.app.run_once import DailyRunner
from bot.backtest.sim_env import build_sim_deps, sim_config
from bot.domain.errors import ConfigError
from bot.domain.types import Env, Side, SystemState

PASSWORD = "correct-horse-battery"
ORIGIN = {"origin": "http://testserver"}


def _auth(pw: str = PASSWORD, user: str = "admin") -> dict[str, str]:
    return {"authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


@pytest.fixture
def env(tmp_path: Path, base_cfg: dict[str, Any], monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    async def no_wait(_: float) -> None:
        return None

    monkeypatch.setattr(server.asyncio, "sleep", no_wait)  # 登录失败的 1 秒等待
    from datetime import date

    deps, acct, _, clock = build_sim_deps(sim_config(base_cfg), tmp_path, date(2026, 11, 2))
    assert DailyRunner(deps).run_once().status == "DONE"
    dash = Dashboard(
        deps.cfg,
        deps.repo,
        password=PASSWORD,
        kill_file=deps.kill_file,
        deps_factory=lambda: deps,
        store=deps.store,
        now=clock.now,
    )
    client = TestClient(create_app(dash), headers=_auth())
    return dash, deps, acct, client


def _post(client: TestClient, dash: Dashboard, name: str, **form: str) -> Any:
    return client.post(
        f"/ops/{name}", data={"csrf": dash.csrf, **form}, headers=ORIGIN, follow_redirects=False
    )


def _wait_background(dash: Dashboard) -> None:
    for t in threading.enumerate():
        if t.name.startswith("dashboard-"):
            t.join(timeout=30)
    assert not dash.busy.locked()


def test_login_required(env) -> None:  # type: ignore[no-untyped-def]
    dash, *_ = env
    anon = TestClient(create_app(dash))
    r = anon.get("/")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
    assert anon.get("/", headers=_auth("wrong-password-123")).status_code == 401
    assert anon.get("/", headers=_auth(user="root")).status_code == 401  # 密码对、用户名错
    assert anon.get("/", headers=_auth()).status_code == 200
    assert anon.post("/ops/pause", data={"csrf": dash.csrf}, headers=ORIGIN).status_code == 401
    assert anon.get("/", headers={"authorization": "Basic !!!"}).status_code == 401


def test_page_shows_status_and_charts(env) -> None:  # type: ignore[no-untyped-def]
    *_, client = env
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    # 测试环境没有常驻进程的存活标记：页首说明定时运行未进行，检查项标出常驻进程
    assert "定时运行未进行" in html and "mom1:2026-11-02" in html and "单位净值" in html
    assert 'class="check bad"' in html and "仓位 · 占策略资金" in html
    csp = r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in csp
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
    # 只有同源脚本文件和不执行的 JSON 数据块；没有内联脚本，也没有外部资源（D-080）
    scripts = re.findall(r"<script([^>]*)>", html)
    assert sorted(scripts) == sorted(
        [' src="/static/app.js" defer', ' type="application/json" id="chart-data"']
    )
    assert "http://" not in html.replace("http://testserver", "") and "https://" not in html
    data = json.loads(re.search(r'id="chart-data">(.*?)</script>', html, re.S).group(1))  # type: ignore[union-attr]
    assert len(data["regime"]) == 365 and data["nav"][-1][0] == "2026-11-02"
    assert 'id="live-slot" data-src="/partials/live"' in html  # 实时持仓由脚本异步加载


def test_live_partial_compares_target_and_actual(env) -> None:  # type: ignore[no-untyped-def]
    *_, client = env
    html = client.get("/partials/live").text
    assert "BTC" in html and "目标" in html and "策略资金 = min(净值, 上限)" in html
    assert "暂时读不到交易所余额" not in html
    inline = client.get("/?live=1").text  # 不开脚本时的后备
    assert 'data-loaded="1"' in inline and "策略资金 = min(净值, 上限)" in inline


def test_compare_holdings_uses_strategy_capital() -> None:
    from bot.app.dashboard.data import compare_holdings

    capital, rows = compare_holdings(
        [("BTC", 0.003, 236.0), ("PEPE", 8e6, 41.5)],
        {"core": {"BTCUSDT": 0.475}, "satellite": {"PEPEUSDT": 0.084, "TRXUSDT": 0.017}},
        nav=5000.0,
        max_capital=500.0,
    )
    assert capital == 500.0
    by = {r.asset: r for r in rows}
    assert by["BTC"].actual == pytest.approx(0.472) and by["BTC"].target == 0.475
    assert by["TRX"].actual == 0 and by["TRX"].target == 0.017  # 有目标、还没持有
    assert [r.asset for r in rows] == ["BTC", "PEPE", "TRX"]


def test_live_failure_still_renders(env) -> None:  # type: ignore[no-untyped-def]
    dash, *_ = env
    dash._deps_factory = lambda: (_ for _ in ()).throw(RuntimeError("no key"))  # type: ignore[assignment]
    dash._deps = None
    client = TestClient(create_app(dash), headers=_auth())
    assert client.get("/").status_code == 200  # 主页面不等交易所
    html = client.get("/partials/live").text
    assert "暂时读不到交易所余额" in html and "RuntimeError: no key" in html
    assert "状态库记录的受管持仓" in html


def test_static_files_need_login(env) -> None:  # type: ignore[no-untyped-def]
    dash, *_, client = env
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200
    assert TestClient(create_app(dash)).get("/static/app.js").status_code == 401


def test_daemon_status_from_alive_marker(env, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    dash, *_, client = env
    assert "定时运行没有在进行" in client.get("/").text  # 没有存活标记
    alive = tmp_path / "daemon.alive"
    now = dash.now()
    alive.write_text(
        json.dumps({"seen": now.isoformat(), "state": "waiting", "next_run": "2026-11-03T00:10:00+00:00"})
    )
    dash.alive_file = alive
    html = client.get("/").text
    assert "定时运行没有在进行" not in html and "等待中" in html and "11-03 08:10" in html
    stale = now - timedelta(minutes=10)
    alive.write_text(json.dumps({"seen": stale.isoformat(), "state": "waiting", "next_run": now.isoformat()}))
    assert "定时运行没有在进行" in client.get("/").text


def test_operations_need_csrf_and_same_origin(env) -> None:  # type: ignore[no-untyped-def]
    dash, deps, _, client = env
    assert client.post("/ops/pause", data={}, headers=ORIGIN).status_code == 403
    assert client.post("/ops/pause", data={"csrf": "x"}, headers=ORIGIN).status_code == 403
    evil = {"origin": "https://evil.example.com"}
    assert client.post("/ops/pause", data={"csrf": dash.csrf}, headers=evil).status_code == 403
    assert deps.repo.get_system_state("mom1").state == SystemState.RUNNING.value  # type: ignore[union-attr]
    assert _post(client, dash, "nope").status_code == 404


def test_pause_resume_are_audited(env) -> None:  # type: ignore[no-untyped-def]
    dash, deps, _, client = env
    r = _post(client, dash, "pause")
    assert r.status_code == 303 and r.headers["location"] == "/?op=1#ops"
    page = client.get("/?op=1").text
    assert 'id="toast"' in page and "已暂停" in page  # 操作结果提示
    assert 'id="toast"' not in client.get("/").text
    status = client.get("/api/ops").json()
    assert status["busy"] is False and status["log"][0]["op"] == "暂停"
    assert deps.repo.get_system_state("mom1").state == SystemState.PAUSED.value  # type: ignore[union-attr]
    _post(client, dash, "resume")
    st = deps.repo.get_system_state("mom1")
    assert st is not None and st.state == SystemState.RUNNING.value and "管理界面" in (st.reason or "")
    ops_events = deps.repo.list_events("mom1", type_="DASHBOARD_OP")
    assert [e.payload_json.count("完成") for e in ops_events] == [1, 1]


def test_kill_needs_typed_word_then_liquidates(env) -> None:  # type: ignore[no-untyped-def]
    dash, deps, acct, client = env
    _post(client, dash, "kill", confirm_text="kill")
    assert dash.log[0].status == "拒绝" and not deps.kill_file.exists()
    assert deps.repo.get_system_state("mom1").state == SystemState.RUNNING.value  # type: ignore[union-attr]
    _post(client, dash, "kill", confirm_text="KILL")
    _wait_background(dash)
    assert dash.log[0].op == "紧急停止" and dash.log[0].status == "完成", dash.log[0].message
    assert deps.repo.get_system_state("mom1").state == SystemState.KILLED.value  # type: ignore[union-attr]
    assert any(o.side is Side.SELL and "-K" in o.client_order_id for o in acct.placed)
    # KILLED 之后恢复需要另外确认
    _post(client, dash, "resume")
    assert dash.log[0].status == "拒绝" and "KILLED" in dash.log[0].message
    _post(client, dash, "resume", after_kill="1")
    assert deps.repo.get_system_state("mom1").state == SystemState.RUNNING.value  # type: ignore[union-attr]


def test_run_and_ack_need_confirmation(env) -> None:  # type: ignore[no-untyped-def]
    dash, deps, acct, client = env
    n = len(acct.placed)
    _post(client, dash, "run")
    assert dash.log[0].status == "拒绝"
    _post(client, dash, "run", confirm="1")
    _wait_background(dash)
    assert dash.log[0].status == "完成" and "mom1:2026-11-02：DONE" in dash.log[0].message
    assert len(acct.placed) == n  # 今天已调过仓：只更新快照
    _post(client, dash, "ack", confirm="1", note="testnet 重置")
    _wait_background(dash)
    assert dash.log[0].status == "完成"
    assert deps.repo.latest_nav_snapshot("mom1", "spot-main").kind == "ACK"  # type: ignore[union-attr]


def test_unkill_removes_pending_kill_file(env) -> None:  # type: ignore[no-untyped-def]
    dash, deps, _, client = env
    deps.kill_file.write_text("x")
    assert "KILL 文件存在" in client.get("/").text
    _post(client, dash, "unkill", confirm="1")
    assert not deps.kill_file.exists()


def test_only_one_background_operation_at_a_time(env) -> None:  # type: ignore[no-untyped-def]
    dash, *_ = env
    release = threading.Event()
    t = dash.run_background("慢操作", lambda: "ok" if release.wait(5) else "timeout")
    assert t is not None and dash.run_background("第二个", lambda: "x") is None
    assert dash.log[0].op == "第二个" and dash.log[0].status == "拒绝"
    release.set()
    t.join(5)
    assert [r.status for r in dash.log if r.op == "慢操作"] == ["完成"]


def test_login_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DASHBOARD_USERNAME", raising=False)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "short12")  # 7 位
    with pytest.raises(ConfigError):
        load_login(Env.DEMO)
    monkeypatch.delenv("DASHBOARD_PASSWORD")
    with pytest.raises(ConfigError):
        load_login(Env.DEMO)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "8chars!!")
    assert load_login(Env.DEMO) == ("admin", "8chars!!")  # 默认用户名 admin；demo / testnet 至少 8 位
    with pytest.raises(ConfigError, match="至少 12 位"):
        load_login(Env.LIVE)  # live 至少 12 位
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    monkeypatch.setenv("DASHBOARD_USERNAME", " ops ")
    assert load_login(Env.LIVE) == ("ops", PASSWORD)


def test_regime_chart_uses_market_history(env) -> None:  # type: ignore[no-untyped-def]
    from bot.app.dashboard.data import build_overview

    dash, deps, _, _ = env
    ov = build_overview(deps.cfg, deps.repo, deps.kill_file, store=deps.store, now=dash.now())
    assert len(ov.regime_history) == 365  # 合成行情 420 天：取最近一年
    assert ov.regime is not None and ov.regime.day == ov.regime_history[-1].day
    assert all(p.sma is not None for p in ov.regime_history[-200:])
    # 没有行情库时退回状态库里运行时写入的记录
    fallback = build_overview(deps.cfg, deps.repo, deps.kill_file, now=dash.now())
    assert 0 < len(fallback.regime_history) < 365


def test_headline_puts_the_most_urgent_state_first(env, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    from dataclasses import replace

    from bot.app.dashboard.data import DaemonStatus, build_overview, headline, health_checks

    dash, deps, *_ = env
    now = dash.now()
    alive = DaemonStatus(True, "waiting", now - timedelta(seconds=40), now + timedelta(hours=3))
    ov = replace(build_overview(deps.cfg, deps.repo, deps.kill_file, now=now), daemon=alive, stale=False)
    assert headline(ov).tone == "good" and headline(ov).title == "运行正常" and "北京" in headline(ov).sub
    assert headline(replace(ov, stale=True)).title == "需要检查"
    assert headline(replace(ov, state="PAUSED", stale=True)).title == "已暂停交易"
    assert headline(replace(ov, state="PAUSED", kill_file=True)).title == "等待紧急停止"
    assert headline(replace(ov, state="KILLED", kill_file=True)).tone == "critical"

    checks = {c.label: c for c in health_checks(ov, telegram=False, heartbeat=True)}
    assert checks["常驻进程"].level == "ok" and "40 秒前" in checks["常驻进程"].value
    assert checks["Telegram 通知"].level == "off" and checks["外部心跳"].level == "ok"
    stale = {c.label: c for c in health_checks(replace(ov, stale=True), telegram=True, heartbeat=True)}
    assert stale["最近运行"].level == "bad" and "26 小时" in stale["最近运行"].value


def test_exposure_is_measured_against_strategy_capital(env) -> None:  # type: ignore[no-untyped-def]
    from dataclasses import replace
    from decimal import Decimal

    from bot.app.dashboard.data import build_overview

    dash, deps, *_ = env
    ov = build_overview(deps.cfg, deps.repo, deps.kill_file, now=dash.now())
    # 净值 5000、上限 500、持币占净值 6%：持币 300，占策略资金 60%
    ov = replace(ov, nav=Decimal(5000), gross=Decimal("0.06"), max_capital=Decimal(500))
    assert ov.capital == 500 and ov.held_value == pytest.approx(300)
    assert ov.exposure_of_capital == pytest.approx(0.6)
    assert replace(ov, nav=None).exposure_of_capital is None
