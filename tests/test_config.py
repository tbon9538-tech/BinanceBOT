from __future__ import annotations

import warnings
from decimal import Decimal

import pytest

from bot.config import AssetsConfig, load_assets, load_config
from bot.domain.errors import ConfigError
from bot.domain.types import Env
from tests.conftest import ASSETS_PATH, CONFIG_PATH


def test_shipped_config_loads() -> None:
    cfg = load_config(CONFIG_PATH)
    assert cfg.env is Env.TESTNET
    assert set(cfg.enabled_sleeves()) == {"core", "satellite"}
    assert cfg.sleeves["core"].type == "trend"
    assert cfg.sleeves["satellite"].type == "xs_momentum"
    assert cfg.rebalance.threshold_mode == "relative"
    assert cfg.rebalance.threshold_rel == 0.25
    assert cfg.risk.trailing_stop.enabled is False
    assert cfg.notify.heartbeat_url is None


def test_shipped_assets_load() -> None:
    assets = load_assets(ASSETS_PATH)
    assert "USDC" in assets.excluded_set()
    assert "ETH" in assets.approved_assets and "NVDAB" not in assets.approved_assets


def test_money_fields_are_exact_decimals() -> None:
    cfg = load_config(CONFIG_PATH)
    assert cfg.execution.max_slippage == Decimal("0.003")
    assert cfg.execution.fee_buffer == Decimal("0.002")
    assert cfg.max_capital_usdt == Decimal("500")
    assert isinstance(cfg.rebalance.min_trade_usdt, Decimal)


def _expect_error(write_yaml, data, fragment: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ConfigError) as ei:
        load_config(write_yaml(data))
    assert fragment in str(ei.value)


def test_capital_weights_sum_must_not_exceed_one(base_cfg, write_yaml) -> None:  # type: ignore[no-untyped-def]
    base_cfg["sleeves"]["core"]["capital_weight"] = 0.6
    _expect_error(write_yaml, base_cfg, "capital_weight 之和")


def test_disabled_sleeve_not_counted(base_cfg, write_yaml) -> None:  # type: ignore[no-untyped-def]
    base_cfg["sleeves"]["core"]["capital_weight"] = 0.9
    base_cfg["sleeves"]["satellite"]["enabled"] = False
    cfg = load_config(write_yaml(base_cfg))
    assert list(cfg.enabled_sleeves()) == ["core"]


def test_at_least_one_enabled_sleeve(base_cfg, write_yaml) -> None:  # type: ignore[no-untyped-def]
    for s in base_cfg["sleeves"].values():
        s["enabled"] = False
    _expect_error(write_yaml, base_cfg, "至少需要启用一个袖子")


@pytest.mark.parametrize(
    ("field", "value"),
    [("max_gross", 1.01), ("target_annual_vol", 0), ("target_annual_vol", 1.5)],
)
def test_sleeve_bounds(base_cfg, write_yaml, field, value) -> None:  # type: ignore[no-untyped-def]
    base_cfg["sleeves"]["satellite"][field] = value
    _expect_error(write_yaml, base_cfg, f"sleeves.satellite.xs_momentum.{field}")


@pytest.mark.parametrize("sid", ["MOM1", "toolong7", "a-b", ""])
def test_strategy_id_pattern(base_cfg, write_yaml, sid) -> None:  # type: ignore[no-untyped-def]
    base_cfg["strategy_id"] = sid
    _expect_error(write_yaml, base_cfg, "strategy_id")


def test_unknown_field_rejected(base_cfg, write_yaml) -> None:  # type: ignore[no-untyped-def]
    base_cfg["rebalance"]["treshold"] = 0.02  # 拼写错误
    _expect_error(write_yaml, base_cfg, "rebalance.treshold")


def test_unknown_sleeve_type_rejected(base_cfg, write_yaml) -> None:  # type: ignore[no-untyped-def]
    base_cfg["sleeves"]["core"]["type"] = "carry"
    _expect_error(write_yaml, base_cfg, "sleeves.core")


def test_maker_then_ioc_rejected_in_v1(base_cfg, write_yaml) -> None:  # type: ignore[no-untyped-def]
    base_cfg["execution"]["mode"] = "maker_then_ioc"
    _expect_error(write_yaml, base_cfg, "v1.1")


