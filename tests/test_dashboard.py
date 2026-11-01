"""管理界面（D-075）：登录、CSRF、各项操作的确认与执行、页面内容。不访问网络。"""

from __future__ import annotations

import base64
import threading
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from bot.app.dashboard import server
from bot.app.dashboard.server import Dashboard, create_app, load_password
from bot.app.run_once import DailyRunner
from bot.backtest.sim_env import build_sim_deps, sim_config
from bot.domain.errors import ConfigError
from bot.domain.types import Side, SystemState

PASSWORD = "correct-horse-battery"
ORIGIN = {"origin": "http://testserver"}


def _auth(pw: str = PASSWORD) -> dict[str, str]:
    return {"authorization": "Basic " + base64.b64encode(f"admin:{pw}".encode()).decode()}


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
    assert anon.post("/ops/pause", data={"csrf": dash.csrf}, headers=ORIGIN).status_code == 401
    assert anon.get("/", headers={"authorization": "Basic !!!"}).status_code == 401


def test_page_shows_status_charts_and_live_positions(env) -> None:  # type: ignore[no-untyped-def]
    *_, client = env
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "RUNNING" in html and "mom1:2026-11-02" in html
    assert html.count("<svg") >= 1 and "单位净值与回撤" in html
    assert "实时持仓" in html and "BTC" in html and "实时余额获取失败" not in html
    assert (
        r.headers["x-frame-options"] == "DENY"
        and "frame-ancestors 'none'" in r.headers["content-security-policy"]
    )
    assert "<script" not in html  # 页面不含任何脚本


def test_live_failure_still_renders_page(env) -> None:  # type: ignore[no-untyped-def]
    dash, *_ = env
    dash._deps_factory = lambda: (_ for _ in ()).throw(RuntimeError("no key"))  # type: ignore[assignment]
    dash._deps = None
    html = TestClient(create_app(dash), headers=_auth()).get("/").text
    assert "实时余额获取失败：RuntimeError: no key" in html
    assert "状态库记录的受管持仓" in html


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
    assert r.status_code == 303 and r.headers["location"] == "/#ops"
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


def test_password_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_PASSWORD", "short")
    with pytest.raises(ConfigError):
        load_password()
    monkeypatch.delenv("DASHBOARD_PASSWORD")
    with pytest.raises(ConfigError):
        load_password()
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    assert load_password() == PASSWORD


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
