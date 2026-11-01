from __future__ import annotations

import logging
from decimal import Decimal

import httpx
import pytest

from bot.domain.errors import ConfigError
from bot.domain.types import Balance, SystemState
from bot.logging_setup import SecretFilter, mask
from bot.notify import heartbeat
from bot.notify.telegram import TelegramNotifier
from bot.risk import guards, ledger
from bot.risk.guards import Action, GuardInput

D = Decimal


def test_nav_counts_only_managed_assets() -> None:
    bal = {
        "USDT": Balance("USDT", D("100"), D("5")),
        "BTC": Balance("BTC", D("0.01"), D(0)),
        "BNB": Balance("BNB", D("1"), D(0)),
    }
    nav = ledger.compute_nav(bal, {"BTC": D("50000"), "BNB": D("600")}, managed_assets={"BTC", "ETH"})
    assert nav.nav == D("605")
    assert nav.positions == {"BTC": D("500")}
    assert nav.gross == D("500") / D("605")


def test_unpriced_managed_asset_reported() -> None:
    bal = {"USDT": Balance("USDT", D("10"), D(0)), "XYZ": Balance("XYZ", D("3"), D(0))}
    assert ledger.compute_nav(bal, {}, {"XYZ"}).unpriced == ("XYZ",)


def test_capital_flows_keep_nav_per_unit() -> None:
    units = ledger.initial_units(D("500"))
    nav = D("600")  # 赚了 20%
    navu = ledger.nav_per_unit(nav, units)
    units2 = ledger.apply_capital_flow(units, nav, D("120"), "DEPOSIT")
    assert ledger.nav_per_unit(nav + D("120"), units2) == navu
    units3 = ledger.apply_capital_flow(units2, nav + D("120"), D("240"), "WITHDRAW")
    assert ledger.nav_per_unit(nav + D("120") - D("240"), units3) == navu
    with pytest.raises(ValueError):
        ledger.apply_capital_flow(units3, D("480"), D("1000"), "WITHDRAW")


def test_unexplained_changes() -> None:
    prev = {"USDT": D("100"), "BTC": D("0.01")}
    cur = {"USDT": D("50.05"), "BTC": D("0.011")}
    explained = {"USDT": D("-50"), "BTC": D("0.001")}  # 本系统的买单
    assert ledger.unexplained_changes(prev, cur, explained, {"BTC": D("50000")}) == {}
    cur2 = {"USDT": D("150"), "BTC": D("0.01")}  # 外部充值 50
    assert set(ledger.unexplained_changes(prev, cur2, {}, {"BTC": D("50000")})) == {"USDT"}


def _g(**kw) -> GuardInput:  # type: ignore[no-untyped-def]
    base = dict(
        kill_requested=False, system_state=SystemState.RUNNING, data_ok=True, unexplained={}, navu=D("1.0"),
        navu_peak=D("1.0"), navu_prev=D("1.0"), hours_since_prev=24.0, price_deviation={}, balances_ok=True,
        max_drawdown_halt=0.40, daily_anomaly_limit=0.15, data_sanity_max_deviation=0.20,
    )  # fmt: skip
    base.update(kw)
    return GuardInput(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kw", "action"),
    [
        ({}, Action.PROCEED),
        ({"kill_requested": True}, Action.KILL),
        ({"system_state": SystemState.PAUSED}, Action.SKIP),
        ({"data_ok": False}, Action.SKIP),
        ({"unexplained": {"USDT": D("50")}}, Action.NO_BUYS),
        ({"navu": D("0.84"), "navu_prev": D("1.0")}, Action.NO_BUYS),  # 单日 −16%
        (
            {"navu": D("0.84"), "navu_prev": D("1.0"), "hours_since_prev": 48.0},
            Action.PROCEED,
        ),  # 漏跑：跳过检查
        ({"navu": D("0.59"), "navu_peak": D("1.0"), "navu_prev": D("0.6")}, Action.HALT),
        (
            {
                "navu": D("0.59"),
                "navu_peak": D("1.0"),
                "navu_prev": D("0.6"),
                "price_deviation": {"BTC": 0.5},
            },
            Action.PAUSE,
        ),
        (
            {"navu": D("0.59"), "navu_peak": D("1.0"), "navu_prev": D("0.6"), "balances_ok": False},
            Action.PAUSE,
        ),
    ],
)
def test_guard_decisions(kw, action) -> None:  # type: ignore[no-untyped-def]
    r = guards.evaluate(_g(**kw))
    assert r.action is action
    assert r.allow_buys == (action is Action.PROCEED)


def test_telegram_disabled_without_token() -> None:
    assert TelegramNotifier(token="", chat_id="").send("hi") is False


def test_telegram_sends_and_never_raises() -> None:
    sent = []

    def ok(req: httpx.Request) -> httpx.Response:
        sent.append(req.url.host)
        return httpx.Response(200, json={"ok": True})

    n = TelegramNotifier(token="123:abc", chat_id="42", transport=httpx.MockTransport(ok))
    assert n.send("日报", "INFO") is True and sent == ["api.telegram.org"]

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=req)

    n2 = TelegramNotifier(token="123:abc", chat_id="42", transport=httpx.MockTransport(boom))
    assert n2.send("x", "CRITICAL") is False


def test_heartbeat_allowlist_and_failures() -> None:
    hits = []

    def ok(req: httpx.Request) -> httpx.Response:
        hits.append(str(req.url))
        return httpx.Response(200)

    t = httpx.MockTransport(ok)
    assert heartbeat.ping("https://hc-ping.com/abc", transport=t) is True
    assert heartbeat.ping("https://evil.example.com/abc", transport=t) is False
    assert heartbeat.ping(None) is False
    assert hits == ["https://hc-ping.com/abc"]


def test_heartbeat_url_from_env() -> None:
    url = "https://hc-ping.com/0f3e5a6c-1111-2222-3333-444455556666"
    assert heartbeat.url_from_env({"HEARTBEAT_URL": url}) == url
    assert heartbeat.url_from_env({"HEARTBEAT_URL": f"  {url}\n"}) == url
    assert heartbeat.url_from_env({"HEARTBEAT_URL": ""}) is None
    assert heartbeat.url_from_env({}) is None
    for bad in ("http://hc-ping.com/abc-secret", "https://example.com/abc-secret"):
        with pytest.raises(ConfigError) as ei:
            heartbeat.url_from_env({"HEARTBEAT_URL": bad})
        assert "abc-secret" not in str(ei.value)  # 错误信息不带地址


def test_secret_filter_scrubs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BINANCE_API_SECRET", "SUPERSECRETVALUE123")
    monkeypatch.setenv("BINANCE_API_KEY", "APIKEY1234567890")
    f = SecretFilter()
    rec = logging.LogRecord(
        "x",
        logging.INFO,
        __file__,
        1,
        "secret=%s key=%s url=/x?signature=abc123 bot12345:%s",
        ("SUPERSECRETVALUE123", "APIKEY1234567890", "A" * 30),
        None,
    )
    f.filter(rec)
    text = rec.getMessage()
    assert "SUPERSECRETVALUE123" not in text and "APIKEY1234567890" not in text
    assert mask("APIKEY1234567890") in text and "signature=****" in text and "bot****" in text


def test_secret_filter_scrubs_heartbeat_url(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://hc-ping.com/0f3e5a6c-1111-2222-3333-444455556666"
    monkeypatch.setenv("HEARTBEAT_URL", url)
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "GET %s", (url,), None)
    SecretFilter().filter(rec)
    assert "0f3e5a6c" not in rec.getMessage()