def test_threshold_mode_values(base_cfg, write_yaml) -> None:  # type: ignore[no-untyped-def]
    base_cfg["rebalance"]["threshold_mode"] = "absolute"
    assert load_config(write_yaml(base_cfg)).rebalance.threshold_mode == "absolute"
    base_cfg["rebalance"]["threshold_mode"] = "percent"
    _expect_error(write_yaml, base_cfg, "rebalance.threshold_mode")


def test_backtest_end_must_precede_holdout(base_cfg, write_yaml) -> None:  # type: ignore[no-untyped-def]
    base_cfg["backtest"]["end"] = "2025-07-01"
    _expect_error(write_yaml, base_cfg, "holdout_start")


def test_backtest_start_before_end(base_cfg, write_yaml) -> None:  # type: ignore[no-untyped-def]
    base_cfg["backtest"]["start"] = "2025-06-30"
    _expect_error(write_yaml, base_cfg, "backtest.start")


def test_momentum_skip_less_than_lookback(base_cfg, write_yaml) -> None:  # type: ignore[no-untyped-def]
    base_cfg["sleeves"]["satellite"]["momentum_skip"] = 28
    _expect_error(write_yaml, base_cfg, "momentum_skip")


def test_max_lookback_return_can_be_disabled(base_cfg, write_yaml) -> None:  # type: ignore[no-untyped-def]
    base_cfg["sleeves"]["satellite"]["max_lookback_return"] = None
    sat = load_config(write_yaml(base_cfg)).sleeves["satellite"]
    assert sat.type == "xs_momentum"
    assert sat.max_lookback_return is None


def test_unknown_status_delays_must_increase(base_cfg, write_yaml) -> None:  # type: ignore[no-untyped-def]
    base_cfg["execution"]["unknown_status_query_delays_sec"] = [1, 10, 3]
    _expect_error(write_yaml, base_cfg, "严格递增")


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://hc-ping.com/0f3e5a6c-1111-2222-3333-444455556666", True),
        ("http://hc-ping.com/abc", False),
        ("https://example.com/ping", False),
    ],
)
def test_heartbeat_url_allowlist(base_cfg, write_yaml, url, ok) -> None:  # type: ignore[no-untyped-def]
    base_cfg["notify"]["heartbeat_url"] = url
    if ok:
        assert load_config(write_yaml(base_cfg)).notify.heartbeat_url == url
    else:
        _expect_error(write_yaml, base_cfg, "spec §13")


def test_market_data_host_allowlist(base_cfg, write_yaml) -> None:  # type: ignore[no-untyped-def]
    base_cfg["data"]["market_data_rest"] = "https://api.example.com"
    _expect_error(write_yaml, base_cfg, "data.market_data_rest")


def test_live_requires_explicit_max_capital(base_cfg, write_yaml) -> None:  # type: ignore[no-untyped-def]
    base_cfg["env"] = "live"
    del base_cfg["max_capital_usdt"]
    _expect_error(write_yaml, base_cfg, "max_capital_usdt")


def test_live_without_explicit_dry_run_warns(base_cfg, write_yaml) -> None:  # type: ignore[no-untyped-def]
    base_cfg["env"] = "live"
    del base_cfg["dry_run"]
    with pytest.warns(UserWarning, match="dry_run"):
        load_config(write_yaml(base_cfg))


def test_live_fully_specified_no_warning(base_cfg, write_yaml) -> None:  # type: ignore[no-untyped-def]
    base_cfg["env"] = "live"
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert load_config(write_yaml(base_cfg)).env is Env.LIVE


def test_missing_file_and_bad_yaml(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ConfigError, match="找不到配置文件"):
        load_config(tmp_path / "nope.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("a: [1, 2\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="不是合法的 YAML"):
        load_config(bad)


def test_assets_overlap_rejected() -> None:
    with pytest.raises(ValueError, match="同时出现在 approved 和 excluded"):
        AssetsConfig(approved_assets=["USDC"], excluded_assets={"stable": ["USDC"]})


def test_assets_bad_regex_and_case(write_yaml) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ConfigError, match="excluded_patterns"):
        load_assets(write_yaml({"excluded_patterns": ["(UP"]}, "assets.yaml"))
    with pytest.raises(ConfigError, match="approved_assets"):
        load_assets(write_yaml({"approved_assets": ["eth"]}, "assets.yaml"))
