"""配置加载与校验（spec §10）。

- `config/config.yaml` → `AppConfig`
- `config/assets.yaml` → `AssetsConfig`（spec §2.2）

所有模型 `extra="forbid"`：配置项拼写错误直接报错（doc/DECISIONS.md D-027）。
金额、价格比例类参数用 Decimal（D-026），策略参数用 float。
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import warnings
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from bot.domain.errors import ConfigError
from bot.domain.types import Env

# spec §13：外部请求只允许发往这些域名
MARKET_DATA_HOSTS = frozenset({"data-api.binance.vision"})
HEARTBEAT_HOSTS = frozenset({"hc-ping.com"})

_STRATEGY_ID_RE = re.compile(r"^[a-z0-9]{1,6}$")
_ACCOUNT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_SLEEVE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,15}$")
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,30}$")
_ASSET_RE = re.compile(r"^[A-Z0-9]{1,20}$")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _to_decimal(v: Any) -> Any:
    # YAML 中的 0.003 会先被解析成 float；经 str 转换避免二进制误差
    if isinstance(v, float):
        return Decimal(str(v))
    return v


Money = Annotated[Decimal, BeforeValidator(_to_decimal)]


def _check_host(url: str, allowed: frozenset[str], field: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in allowed:
        raise ValueError(f"{field} 只允许 https 且域名属于 {sorted(allowed)}（spec §13），实际为 {url!r}")
    return url


def _check_symbols(values: list[str]) -> list[str]:
    for s in values:
        if not _SYMBOL_RE.match(s):
            raise ValueError(f"交易对 {s!r} 格式不正确，应为大写字母或数字，例如 BTCUSDT")
    return values


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DataConfig(_Model):
    market_data_rest: str = "https://data-api.binance.vision"
    history_days: int = Field(800, ge=200)
    price_jump_warn: float = Field(0.6, gt=0)  # spec §3.3 第 3 条：单日收盘变化超过它记 WARN

    @field_validator("market_data_rest")
    @classmethod
    def _host(cls, v: str) -> str:
        return _check_host(v, MARKET_DATA_HOSTS, "data.market_data_rest")


class UniverseConfig(_Model):
    universe_size: int = Field(30, ge=1)
    min_median_quote_volume: float = Field(5_000_000, ge=0)
    volume_median_days: int = Field(30, ge=5)
    min_listing_days: int = Field(90, ge=1)
    segment_gap_days: int = Field(7, ge=1)
    assets_file: Path = Path("config/assets.yaml")


class RegimeConfig(_Model):
    symbol: str = "BTCUSDT"
    sma_period: int = Field(100, ge=2)
    buffer: float = Field(0.02, ge=0, lt=0.5)
    replay_days: int = Field(730, ge=0)
    risk_on_mode: Literal["ramp", "immediate"] = "ramp"
    mode: Literal["sma", "tsmom", "ensemble"] = "sma"  # tsmom：研究对照 E2b；ensemble：研究 R2 多周期集成
    tsmom_lookback: int = Field(90, ge=5)
    ensemble_periods: list[int] = Field(default_factory=lambda: [50, 100, 150, 200], min_length=2)

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v: str) -> str:
        return _check_symbols([v])[0]

    @model_validator(mode="after")
    def _replay(self) -> RegimeConfig:
        if self.replay_days < self.sma_period:
            raise ValueError("regime.replay_days 不能小于 sma_period")
        return self


class _SleeveBase(_Model):
    enabled: bool = True
    capital_weight: float = Field(ge=0, le=1)
    target_annual_vol: float = Field(gt=0, le=1)
    vol_lookback: int = Field(30, ge=5)
    max_gross: float = Field(0.95, gt=0, le=1)
    rebalance_mode: Literal["smoothed", "weekly"] = "smoothed"
    smoothing_days: int = Field(7, ge=1)
    rebalance_weekday: int = Field(0, ge=0, le=6)


class TrendSleeveConfig(_SleeveBase):
    """核心袖子：大盘过滤为 Risk-On 时按波动率目标持有（spec §4.3）。"""

    type: Literal["trend"]
    symbols: list[str] = Field(min_length=1)
    # 单资产趋势过滤（研究 R4）：none = 只跟随大盘过滤；own_sma = 另外要求资产自身对 SMA 的带缓冲状态为 Risk-On
    asset_filter: Literal["none", "own_sma"] = "none"
    asset_sma_period: int = Field(100, ge=2)
    asset_buffer: float = Field(0.02, ge=0, lt=1)

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, v: list[str]) -> list[str]:
        return _check_symbols(v)


class XsMomentumSleeveConfig(_SleeveBase):
    """卫星袖子：横截面动量（spec §4.3）。"""

    type: Literal["xs_momentum"]
    exclude_symbols: list[str] = Field(default_factory=list)
    momentum_lookback: int = Field(28, ge=2)
    momentum_skip: int = Field(0, ge=0)
    score: Literal["ret_over_vol", "ret", "blend", "ctrend"] = "ret_over_vol"
    # 以下三项只在 score: ctrend 时使用（研究 R3，预注册见 doc/research/log.md）
    ctrend_signals: Literal["price_volume", "price"] = "price_volume"
    ctrend_window_weeks: int = Field(52, ge=2)  # 平均最近几个锚点的横截面回归系数
    ctrend_horizon_days: int = Field(7, ge=1)  # 锚点间隔与预测期
    # 山寨季过滤（研究 R8）：标的池等权的 N 日收益跑赢 BTC 时才持有卫星；None 表示不过滤
    alt_season_lookback: int | None = Field(None, ge=2)
    max_lookback_return: float | None = Field(1.5, gt=0)
    coin_sma_period: int = Field(50, ge=2)
    top_n: int = Field(5, ge=1)
    weighting: Literal["equal", "inverse_vol"] = "equal"
    max_weight_per_coin: float = Field(0.25, gt=0, le=1)

    @field_validator("exclude_symbols")
    @classmethod
    def _symbols(cls, v: list[str]) -> list[str]:
        return _check_symbols(v)

    @model_validator(mode="after")
    def _skip(self) -> XsMomentumSleeveConfig:
        if self.momentum_skip >= self.momentum_lookback:
            raise ValueError("momentum_skip 必须小于 momentum_lookback")
        return self


SleeveConfig = Annotated[TrendSleeveConfig | XsMomentumSleeveConfig, Field(discriminator="type")]


class RebalanceConfig(_Model):
    threshold_mode: Literal["absolute", "relative"] = "relative"
    threshold: float = Field(0.02, ge=0, lt=1)
    threshold_rel: float = Field(0.25, ge=0, lt=1)
    min_trade_usdt: Money = Field(Decimal("10"), ge=0)
    run_time_utc: str = "00:10"

    @field_validator("run_time_utc")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM_RE.match(v):
            raise ValueError("run_time_utc 格式应为 HH:MM（UTC）")
        return v


class TrailingStopConfig(_Model):
    enabled: bool = False
    atr_period: int = Field(14, ge=2)
    atr_multiplier: float = Field(2.5, gt=0)
    cooldown_days: int = Field(7, ge=0)


class RiskConfig(_Model):
    trailing_stop: TrailingStopConfig = TrailingStopConfig()
    max_drawdown_halt: float = Field(0.40, gt=0, le=0.9)
    daily_anomaly_limit: float = Field(0.15, gt=0, lt=1)
    data_sanity_max_deviation: float = Field(0.20, gt=0, lt=1)
    force_exit_assets: list[str] = Field(default_factory=list)

    @field_validator("force_exit_assets")
    @classmethod
    def _assets(cls, v: list[str]) -> list[str]:
        for a in v:
            if not _ASSET_RE.match(a):
                raise ValueError(f"资产 {a!r} 格式不正确，应为大写，例如 SOL")
        return v


class ExecutionConfig(_Model):
    mode: Literal["ioc", "maker_then_ioc"] = "ioc"
    max_slippage: Money = Field(Decimal("0.003"), gt=0, lt=Decimal("0.1"))
    max_attempts: int = Field(2, ge=1, le=5)
    kill_slippage: Money = Field(Decimal("0.01"), gt=0, lt=Decimal("0.2"))
    fee_buffer: Money = Field(Decimal("0.002"), ge=0, lt=Decimal("0.05"))
    http_timeout_sec: float = Field(10, gt=0, le=60)
    recv_window_ms: int = Field(5000, ge=1, le=60000)
    unknown_status_query_delays_sec: list[float] = Field(default_factory=lambda: [1.0, 3.0, 10.0, 30.0, 60.0])

    @field_validator("mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        if v != "ioc":
            raise ValueError("v1 只支持 execution.mode: ioc；maker_then_ioc 计划在 v1.1 实现（D-004、D-028）")
        return v

    @field_validator("unknown_status_query_delays_sec")
    @classmethod
    def _delays(cls, v: list[float]) -> list[float]:
        if not v or any(d <= 0 for d in v) or any(b <= a for a, b in itertools.pairwise(v)):
            raise ValueError("unknown_status_query_delays_sec 必须非空、为正且严格递增")
        return v


class BacktestConfig(_Model):
    initial_capital: Money = Field(Decimal("1000"), gt=0)
    fee: float = Field(0.001, ge=0, lt=0.05)
    slippage: float = Field(0.0005, ge=0, lt=0.05)
    delist_haircut: float = Field(0.5, ge=0, le=1)
    # 只用于报告"扣除 USDT 理财收益"的参考夏普（spec §1.2）
    usdt_yield_annual: float = Field(0.04, ge=0, lt=0.5)
    cash_yield_annual: float = Field(
        0.0, ge=0, lt=0.5
    )  # 研究 R7：回测中闲置 USDT 的理财年化收益；0 表示不计息
    start: date = date(2020, 1, 1)
    end: date = date(2026, 10, 31)

    @model_validator(mode="after")
    def _range(self) -> BacktestConfig:
        if self.start >= self.end:
            raise ValueError("backtest.start 必须早于 backtest.end")
        return self


class ResearchConfig(_Model):
    holdout_start: date = date(2026, 11, 1)  # 2026-11-02 起（D-085）；第一段保留集 2025-07-01 已用过
    segment_split: date = date(2025, 7, 1)  # 报告中段 A / 段 B 的分界（D-085：新研究须两段都不劣于当前配置）


class NotifyConfig(_Model):
    telegram_enabled: bool = True  # 只发通知，不接收命令（D-006）
    heartbeat_enabled: bool = True  # 地址放在 .env 的 HEARTBEAT_URL（D-071）
    daily_report: bool = True
    missed_run_hours: float = Field(30, gt=0)  # spec §12 MISSED_RUN：最近一次 DONE 距今超过它时告警

    @model_validator(mode="before")
    @classmethod
    def _heartbeat_url_moved(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and "heartbeat_url" in data:
            raise ValueError(
                "notify.heartbeat_url 已移到 .env 的 HEARTBEAT_URL（D-071）：心跳地址相当于密钥，不能写进公开仓库"
            )
        return data


class BackupConfig(_Model):
    """状态库自动备份（ROADMAP A3）：每次运行正常结束后复制 data/state-<env>.db。"""

    enabled: bool = True
    dir: Path = Path("data/backup")
    keep: int = Field(30, ge=1)  # 每个环境保留最近几份


class LoggingConfig(_Model):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    dir: Path = Path("logs")
    retention_days: int = Field(90, ge=1)


class AppConfig(_Model):
    env: Env = Env.TESTNET
    dry_run: bool = False
    strategy_id: str = "mom1"
    account_id: str = "spot-main"
    max_capital_usdt: Money = Field(Decimal("500"), gt=0)
    data: DataConfig = DataConfig()
    universe: UniverseConfig = UniverseConfig()
    regime: RegimeConfig = RegimeConfig()
    sleeves: dict[str, SleeveConfig]
    rebalance: RebalanceConfig = RebalanceConfig()
    risk: RiskConfig = RiskConfig()
    execution: ExecutionConfig = ExecutionConfig()
    backtest: BacktestConfig = BacktestConfig()
    research: ResearchConfig = ResearchConfig()
    notify: NotifyConfig = NotifyConfig()
    backup: BackupConfig = BackupConfig()
    logging: LoggingConfig = LoggingConfig()

    @field_validator("strategy_id")
    @classmethod
    def _strategy_id(cls, v: str) -> str:
        if not _STRATEGY_ID_RE.match(v):
            raise ValueError("strategy_id 必须是 1~6 位小写字母或数字（会写进 clientOrderId，spec §6.4）")
        return v

    @field_validator("account_id")
    @classmethod
    def _account_id(cls, v: str) -> str:
        if not _ACCOUNT_ID_RE.match(v):
            raise ValueError("account_id 只能包含小写字母、数字、- 和 _，最长 32 位")
        return v

    @field_validator("sleeves")
    @classmethod
    def _sleeve_names(cls, v: dict[str, SleeveConfig]) -> dict[str, SleeveConfig]:
        for name in v:
            if not _SLEEVE_NAME_RE.match(name):
                raise ValueError(f"袖子名 {name!r} 不合法：小写字母开头，只含小写字母、数字、下划线，≤16 位")
        return v

    @model_validator(mode="after")
    def _cross_checks(self) -> AppConfig:
        enabled = [s for s in self.sleeves.values() if s.enabled]
        if not enabled:
            raise ValueError("至少需要启用一个袖子")
        total = sum(s.capital_weight for s in enabled)
        if total > 1 + 1e-9:
            raise ValueError(f"已启用袖子的 capital_weight 之和为 {total:.4f}，不能超过 1（spec §4.5）")
        if self.backtest.end >= self.research.holdout_start:
            raise ValueError(
                f"backtest.end（{self.backtest.end}）必须早于 research.holdout_start"
                f"（{self.research.holdout_start}），保留集只能由 holdout 命令读取（spec §3.6）"
            )
        if self.env is Env.LIVE:
            if "max_capital_usdt" not in self.model_fields_set:
                raise ValueError("env: live 时必须在配置中显式写出 max_capital_usdt（spec §13）")
            if "dry_run" not in self.model_fields_set:
                warnings.warn("env: live 但未显式设置 dry_run，默认 dry_run=false 会真实下单", stacklevel=2)
        return self

    def enabled_sleeves(self) -> dict[str, TrendSleeveConfig | XsMomentumSleeveConfig]:
        return {k: v for k, v in self.sleeves.items() if v.enabled}


class AssetsConfig(_Model):
    """资产审核名单（spec §2.2）。审核结果只由人工编辑生效，程序不得写入 approved。"""

    approved_assets: list[str] = Field(default_factory=list)
    excluded_assets: dict[str, list[str]] = Field(default_factory=dict)
    excluded_patterns: list[str] = Field(default_factory=list)
    pattern_exceptions: list[str] = Field(default_factory=list)

    @field_validator("approved_assets", "pattern_exceptions")
    @classmethod
    def _asset_list(cls, v: list[str]) -> list[str]:
        for a in v:
            if not _ASSET_RE.match(a):
                raise ValueError(f"资产 {a!r} 格式不正确，应为大写字母或数字")
        return v

    @field_validator("excluded_assets")
    @classmethod
    def _excluded(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        for cat, assets in v.items():
            for a in assets:
                if not _ASSET_RE.match(a):
                    raise ValueError(f"excluded_assets.{cat} 中的资产 {a!r} 格式不正确")
        return v

    @field_validator("excluded_patterns")
    @classmethod
    def _patterns(cls, v: list[str]) -> list[str]:
        for p in v:
            try:
                re.compile(p)
            except re.error as e:
                raise ValueError(f"excluded_patterns 中的正则 {p!r} 无效：{e}") from e
        return v

    @model_validator(mode="after")
    def _no_overlap(self) -> AssetsConfig:
        both = set(self.approved_assets) & self.excluded_set()
        if both:
            raise ValueError(f"以下资产同时出现在 approved 和 excluded 中：{sorted(both)}")
        return self

    def excluded_set(self) -> set[str]:
        return {a for assets in self.excluded_assets.values() for a in assets}


def _format_validation_error(path: Path, err: ValidationError) -> str:
    lines = [f"配置文件 {path} 校验失败："]
    for e in err.errors():
        loc = ".".join(str(x) for x in e["loc"]) or "(根)"
        lines.append(f"  - {loc}: {e['msg']}")
    return "\n".join(lines)


def config_hash(cfg: AppConfig) -> str:
    """配置内容的短哈希（试验记录、策略登记、保留集运行记录共用）。"""
    raw = json.dumps(cfg.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode()).hexdigest()[:10]


def _read_yaml(path: Path) -> Mapping[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"找不到配置文件：{path}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"配置文件 {path} 不是合法的 YAML：{e}") from e
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件 {path} 的顶层必须是映射（key: value）")
    return raw


def load_config(path: Path | str) -> AppConfig:
    """读取并校验 config.yaml。相对路径（如 assets_file）相对于项目根目录（运行命令时的工作目录）。"""
    path = Path(path)
    try:
        return AppConfig.model_validate(_read_yaml(path))
    except ValidationError as e:
        raise ConfigError(_format_validation_error(path, e)) from e


def load_assets(path: Path | str) -> AssetsConfig:
    path = Path(path)
    try:
        return AssetsConfig.model_validate(_read_yaml(path))
    except ValidationError as e:
        raise ConfigError(_format_validation_error(path, e)) from e
