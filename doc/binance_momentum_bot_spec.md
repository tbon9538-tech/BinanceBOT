# 币安现货趋势–动量轮动机器人：开发需求说明 v3

> **给 Claude Code 的说明（必须先读）**
>
> 1. 按本文档实现一个完整、可运行的 Python 项目。所有策略参数放在 `config/config.yaml`，资产审核名单放在 `config/assets.yaml`，**代码中不得硬编码参数**。
> 2. 按 `doc/dev_plan.md` 的里程碑（M0～M7）顺序开发。每个里程碑结束时：`pytest` 全部通过 → 提交一次 git commit → 用中文简要汇报完成内容、偏离文档之处和遇到的问题。
> 3. **`dev_plan.md` 中标为"检查点"的地方必须停下来**，等用户确认后再继续。尤其是：M2 研究结束后（确认参数）、保留集检验之后（确认是否进入 testnet）、首次 demo 连续运行之前。
> 4. 开发与测试**只允许使用 `testnet` 或 `demo` 环境**。不得以 `env: live` 运行任何会下单的代码，也不要向用户索要 live API Key。
> 5. 标为 **【待研究 Exx】** 的参数，先按默认值实现成可配置项。参数的取值由 `strategy_research.md` 中对应编号的实验决定，**开发者不得自行调参**。
> 6. 本文档没有覆盖的细节，选择更保守、更安全的实现，并记录到 `doc/DECISIONS.md`（格式：日期、问题、选择、理由）。所有文档都放在 `doc/` 下，目录约定见 §11.1。
> 7. 在项目根目录维护 `CLAUDE.md`，记录项目结构、常用命令（测试、回测、运行）以及本文档的关键约束，便于后续会话继续开发。
> 8. **保留集纪律**：数据层必须拦截研究命令读取 `research.holdout_start`（2025-07-01）及之后的 K 线，见 §3.6。
>
> 术语：**袖子（sleeve）**指组合中一个独立计算目标权重的子策略；**t 日**指最近一根**已收盘**日线的日期（UTC）；**运行日**指程序实际执行的 UTC 日期，通常为 t+1。

---

## 0. 项目概览

- **交易所**：Binance 现货（Spot），只做多，不使用杠杆、合约和理财。
- **策略**：**核心–卫星双袖子**，两个袖子共用 BTC 大盘过滤（`open_questions.md` Q0，2026-11-01 已确认）。
  - **核心袖子**：持有 BTC，采用趋势跟踪和波动率目标。
  - **卫星袖子**：在流动性靠前的山寨币中做横截面动量轮动，同样采用波动率目标。
  - 两个袖子各自的目标都做 7 日平滑（分批调仓）。
  - 选择这个结构的理由见 `strategy_review.md` 和 `strategy_research.md` §4。
- **计价货币**：USDT。
- **运行方式**：每天执行一次幂等的 `run --once`，由外部调度（cron、容器内循环或 Windows 任务计划）触发。v1 **不使用** WebSocket，也不使用 User Data Stream。
- **环境**（通过配置切换，同一套代码；地址于 2026-11-01 核实，见 `strategy_review.md` §7）：

  | env | 交易与账户 REST | 用途 |
  |---|---|---|
  | `testnet` | `https://testnet.binance.vision` | 验证下单流程、状态机、故障恢复。约每月重置一次余额，没有 `/sapi` |
  | `demo` | `https://demo-api.binance.com` | 接近真实行情的模拟运行。限频和过滤器与 live 一致 |
  | `live` | `https://api.binance.com` | 实盘。**开发阶段禁止使用** |

  | 用途 | 地址 | 说明 |
  |---|---|---|
  | 信号行情（所有环境共用） | `https://data-api.binance.vision` | live 只读公开行情，不需要密钥 |
  | 历史数据（回测） | `https://data.binance.vision` | 含已下架交易对 |
  | 预留：WebSocket API | testnet `wss://ws-api.testnet.binance.vision/ws-api/v3`；demo `wss://demo-ws-api.binance.com/ws-api/v3`；live `wss://ws-api.binance.com:443/ws-api/v3` | v1 不使用 |

- **技术栈**：Python 3.11+、`httpx`（同步客户端即可）、`pandas`、`numpy`、`pyyaml`、`pydantic` v2（配置校验）、`python-dotenv`、`SQLAlchemy` 2.x + SQLite、`cryptography`（Ed25519 签名）、`matplotlib`、`pytest`。
- **不使用** `binance-connector`（官方已标为 deprecated），也不使用 `binance-sdk-spot`，改为自研轻量客户端（见 §6.1；`open_questions.md` Q4，2026-11-01 已确认）。不使用 Freqtrade。

---

## 1. 目标、评估口径与基准

### 1.1 目标（`open_questions.md` Q1，2026-11-01 已确认选项 B）

采用"风险为硬约束、收益为期望"：

- **硬约束**（完整牛熊周期回测，含保留集）：
  - 最大回撤 ≤ 35%（目标 30%）；
  - Sharpe ≥ 0.8；
  - Calmar ≥ 0.6；
  - 熊市年份（BTC 年跌幅超过 30% 的年份）的亏损 ≤ BTC 同期亏损的 1/3。
- **期望**：年化 15%～35%。

### 1.2 指标口径（回测、日报、研究报告统一使用）

| 指标 | 定义 |
|---|---|
| 日收益 | 单位净值的日简单收益：`NAVu_t / NAVu_{t-1} − 1`。实盘取每日 PRE_TRADE 快照，回测取每日收盘估值 |
| CAGR | `(NAVu_end / NAVu_start)^(365 / 天数) − 1` |
| 年化波动 | `std(日收益, ddof=1) × √365` |
| Sharpe | `mean(日收益) / std(日收益) × √365`，无风险利率取 0。另外报告"扣除 USDT 理财收益"的版本作为参考 |
| 最大回撤 | `min(NAVu_t / max_{s≤t} NAVu_s − 1)` |
| Calmar | `CAGR / |最大回撤|` |
| 换手率 | `Σ|成交额| / 平均 NAV`，年化 |
| 平均仓位 | 每日 `Σ 持仓市值 / NAV` 的平均值 |

### 1.3 基准（回测报告必须包含）

1. BTC 买入持有。
2. BTC 定投：每月 1 日等额投入，与策略按相同现金流比较终值和 IRR。
3. **BTC 趋势**：使用与本策略相同的大盘过滤，Risk-On 时满仓 BTC，Risk-Off 时持有 USDT，成本相同。
4. 成交额前 10 等权持有，每月 1 日再平衡。

---

## 2. 交易标的池（仅用于卫星袖子）

### 2.1 候选交易对

每次运行时，从 **live** 的 `GET https://data-api.binance.vision/api/v3/exchangeInfo`（weight 20）中选出同时满足下列条件的交易对：

- `quoteAsset == "USDT"`
- `status == "TRADING"`。其他状态（`BREAK`、`HALT`、`END_OF_DAY`、`CANCEL_ONLY`）一律视为不可交易。
- `isSpotTradingAllowed == true`。不要读取 `permissions` 字段，它已恒为空数组。

**执行环境**（testnet/demo）中也必须存在同名交易对，且其状态为 `TRADING`；否则该币的目标权重转为 USDT，并记录一条 INFO 日志。

### 2.2 资产审核（`config/assets.yaml`）

起因：exchangeInfo 中没有资产类别字段。2026-06 之后，币安上线了约 100 个代币化股票/ETF 交易对，例如 `NVDABUSDT`、`TQQQBUSDT`。

```yaml
# config/assets.yaml
approved_assets: [ETH, SOL, BNB, XRP, DOGE, ADA, ...]   # 人工审核通过，只有这里的资产可以进入卫星标的池
excluded_assets:                                          # 人工确认排除
  stablecoins_fiat: [USDC, FDUSD, TUSD, USDP, DAI, BUSD, USD1, USDE, BFUSD, XUSD, RLUSD, EUR, EURI, AEUR, TRY, BRL, ...]
  wrapped_or_pegged: [WBTC, WBETH, BETH, PAXG, XAUT]
  tokenized_stocks_etf: [NVDAB, TSLAB, SPYB, QQQB, TQQQB, SOXLB, SOXSB, UVXYB, ...]
  other: []
excluded_patterns:                                        # 正则，作用于 baseAsset
  - '^[A-Z0-9]+(UP|DOWN|BULL|BEAR)$'                      # 杠杆代币
pattern_exceptions: [JUP]                                  # 命中正则但不是杠杆代币的资产（SUPER 不以 UP 结尾，不会被命中）
```

规则：

1. 只有 `approved_assets` 中的资产可以进入卫星标的池。
2. 一个资产如果通过了 §2.4 的流动性和上市天数筛选，但既不在 approved 也不在 excluded 中（包括被正则命中、又不在例外名单里的情况），就是"待审核"：
   - 写入 `asset_review` 表（状态 `PENDING`）；
   - 发送 `ASSET_REVIEW_NEEDED` 告警，同一资产 7 天内最多发一次；
   - **不参与交易**。
3. `python -m bot assets review` 列出待审核资产，以及辅助判断的信息：首个交易日、30 日成交额中位数、90 日年化波动，以及"疑似稳定币"标记（同时满足"90 日年化波动 < 5%"和"价格与 1 的偏差 < 5%"）。
4. 审核结果只通过人工编辑 `assets.yaml` 生效，**程序不得自动写入 approved**。
5. **回测**：没有历史审核记录，因此用"不在 excluded_assets 中、且不被 excluded_patterns 命中的全部资产"来近似 approved。这一近似须在报告中注明。
6. 初始的 `assets.yaml` 由 M1 任务生成（见 `dev_plan.md`）：先用规则自动排除，再把剩余资产列给用户审核。

### 2.3 连续交易段与上市天数

- 对每个交易对，把日线按时间排序。两根相邻日线的日期间隔超过 `universe.segment_gap_days`（默认 7）天时，切分成新的**交易段**。
- `listing_age(t)` 等于 t 所在交易段从第一根日线到 t 的天数（含首尾两天）。
- 所有指标都只在**同一交易段内**计算，不跨段。
  - 例：`LUNAUSDT` 在 2022-05 之后是另一个资产，切段后自然按新币处理。
- 代币迁移（例如 MATIC→POL）不做拼接，新代码按新上市计算。

### 2.4 流动性排名（每天计算，基于 t 日及之前的数据）

1. 对 §2.1 和 §2.2 筛选后的每个交易对：
   - `medqv(t)` 为最近 `volume_median_days`（30）根日线的 `quote_volume` 中位数；
   - 30 根中有效值少于 25 根时，视为不合格。
2. 合格条件：
   - `listing_age(t) ≥ min_listing_days`（90）；
   - `medqv(t) ≥ min_median_quote_volume`（默认 5,000,000 USDT）【待研究 E1】；
   - 不在卫星袖子的 `exclude_symbols` 中（默认排除 BTCUSDT）；
   - 不在 `risk.force_exit_assets` 中。
3. 按 `medqv(t)` 降序取前 `universe_size`（30）个【待研究 E1】。并列时按 symbol 字母升序。

### 2.5 持仓币出现异常状态

- 如果持仓币在执行环境中的状态变为非 `TRADING`：
  - 发送 `HELD_SYMBOL_NOT_TRADING` 告警（CRITICAL）；
  - 将该持仓标记为 `frozen`，继续按最后价格计入净值；
  - 目标权重强制为 0。状态恢复为 `TRADING` 后自动卖出。
- 人工看到下架公告后，可以把资产填入 `risk.force_exit_assets`，下一次运行时强制卖出。

---

## 3. 数据

### 3.1 来源划分

| 数据 | 来源 |
|---|---|
| 信号用日线 K 线、标的池用的 exchangeInfo | live 公共行情 `data-api.binance.vision`（无密钥） |
| 下单用的过滤器、盘口（bookTicker）、参考价（avgPrice）、余额、订单 | **执行环境**（testnet/demo/live） |
| 净值估值价格 | 执行环境的 `ticker/price` |
| 回测历史 | `data.binance.vision`，再用 REST `klines` 补充最近的数据 |

**禁止**用 live 价格在 testnet/demo 下单或估值，原因见 R-28。

### 3.2 K 线规则

- 使用 1d K 线，UTC 00:00 开盘。REST 调用不传 `timeZone` 参数。
- REST `GET /api/v3/klines`：weight 2，`limit` 最大 1000。**返回结果的最后一根可能还没收盘**：凡是 `closeTime ≥ 服务器当前时间` 的行，一律丢弃。
- `t` 等于 BTCUSDT 最新一根已收盘日线的日期。
  - 运行日 D 开始运行时，如果 BTCUSDT 还拿不到 `D−1` 日的日线，就等 5 分钟再试，最多重试 6 次。
  - 仍然拿不到时，本次运行失败，发送 `DATA_NOT_READY` 告警，**不交易**。
- 本地缓存放在 `data/market.db`（SQLite）的 `klines_1d` 表，主键为 `(symbol, open_time_ms)`。
  - 字段：`open, high, low, close, volume, quote_volume, trades, close_time_ms, source('rest'|'vision'), ingested_at`。
  - 价格和数量用 TEXT 存储十进制字符串；计算时转为 float64（指标计算允许使用浮点）。
- 增量更新：从已存的最后一根日线次日开始拉取。新出现的交易对，首次回补 `data.history_days`（默认 800）天。

### 3.3 数据质量检查（每次运行；不通过时的处理见括号）

1. BTCUSDT 在 t 日必须有日线，并且过去 `regime.sma_period` 天内没有缺失。（否则中止运行，发送告警）
2. 每根日线必须满足 `0 < low ≤ min(open, close) ≤ max(open, close) ≤ high`。（否则该币当天不参与计算，并记录 WARN）
3. 标的池里任何币的 `|close_t / close_{t−1} − 1| > 0.6`：记录 WARN，不中止。

### 3.4 指标计算要求的数据完整性

- 一个币要成为卫星候选，在 `[t − W + 1, t]` 窗口内**不能有任何缺失**，其中 `W = max(momentum_lookback + 1, coin_sma_period, vol_lookback + 1)`。
- 不满足时，这个币当天不参与候选，记录一条 DEBUG 日志。

### 3.5 限频

- 启动时从 `exchangeInfo.rateLimits` 读取限额，不写死。2026-11 实测值：live 为 REQUEST_WEIGHT 6000/分钟；testnet 的 ORDERS 为 50/10 秒。
- 每次响应都读取 `X-MBX-USED-WEIGHT-1M`。已用量超过限额的 80% 时，暂停到下一个整分钟再继续。
- 收到 HTTP 429 或 418 时：**整个进程停止发送请求**，等待 `Retry-After` 秒（没有这个头时等 60 秒）；同时发送 `RATE_LIMITED` 告警。418 视为 CRITICAL，本次运行直接终止。
- `data-api.binance.vision` 和 `api.binance.com` 共享同一个 IP 的权重额度。

### 3.6 保留集守卫

- 配置项 `research.holdout_start: "2025-07-01"`（2026-11-02 起为 `"2026-11-01"`，见 v3 实现阶段修订 #7、D-085）。
- 研究类命令包括 `backtest`、`experiment`、`walkforward`、`bootstrap`。它们读取 K 线时，数据层必须过滤掉 `open_time ≥ holdout_start` 的行。
- 命令的 `--end` 参数大于等于 `holdout_start` 时，直接报错退出。
- 只有 `python -m bot holdout --final --confirm <当日日期>` 可以读取保留集。每次运行都追加记录到 `doc/research/holdout_runs.log`，内容包括时间、git commit、配置哈希和结果摘要。日志里已有记录时，再次运行必须额外加 `--i-know-this-is-not-the-first-run`，并在报告中醒目标注"非首次运行"。
- 实盘命令 `run` 不受这个限制。

### 3.7 时间

- 程序内部一律使用 UTC，日期用 `YYYY-MM-DD` 字符串或 `datetime.date`。
- 签名请求前先用 `GET /api/v3/time` 计算本地时钟偏移，`timestamp` 取本地时间加上偏移。`recvWindow` 默认 5000 ms。

---

## 4. 信号与目标组合

本节所有函数都是**纯函数**：输入为截至 t 日的数据和配置，输出为目标权重。实盘、回测、研究**共用同一份代码**（`bot/strategy/`）。

### 4.1 指标定义

记 `c_t` 为 t 日收盘价，以下计算都在同一交易段内进行：

```
r_t        = c_t / c_{t−1} − 1                        # 日简单收益
mom(L,s)_t = c_{t−s} / c_{t−L} − 1                    # 动量收益（s = skip 天数）
vol(L)_t   = std(r_{t−L+1}, …, r_t; ddof=1)           # 日波动，L 个收益
ann_vol    = vol × √365
SMA(N)_t   = mean(c_{t−N+1}, …, c_t)                  # 需要 N 个收盘价
lbr(L)_t   = c_t / c_{t−L} − 1                        # 回看期总涨幅
TR_t       = max(h_t − l_t, |h_t − c_{t−1}|, |l_t − c_{t−1}|)
ATR(n)_t   = Wilder 平滑：ATR_t = ATR_{t−1} + (TR_t − ATR_{t−1}) / n，
             初值为最早 n 个 TR 的简单平均
```

### 4.2 大盘状态机（BTC Regime）

- 输入：`c_t`（BTCUSDT）和 `m_t = SMA(regime.sma_period)_t`。`sma_period` 默认 100【待研究 E2】，`buffer` 默认 0.02【待研究 E2】。
- 状态：`RISK_ON` / `RISK_OFF`。转移规则：
  - 当前为 `RISK_OFF`，且 `c_t > m_t × (1 + buffer)` → 转为 `RISK_ON`
  - 当前为 `RISK_ON`，且 `c_t < m_t × (1 − buffer)` → 转为 `RISK_OFF`
  - 其余情况保持原状态。`m_t` 不可用时也保持原状态。
- **初始状态（重放）**：
  1. 取本地缓存中 BTCUSDT 最早可算出 SMA 的那一天，从那天起以 `RISK_OFF` 开始，逐日重放到 t。
  2. 要求至少有 `regime.replay_days`（730）天的数据，不足时先回补。
  3. 重放结果与数据库 `regime_state` 中最近一条记录不一致时，以重放结果为准，同时写入一条 `REGIME_REPLAY_MISMATCH` 事件并告警。
- 输出：`regime_t`、`switched_on_t`、`switched_off_t`。每日写入 `regime_state` 表。

### 4.3 每日原始目标 `D_t`（逐个袖子计算）

**核心袖子**（`type: trend`，`symbols: [BTCUSDT]`）：

```
如果 regime_t == RISK_ON：
    σ = ann_vol(vol_lookback=30)_t(BTCUSDT)
    w = min(max_gross, target_annual_vol / σ)     # σ ≤ 0 或无法计算时，w = 0，并记录 ERROR
    D_t = {BTCUSDT: w}
否则：
    D_t = {}
```

`target_annual_vol` 默认 0.30【待研究 E4】，`max_gross` 默认 0.95。

**卫星袖子**（`type: xs_momentum`）：

```
如果 regime_t != RISK_ON：D_t = {}，结束
U = universe(t)                                   # §2.4
对 U 中的每个 symbol（必须满足 §3.4 的完整性）：
    score = mom(L, s)_t / vol(L)_t                # score: ret_over_vol（默认）
            或 mom(L, s)_t                        # score: ret
            或 mean_{L' ∈ {14,28,56}} mom(L',0)_t / vol(L')_t   # score: blend
    候选条件（全部满足）：
        score > 0
        c_t > SMA(coin_sma_period)_t
        max_lookback_return 为 null，或 lbr(L)_t ≤ max_lookback_return
        symbol 不在冷却期（§5.7，仅在止损启用时适用）
按 score 降序排列（并列时按 symbol 升序），取前 top_n 个，记为集合 S，k = |S|
如果 k == 0：D_t = {}，结束
原始相对权重：
    equal：       a_i = 1 / k
    inverse_vol： a_i = (1 / vol(L)_i) / Σ_j (1 / vol(L)_j)
a = cap_weights(a, max_weight_per_coin)           # 见下面的伪代码；结果之和 ≤ 1，余下部分为 USDT
组合日收益 p_τ = Σ_i a_i × r_{i,τ}，τ ∈ [t − vol_lookback + 1, t]
σ_p = std(p; ddof=1) × √365
gross = min(max_gross, target_annual_vol / σ_p)   # σ_p ≤ 0 时，gross = 0，并记录 ERROR
D_t = {i: a_i × gross}
```

默认参数：`L = momentum_lookback = 28`【E3a】，`s = momentum_skip = 0`【E3b】，`score = ret_over_vol`【E3c】，`max_lookback_return = 1.5`【E5】，`coin_sma_period = 50`，`top_n = 5`，`weighting = equal`，`max_weight_per_coin = 0.25`，`target_annual_vol = 0.30`【E4】，`vol_lookback = 30`，`max_gross = 0.95`。

```python
def cap_weights(a: dict[str, float], cap: float, max_iter: int = 20) -> dict[str, float]:
    """迭代封顶（water-filling）：超出上限的部分按比例分给未封顶的币；全部封顶后剩余部分留作 USDT。"""
    w = dict(a)
    for _ in range(max_iter):
        over = {k for k, v in w.items() if v > cap + 1e-12}
        if not over:
            break
        excess = sum(w[k] - cap for k in over)
        for k in over:
            w[k] = cap
        free = {k for k, v in w.items() if v < cap - 1e-12}
        if not free:
            break
        tot = sum(w[k] for k in free)
        for k in free:
            w[k] += excess * w[k] / tot
    return w
```

### 4.4 平滑后的目标 `S_t`（分批调仓，替代 v2 的 tranche 机制）

每个袖子独立计算。`n = smoothing_days`（默认 7）。

```
rebalance_mode == "smoothed"（默认）：
    S_t = (1/n) × Σ_{j=0}^{n−1} F(D_{t−j})
rebalance_mode == "weekly"（仅用于研究对照 E8）：
    S_t = F(D_{t*})，其中 t* 为 ≤ t 且星期几等于 rebalance_weekday（0 = 周一）的最近一天
```

`F(·)` 用于剔除覆盖规则作用下的币：

- `regime_t == RISK_OFF` 时：**S_t = {}**，Risk-Off 当天立即清零。
- 对每个币 i，出现以下任一情况时，在 `F(D_{t−j})` 中把它的权重置为 0（这部分权重变成 USDT，**不**分配给其他币）：
  - 在 `risk.force_exit_assets` 中、被列入黑名单，或者是 `frozen` 持仓；
  - 启用了止损，并且 i 在 `stop_date_i` 触发了止损，同时 `t−j ≤ cooldown_until_i`。效果是：止损当天以及之前算出的目标都不再包含 i；冷却期结束后新算出的目标才可以重新纳入 i（见 R-17）。

说明：

- `D_{t−j}` 只由历史数据和配置决定。缺失的日子（比如程序没运行）可以随时重算，数据库里存储的 `daily_targets` 只用于审计和对账。
- **Risk-On 切换时不做全量重算**【待研究 E2c，`regime.risk_on_mode: ramp`】。Risk-Off 期间的 `D` 都是空集，所以切回 Risk-On 后，仓位会在 n 天内线性建立。
- 如果 E2c 的结论是 `immediate`，切换当天把窗口中的 `D_{t−j}` 全部替换为 `D_t`。
- `n = 1` 等价于每日全量调仓。

### 4.5 组合目标

```
W_t(i) = Σ_{sleeve} capital_weight_sleeve × S_t^sleeve(i)
```

- 默认 `core.capital_weight = 0.5`，`satellite.capital_weight = 0.5`【待研究 E7】。所有启用袖子的 `capital_weight` 之和必须 ≤ 1，配置校验时检查。
- `Σ_i W_t(i) ≤ 0.95`，其余部分为 USDT。
- 每次运行都要把 `D_t`（各袖子）、`S_t`、`W_t` 写入数据库。

---

## 5. 风控

### 5.1 系统状态（持久化在 `system_state` 表）

| 状态 | 含义 | 进入方式 | 退出方式 |
|---|---|---|---|
| `RUNNING` | 正常运行 | 默认状态 | — |
| `PAUSED` | 只做快照和报告，不交易 | `python -m bot pause` | `python -m bot resume --confirm` |
| `HALTED` | 回撤熔断，已清仓 | §5.4 | `python -m bot resume --confirm` |
| `KILLED` | 紧急停止，已清仓 | §5.5 | `python -m bot resume --confirm --after-kill` |

### 5.2 净值与份额（NAV per unit）

- **受管净值**：`NAV = USDT（free + locked）+ Σ_{受管资产} 数量 × 执行环境 ticker 价格`。受管资产的定义见 §7。
- **份额**：首次运行时 `units = NAV / 1.0`，即单位净值 `NAVu = 1.0`。
- **出入金**：通过 `python -m bot capital --deposit 100 --note "..."` 或 `--withdraw` 手工登记，按当时的 `NAVu` 申购或赎回份额，写入 `capital_flows` 表。
- **无法解释的余额变化**：如果 USDT 或受管资产的余额变化超过 1 USDT，又无法用本系统的成交解释，就发送 `UNEXPLAINED_BALANCE_CHANGE` 告警，并且**当天禁止买入**，直到人工登记或运行 `python -m bot capital --acknowledge`。
- **策略可用资金**：`C = min(NAV, max_capital_usdt)`。目标市值 = `W_t(i) × C`。

### 5.3 单日异常保护

- 比较本次和上一次 `PRE_TRADE` 快照的 `NAVu`。若 `NAVu_now / NAVu_prev − 1 ≤ −daily_anomaly_limit`（默认 0.15）：
  - 本次运行**禁止买入**，允许卖出；
  - 发送 `DAILY_ANOMALY` 告警；
  - 下一次运行自动恢复。
- 两次快照的间隔超过 36 小时时（中间漏跑），不做这项检查，只记录 INFO。

### 5.4 回撤熔断

1. 计算 `dd = NAVu / max(历史 NAVu) − 1`。
2. 若 `dd ≤ −max_drawdown_halt`（默认 0.40，**在 M2 结束后按"正式回测最大回撤 × 1.5，且不超过 0.5"重新设定**），先做**数据健全性检查**：对每个受管资产，比较执行环境 ticker 价格与 live `data-api` ticker 价格。
   - **对 testnet 不适用**：testnet 价格独立于 live。在 testnet 上跳过这一步，只记录日志。
3. 有任何资产的偏差 > `data_sanity_max_deviation`（0.20），或者余额接口返回空、价格为 0 → 判定为 `DATA_ANOMALY`：进入 `PAUSED`，**不清仓**，发送 CRITICAL 告警。
4. 检查通过 → 按 §5.5 的清仓流程卖出全部受管资产，进入 `HALTED`，发送 CRITICAL 告警。

### 5.5 Kill Switch

- **触发方式**（v1 的 Telegram 不接收任何命令，见 `open_questions.md` Q5，已选 C）：
  - 命令行 `python -m bot kill`：**立即执行**，是远程紧急停止的标准方式（SSH 登录服务器后执行）。
    - 进程锁空闲时，直接执行下面的动作；
    - 进程锁被占用（有一次运行正在进行）时，先创建 `KILL` 文件，等待正在运行的进程在下一笔订单前停下并释放锁（最长等待 120 秒），再执行动作；超时仍拿不到锁则退出并提示，`KILL` 文件保留。
  - 项目根目录下存在 `KILL` 文件：程序在每次运行开始时检查，并且在每一笔订单之前再检查一次；常驻模式（`app/daemon.py`）每 60 秒额外检查一次。**注意**：v1 没有常驻进程时，只创建 `KILL` 文件要等到下一次运行（或正在进行的运行的下一笔订单前）才生效；需要立即停止时用 `python -m bot kill`。
- **动作**：
  1. 撤销本策略的全部挂单，按 clientOrderId 前缀识别；
  2. 卖出全部受管资产：使用 IOC，滑点上限为 `execution.kill_slippage`（默认 0.01），最多尝试 3 次；
  3. 进入 `KILLED` 状态，发送 CRITICAL 告警；
  4. 最后删除 `KILL` 文件，防止重启后被重复触发。
- **不卖**非受管资产。

### 5.6 运行前风控的执行顺序

`KILL 检查 → 系统状态检查 → 数据质量 → 无法解释的余额变化 → 回撤熔断 → 单日异常 → 生成订单`。

### 5.7 ATR 追踪止损（实现，默认关闭）【待研究 E6】

配置项：`risk.trailing_stop.enabled: false`。启用后的规则：

- **入场日**：受管资产的数量从不超过灰尘阈值（市值 < 交易所最小下单额）变为超过阈值的那一天。从这天起开始记录 `highest_close`（t 日收盘价）。
- 之后每天更新：`highest_close = max(highest_close, c_t)`。加仓不重置；持仓重新回到灰尘阈值以下时，删除记录。
- **触发条件**：`c_t < highest_close − atr_multiplier × ATR(atr_period)_t`（默认 2.5、14）。触发后：
  - 写入 `stop_events` 表；
  - `cooldown_until = t + cooldown_days`（默认 7 天，按日历日计算）；
  - 按 §4.4 的 `F(·)` 剔除该币。
- 止损只适用于卫星袖子的币。核心袖子（BTC）由大盘过滤负责退出。

---

## 6. 订单执行

### 6.1 交易所客户端（`bot/exchange/binance/`）

自研同步客户端，只实现以下接口：

| 接口 | 用途 |
|---|---|
| `GET /api/v3/time` | 计算时钟偏移 |
| `GET /api/v3/exchangeInfo` | 交易对、过滤器、限频（weight 20） |
| `GET /api/v3/klines` | K 线（weight 2） |
| `GET /api/v3/ticker/bookTicker`（单个 symbol） | 盘口买一卖一（weight 2） |
| `GET /api/v3/ticker/price` | 估值价格 |
| `GET /api/v3/avgPrice` | 参考价，用于价格过滤检查 |
| `GET /api/v3/account`（`omitZeroBalances=true`） | 余额（weight 20） |
| `GET /api/v3/account/commission` | 实际费率（weight 20） |
| `POST /api/v3/order` | 下单 |
| `GET /api/v3/order`（按 `origClientOrderId`） | 查单（weight 4） |
| `DELETE /api/v3/order` | 撤单 |
| `GET /api/v3/openOrders`（指定 symbol） | 当前挂单（weight 6） |
| `GET /api/v3/myTrades`（带 `orderId`） | 成交明细（weight 5） |

- **签名**：
  - **先对 query string 或 body 做 percent-encode，再签名**。2026-01-15 起这是强制要求，否则返回 `-1022`。
  - 同时支持 HMAC-SHA256 和 Ed25519（官方推荐 Ed25519，HMAC 已被标为 deprecated）。类型由 `BINANCE_KEY_TYPE=hmac|ed25519` 决定，Ed25519 私钥路径从 `BINANCE_PRIVATE_KEY_PATH` 读取。
  - 实现前，以官方 GH/rest-api.md 中的签名示例作为单元测试用例。
- **超时**：连接超时 5 秒，读超时 `execution.http_timeout_sec`（10 秒）。
- **重试**：
  - **GET** 请求遇到网络错误或 5xx，按 1、2、4 秒退避，最多重试 3 次。
  - **POST/DELETE 订单类请求一律不自动重试**，按 §6.6 处理。
- **数值**：全部用 `decimal.Decimal`。从 JSON 解析时保留字符串，交给 `Decimal(str)`，**禁止经过 float**。

### 6.2 单次运行流程（`python -m bot run --once`）

```
 1. 获取进程锁（data/run.lock，非阻塞；拿不到时退出并记录日志）
 2. 检查 KILL 文件；加载配置并校验；读取系统状态
 3. 同步时钟；拉取 exchangeInfo（live 与执行环境）、余额、实际费率
 4. 对账：处理上一次运行遗留的 SENT/UNKNOWN 意图（§6.6），再与交易所余额核对（§6.8）
 5. 更新 K 线缓存；数据质量检查；确定 t
 6. 计算大盘状态、各袖子的 D_t、S_t，以及 W_t（纯函数）
 7. 计算 PRE_TRADE 净值快照并写库；按 §5.6 执行风控
 8. 生成订单计划（§6.3）；把意图写库（PLANNED）
 9. 执行全部卖单 → 重新查询余额 → 按实际可用 USDT 执行买单
10. 再次对账；计算 POST_TRADE 快照；写入 run 状态 DONE；发送日报
```

**幂等性**：

- `run_id = f"{strategy_id}:{运行日}"`。
- 同一个 `run_id` 已经是 `DONE` 时，不再交易（只更新快照），除非指定 `--force-rebalance`。
- 运行中断后重跑，从第 1 步重新开始。由于订单计划是"目标减去当前实际持仓"，已经成交的部分不会被重复下单。前提是第 4 步已经处理完所有不确定的订单。

**`--force-rebalance`**：忽略"今天已经 DONE"的检查，同时忽略权重容忍带。最小下单额的检查仍然生效。用于 testnet 联调。

### 6.3 订单计划

对每个 `i ∈ 当前受管持仓 ∪ W_t 中权重 > 0 的币`：

```
bid, ask = 执行环境的 bookTicker
mid      = (bid + ask) / 2
cur_val  = 数量（free + locked）× mid
tgt_val  = W_t(i) × C
cw       = cur_val / C
delta    = tgt_val − cur_val
跳过条件（由 bot/strategy/rebalance.py 中的纯函数 should_skip 统一判断，见下文）：
  a) 容忍带，仅在 tgt_val > 0 时适用：
     threshold_mode == "absolute"：|W_t(i) − cw| < rebalance.threshold（0.02）
     threshold_mode == "relative"：|W_t(i) − cw| < rebalance.threshold_rel（0.25）× max(W_t(i), cw)
  b) 金额下限，两种模式都适用：|delta| < max(rebalance.min_trade_usdt（10）, minNotional × 1.1)
     例外：tgt_val == 0 时，只要持仓市值 ≥ minNotional，就卖出全部
  c) tgt_val == 0 且持仓市值 < minNotional：记为灰尘（dust），不下单
```

**容忍带模式**【待研究 E8】：`rebalance.threshold_mode` 取 `absolute` 或 `relative`，暂定默认 `relative`。理由：核心–卫星结构下，卫星单币的目标权重只有约 3%～4%（袖子资金 0.5 × 单币 1/5，再乘以波动率目标缩放），绝对阈值 0.02 相当于单币仓位的一半以上，叠加 7 日平滑后大部分调整都会被跳过。最终默认值由 E8 决定。

**共用跳过函数**：研究引擎（§9.3）与订单计划必须调用同一个函数，禁止各写一份：

```python
# bot/strategy/rebalance.py
class SkipReason(Enum):
    TOLERANCE_BAND = "tolerance_band"   # 条件 a
    BELOW_MIN_TRADE = "below_min_trade" # 条件 b
    DUST = "dust"                       # 条件 c

def should_skip(target_w: float, current_w: float, delta_usdt: float, position_usdt: float,
                min_notional_usdt: float, cfg: RebalanceConfig) -> SkipReason | None: ...
```

研究引擎中 `min_notional_usdt` 取 5（历史过滤器无法获得的近似值），实盘取执行环境 exchangeInfo 的 `NOTIONAL.minNotional`。每次跳过都按原因计数，写入回测报告和日报。

**卖单**：

- 数量 `q = min(|delta| / bid, free_qty)`；如果 `tgt_val == 0`，`q = free_qty`。
- 按 `stepSize` 向下取整，再检查 `minQty ≤ q ≤ maxQty`。
- 限价 `p = bid × (1 − max_slippage)`，按 `tickSize` **向上**取整。

**买单**（在卖单完成并刷新余额之后计算）：

- 可用资金 `A = USDT_free × (1 − fee_buffer)`，`fee_buffer` 默认 0.002。
- 所有买单的 `delta` 之和超过 `A` 时，按比例缩小每一笔。
- 限价 `p = ask × (1 + max_slippage)`，按 `tickSize` **向下**取整。
- 数量 `q = 金额 / p`，按 `stepSize` 向下取整。

**过滤器校验**（按 GH/filters.md，以执行环境的 exchangeInfo 为准；每笔订单下单前都要校验）：

| 过滤器 | 规则 |
|---|---|
| `PRICE_FILTER` | `minPrice ≤ p ≤ maxPrice`，且 `p % tickSize == 0`（值为 0 的项不检查） |
| `LOT_SIZE` | `minQty ≤ q ≤ maxQty`，且 `q % stepSize == 0` |
| `NOTIONAL` | `minNotional ≤ p × q`；`maxNotional` 也要检查 |
| `PERCENT_PRICE_BY_SIDE` | 设 `ref` 为 `GET /api/v3/avgPrice` 的结果。买单要求 `ref × bidMultiplierDown ≤ p ≤ ref × bidMultiplierUp`，卖单要求 `ref × askMultiplierDown ≤ p ≤ ref × askMultiplierUp` |

处理方式：

- 价格超出 `PERCENT_PRICE_BY_SIDE` 区间时，把 `p` 收窄到区间边界，再重新取整。
- 收窄后，如果买价 < ask 或卖价 > bid，订单可能无法成交。这种情况照常下单，并记录 INFO。
- 其他校验不通过时，放弃该笔订单，记录 WARN。
- `MIN_NOTIONAL` 已经没有任何交易对使用。只保留解析容错：遇到时按 `NOTIONAL` 的规则处理。

### 6.4 newClientOrderId

格式（不超过 36 个字符，字符集 `[A-Za-z0-9_-]`，与时钟无关）：

```
{strategy_id}-{运行日 yymmdd}-{symbol_hash8}-{S|B|K}{seq:03d}-{attempt}
示例：mom1-261102-3f9a1c2b-B007-1   （27 个字符）
```

- `strategy_id` 只能是 1～6 位小写字母或数字（在配置校验中检查）。
- `symbol_hash8` 取 `sha1(symbol.encode('utf-8')).hexdigest()[:8]`。不直接用 symbol，因为 symbol 可能含有非 ASCII 字符，长度也可能超限。
- `S`、`B`、`K` 分别表示卖单、买单、kill 卖单。
- `seq` 是当日意图序号，从数据库的 `(strategy_id, 运行日)` 序列中分配，**重启后继续递增**。
- `attempt` 是同一意图的第几次尝试，从 1 开始。
- 每次下单尝试的 ID 都必须全局唯一。原因：交易所只保证 clientOrderId 在当前挂单中唯一，复用的行为存在不确定性。

### 6.5 下单（v1：`execution.mode: ioc`）

1. 写入意图（`order_intents.status = PLANNED`），然后发送 `POST /api/v3/order`，参数为 `type=LIMIT, timeInForce=IOC, newOrderRespType=FULL, newClientOrderId=cid`。发送前把状态更新为 `SENT`。
2. 根据响应处理：

   | 响应 | 处理 |
   |---|---|
   | `FILLED` | 记录 `fills[]`（含 `commission`、`commissionAsset`） |
   | `EXPIRED`，且 `executedQty > 0` | 部分成交，剩余部分已过期。记录成交，读取 `expiryReason`（如果有） |
   | `EXPIRED`，且 `executedQty == 0` | 没有成交 |
   | HTTP 4xx（429/418 除外，见 §3.5），或业务错误码（如 `-1013`、`-2010`） | 记为 `REJECTED`，记录错误码，**不重试** |

3. 未成交的剩余数量：重新获取 bookTicker，以 `attempt + 1` 再下一次。最多尝试 `max_attempts`（2）次。仍未完成时发送 `ORDER_INCOMPLETE` 告警（WARN），不再继续追单。
4. 买单使用实际成交得到的数量；手续费在没有用 BNB 抵扣时，从买到的币中扣除。**持仓数量一律以交易所余额为准**。

`maker_then_ioc` 模式（v1.1，不在 v1 范围内）：先下 `LIMIT_MAKER`（买价取 bid、卖价取 ask，`newOrderRespType=FULL`，因为这类订单默认响应是 ACK）。如果返回 `-2010`（"would immediately match"），直接转入 IOC。挂单等待 `maker_timeout_sec` 后撤单，撤单之后必须用 `GET order` 确认最终的成交量，再用 IOC 处理剩余部分。

### 6.6 执行状态未知的处理（关键路径，必须有单元测试）

**触发条件**：下单请求超时、网络中断，返回 HTTP 5xx，或者返回 `-1007`（"execution status unknown"）。

1. 意图状态记为 `UNKNOWN`。
2. 依次在 `unknown_status_query_delays_sec`（`[1, 3, 10, 30, 60]`）秒后，用 `GET /api/v3/order?symbol=…&origClientOrderId=cid` 查询：
   - 查到订单：按查询结果更新状态，并用 `GET /api/v3/myTrades?symbol=…&orderId=…` 补全成交明细。
   - 全部返回 `-2013`（订单不存在）：
     1. 意图记为 `NOT_FOUND`；
     2. 本次运行**不再对该 symbol 下任何订单**；
     3. 发送 `ORDER_STATUS_UNKNOWN` 告警（WARN）。
3. 下一次运行的第 4 步，对 `NOT_FOUND` 的意图再查询一次：
   - 仍然查不到，并且该资产余额的变化能被已知成交解释 → 视为从未下单，关闭该意图。
   - 余额变化无法解释 → 写入"推断成交"事件，发送告警，以交易所余额为准。
4. **任何情况下都不能因为"不知道是否成交"而换一个新 ID 重下同一个意图**。下一次运行会根据实际余额重新计算差额，这是唯一的补单途径。

### 6.7 撤单

v1 只用 IOC，正常情况下不会留下挂单。第 4 步对账时，如果发现本策略前缀的挂单（例如人工或旧版本留下的），全部撤销，并记录 WARN。

### 6.8 对账

- 每次运行的第 4 步和第 10 步都要执行。
- 交易所余额是**唯一真相**。对每个受管资产，比较 `本地记录数量 = 上次快照数量 + 本次成交净额 − 手续费` 与交易所余额：
  - 差额 > 1 个 stepSize：写入 `RECONCILE_DIFF` 事件，用交易所数值覆盖本地记录；
  - 差额市值 > 1 USDT：同时发送告警。
- 检查挂单。

### 6.9 手续费与灰尘

- 每笔成交都记录 `commission` 和 `commissionAsset`。成本统计统一按成交时的价格折算成 USDT。
- 是否开启 BNB 抵扣，见 `open_questions.md` Q8，v1 默认关闭。如果开启，BNB 余额作为"手续费储备"，不计入策略持仓，单独显示；同时必须处理 BNB 也可能被卫星袖子选中的冲突（从 `approved_assets` 移除，或分开记账），见 §16。
- 灰尘：持仓市值低于 `minNotional`，或数量低于 `minQty` 时，无法卖出。灰尘仍然计入净值，在日报中列出。之后如果再买入同一个币，灰尘会自然合并。

---

## 7. 账户与资金归属

- **受管资产** = USDT + `positions` 表中 `managed = true` 的资产。
  - 本策略第一次买入某个资产时，自动登记为受管资产。
  - 用户也可以用 `python -m bot adopt <ASSET>` 显式接管已有的资产。
- **非受管资产**：账户里其他所有资产（包括 BNB 手续费储备）。不计入净值，不交易，也不被 kill 卖出，只在日报中列出。
- **启动时**：交易所里存在、但本地不受管的资产，一律视为非受管，并发送一次 INFO。这一点与 v2 不同：v2 会自动纳入这些资产，风险很大（R-25）。
- **live 环境**：强烈建议使用**专用子账户**（Q2）。testnet 和 demo 天然是独立账户，不受影响。
- **数据模型**：所有业务表都带 `strategy_id` 和 `account_id`。v1 只有一个策略（`mom1`）和一个账户（`spot-main`）。

---

## 8. 状态持久化

- 状态库：`data/state.db`（SQLite，SQLAlchemy 2.x，WAL 模式）。行情缓存放在单独的 `data/market.db`。
- 金额与数量字段用 TEXT 存储十进制字符串，时间统一用 UTC ISO 8601。
- 表结构变更用简单的版本号迁移：`schema_version` 表，加上 `bot/persistence/migrations/` 目录。v1 不引入 Alembic。

| 表 | 主要字段 | 说明 |
|---|---|---|
| `accounts` | account_id PK, venue, account_type(`SPOT`/`USDM_FUTURES`), env | 第二阶段新增合约账户时只需加行 |
| `strategies` | strategy_id PK, name, version, config_hash | |
| `runs` | run_id PK, strategy_id, account_id, run_date, signal_date(t), status(`STARTED/PLANNED/EXECUTING/DONE/FAILED/SKIPPED`), started_at, finished_at, error | |
| `regime_state` | date PK, symbol, close, sma, state, switched | |
| `daily_targets` | (strategy_id, sleeve, date, symbol) PK, kind(`D`/`S`/`W`), weight | 审计用 |
| `stop_events` | id, strategy_id, symbol, date, close, stop_line, cooldown_until | 止损启用时使用 |
| `order_intents` | cid PK, run_id, strategy_id, account_id, symbol, side, seq, attempt, intended_qty, limit_price, status(`PLANNED/SENT/FILLED/PARTIAL/EXPIRED/REJECTED/UNKNOWN/NOT_FOUND/CLOSED`), exchange_order_id, error_code, created_at, updated_at | write-ahead |
| `orders` | cid PK, order_id, symbol, status, executed_qty, cumm_quote_qty, expiry_reason, raw_json, updated_at | 交易所返回的原始订单 |
| `fills` | (symbol, trade_id) PK, cid, order_id, side, price, qty, quote_qty, commission, commission_asset, time | |
| `positions` | (account_id, strategy_id, asset) PK, managed, qty, entry_date, highest_close, dust, frozen, updated_at | |
| `capital_flows` | id, account_id, strategy_id, time, kind(`DEPOSIT/WITHDRAW/ADJUST`), amount_usdt, nav_per_unit, units_delta, note | |
| `nav_snapshots` | id, strategy_id, account_id, time, kind(`PRE_TRADE/POST_TRADE`), nav_usdt, units, nav_per_unit, gross_exposure, details_json | |
| `system_state` | strategy_id PK, state, since, reason | |
| `asset_review` | asset PK, status(`PENDING`/`NOTIFIED`), first_seen, last_notified, info_json | 审核结果以 assets.yaml 为准 |
| `events` | id, time, level, type, strategy_id, account_id, payload_json | 风控触发、对账差异、错误等 |
| `id_sequences` | (strategy_id, run_date) PK, next_seq | 为 newClientOrderId 分配序号 |

**启动流程**（每次 `run --once` 都会执行）：

1. 读取配置并校验；
2. 检查密钥是否存在（只检查环境变量，不打印内容）；
3. 读取系统状态；
4. 同步时钟；
5. 拉取余额和挂单；
6. 处理未决意图；
7. 对账；
8. 继续执行 §6.2 的后续步骤。

v1 没有常驻状态，所以不需要单独的"初始化 tranche"步骤：§4.4 的 `D` 可以随时重算。

---

## 9. 回测与研究

### 9.1 两层引擎

1. **研究引擎**（`bot/backtest/engine.py`）：逐日循环。直接调用 `bot/strategy/` 中的纯函数生成 `W_t`，再按权重差额成交并计算成本，供 E0～E10 的实验使用。速度要求：5 年、30 个币的单次回测在 10 秒以内。
2. **执行回放**（`bot/backtest/replay.py`）：用模拟交易所 `SimSpotAccount` 实现 §11 的 `SpotAccount` 接口，在 60～120 天的区间里逐日运行**真实的** `run_once` 流程，包括订单计划、过滤器取整、先卖后买和对账。用于验证执行层与研究引擎一致：两者的期末 NAV 差异 < 1%，并且每一项差异都能解释（取整、最小下单额）。

### 9.2 历史数据

1. 分页列出 `https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?delimiter=/&prefix=data/spot/monthly/klines/`（参数 `marker`；当 `IsTruncated=false` 时结束），保留以 `USDT` 结尾的交易对，**包括已下架的**。
2. 对每个交易对，列出 `data/spot/monthly/klines/{SYMBOL}/1d/` 下的 zip 文件并下载，校验同名 `.CHECKSUM`（sha256）。当月的数据从 `data/spot/daily/klines/` 补充，最近几天再用 REST `klines` 补充。
3. CSV 解析：
   - **没有表头**，共 12 列：open_time、open、high、low、close、volume、close_time、quote_volume、trades、taker_buy_base、taker_buy_quote、ignore。
   - **时间戳单位**：2025-01-01 起为微秒（16 位），此前为毫秒（13 位）。统一规则：`open_time > 10**14` 时除以 1000。
   - 遇到首行是表头的情况，自动跳过（兼容处理）。
4. 按日期去重，按 §2.3 切分交易段，写入 `market.db`，`source` 记为 `'vision'`。
5. 下载工具：`python -m bot data sync --since 2018-01-01`，支持断点续传，并发数不超过 8。下载的文件放在独立的 `data/raw/` 目录。

### 9.3 回测规则

- 信号使用 t 日收盘数据，在 **t+1 日开盘价**成交。止损同样在 t 日收盘判断、t+1 日开盘成交。
- 单边成本 = `fee`（0.001）+ `slippage`（0.0005），都可配置。E9 实验会加入按成交额排名分档的滑点。
- 初始资金为 `backtest.initial_capital`（默认 1000 USDT）。是否调仓由 §6.3 的共用函数 `should_skip` 判断（容忍带、`min_trade_usdt`、5 USDT 最小下单额近似），数量允许为小数。
- **按真实资金量回测**（E9）：`initial_capital` 分别取 500 和 2000 USDT，严格应用最小下单额、`min_trade_usdt` 和容忍带，并与"理想情况"（`min_trade_usdt = 0`、最小下单额 = 0、不设容忍带）对比，报告收益和跟踪误差的偏差，以及实际调仓频率。
- 标的池按**当时**的数据逐日计算，包含已下架的币，`approved_assets` 按 §2.2 第 5 条近似。
- **下架处理**：持仓币所在的交易段结束（之后再没有日线）时，在最后一根日线按 `close × (1 − delist_haircut)` 退出。`delist_haircut` 默认 0.5【待研究】，这是保守假设。
- 回测的 NAV 每天按收盘价估值。

### 9.4 输出

- 完整报告写入 `reports/<run_name>/`，**不进 git**。
- 每个正式实验的 `summary.md` 和关键图表（权益曲线、热力图）复制到 `doc/research/reports/<实验编号>/` 并提交（由 `experiment` 命令自动完成）。

报告内容：

- `summary.json` 与 `summary.md`：§1.2 的全部指标；分年度收益表（必须包含 2022 年）；前、后半段对比；单日跌幅 ≤ −10% 和 ≤ −15% 的天数；最长回撤持续时间；平均仓位；空仓天数占比；换手率；总成本；下架退出的次数。
- **Regime 区间表**：每个 Risk-On 区间的起止日期、天数、策略收益、BTC 收益。
- **最大 5 段回撤表**：峰值日、谷底日、恢复日、回撤幅度。
- `equity.png`：策略与 §1.3 四个基准的权益曲线，使用对数坐标，并附回撤子图。
- `daily.csv`：每日的 NAVu、各袖子权重、W_t、换手、成本。
- **调仓统计**：按袖子统计实际调仓频率（每月平均交易笔数、平均每笔金额），以及被 `should_skip` 跳过的次数和原因分布；卫星袖子必须单独列出。
- 试验记录追加到 `doc/research/trials.csv`：本次实验的全部配置及结果，用于计算 DSR。

### 9.5 研究命令

| 命令 | 说明 |
|---|---|
| `python -m bot backtest --config ... --start 2020-01-01 --end 2025-06-30 --name xxx` | 单次回测 |
| `python -m bot experiment --id E3a` | 读取 `doc/research/experiments.yaml` 中定义的网格，逐个运行，结果追加到 `doc/research/trials.csv`；摘要复制到 `doc/research/reports/E3a/`；结论由人写入 `doc/research/log.md` |
| `python -m bot walkforward --train-months 12 --test-months 3 --choose target_annual_vol,core_weight` | 滚动训练与检验 |
| `python -m bot bootstrap --block-days 30 --n 1000` | 最大回撤的 block bootstrap 分布 |
| `python -m bot holdout --final --confirm YYYY-MM-DD` | 保留集一次性检验（§3.6） |
| `python -m bot dsr --trials doc/research/trials.csv` | 计算 Deflated Sharpe Ratio |

v1 的统计工具只保留 DSR、block bootstrap 和保留集检验；PBO/CSCV 放入后续版本（§16）。

### 9.6 回测正确性（必须有测试，详见 `dev_plan.md` §6.2）

- **无前视**：构造人工数据，在 t+1 日放入极端值，确认 t 日的信号和成交价都不受影响。
- **已知答案**：
  - 单币恒定上涨、无成本时，CAGR 与解析解一致；
  - 两个币交替领涨时，换仓日期与人工计算一致。
- **成本守恒**：`Σ成本 = Σ|成交额| × 单边成本率`。
- **交易段切分**：用 LUNA 式的同名复用数据验证。
- **保留集拦截**：研究命令读取 2025-07-01 之后的数据时必须报错。

---

## 10. 配置

### 10.1 `config/config.yaml`

```yaml
env: testnet                  # testnet | demo | live（live 需要额外确认，见 §13）
dry_run: false                # true：只计算并输出订单计划，不下单
strategy_id: mom1             # 1~6 位小写字母或数字，会出现在 clientOrderId 中
account_id: spot-main
max_capital_usdt: 500

data:
  market_data_rest: https://data-api.binance.vision
  history_days: 800

universe:                     # 只用于卫星袖子
  universe_size: 30           # 【待研究 E1】
  min_median_quote_volume: 5000000   # 【待研究 E1】
  volume_median_days: 30
  min_listing_days: 90
  segment_gap_days: 7
  assets_file: config/assets.yaml

regime:
  symbol: BTCUSDT
  sma_period: 100             # 【待研究 E2】
  buffer: 0.02                # 【待研究 E2】
  replay_days: 730
  risk_on_mode: ramp          # ramp | immediate【待研究 E2c】

sleeves:
  core:
    enabled: true
    type: trend
    capital_weight: 0.5       # 【待研究 E7】
    symbols: [BTCUSDT]
    target_annual_vol: 0.30   # 【待研究 E4】
    vol_lookback: 30
    max_gross: 0.95
    rebalance_mode: smoothed  # smoothed | weekly
    smoothing_days: 7
    rebalance_weekday: 0      # 仅在 weekly 模式下使用，0 = 周一
  satellite:
    enabled: true
    type: xs_momentum
    capital_weight: 0.5       # 【待研究 E7】
    exclude_symbols: [BTCUSDT]
    momentum_lookback: 28     # 【待研究 E3a】
    momentum_skip: 0          # 【待研究 E3b】
    score: ret_over_vol       # ret_over_vol | ret | blend【待研究 E3c】
    max_lookback_return: 1.5  # null 表示关闭【待研究 E5】
    coin_sma_period: 50
    top_n: 5
    weighting: equal          # equal | inverse_vol
    max_weight_per_coin: 0.25
    target_annual_vol: 0.30   # 【待研究 E4】
    vol_lookback: 30
    max_gross: 0.95
    rebalance_mode: smoothed
    smoothing_days: 7
    rebalance_weekday: 0

rebalance:
  threshold_mode: relative    # absolute | relative【待研究 E8】
  threshold: 0.02             # absolute 模式：|W − cw| < threshold 时跳过
  threshold_rel: 0.25         # relative 模式：|W − cw| < threshold_rel × max(W, cw) 时跳过
  min_trade_usdt: 10          # 两种模式都生效
  run_time_utc: "00:10"       # 供常驻模式和部署脚本使用

risk:
  trailing_stop:
    enabled: false            # 【待研究 E6】
    atr_period: 14
    atr_multiplier: 2.5
    cooldown_days: 7
  max_drawdown_halt: 0.40     # M2 结束后按"正式回测最大回撤 × 1.5，上限 0.5"重新设定
  daily_anomaly_limit: 0.15
  data_sanity_max_deviation: 0.20
  force_exit_assets: []

execution:
  mode: ioc                   # ioc（v1）| maker_then_ioc（v1.1）
  max_slippage: 0.003
  max_attempts: 2
  kill_slippage: 0.01
  fee_buffer: 0.002
  http_timeout_sec: 10
  recv_window_ms: 5000
  unknown_status_query_delays_sec: [1, 3, 10, 30, 60]

backtest:
  initial_capital: 1000
  fee: 0.001
  slippage: 0.0005
  delist_haircut: 0.5         # 【待研究】
  start: "2020-01-01"
  end: "2025-06-30"

research:
  holdout_start: "2025-07-01"

notify:
  telegram_enabled: true      # 只发通知，不接收命令（Q5 选 C）；没有配置 token 时自动降级为只写日志
  heartbeat_url: null         # M6：每次运行成功后 ping，例如 https://hc-ping.com/<uuid>；null 表示关闭
  daily_report: true

logging:
  level: INFO
  dir: logs
  retention_days: 90
```

配置校验（pydantic）至少检查：

- 所有启用袖子的 `capital_weight` 之和 ≤ 1；
- 每个袖子的 `max_gross` ≤ 1，`0 < target_annual_vol ≤ 1`；
- `strategy_id` 符合 `^[a-z0-9]{1,6}$`；
- `env == live` 时，`dry_run` 未设置会给出警告，并且必须满足 §13 的确认要求。

### 10.2 `.env.example`

```
BINANCE_API_KEY=
BINANCE_KEY_TYPE=hmac            # hmac | ed25519
BINANCE_API_SECRET=              # 仅 hmac 使用
BINANCE_PRIVATE_KEY_PATH=        # 仅 ed25519 使用（PEM 格式）
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

---

## 11. 架构与项目结构

### 11.1 目录

```
bot/
  __main__.py              # CLI：run / backtest / experiment / walkforward / bootstrap / holdout / dsr /
                           #      kill / pause / resume / status / capital / adopt / assets / data
  config.py                # pydantic 模型，加载与校验 config.yaml、assets.yaml
  domain/
    types.py               # AccountType、Side、OrderStatus、Instrument、SymbolRules（Decimal）、
                           # TargetPortfolio、OrderIntent、Fill、Balance（纯数据类）
  exchange/
    base.py                # Protocol：MarketDataSource、SpotAccount；预留 FuturesAccount
    binance/
      client.py            # HTTP、签名（HMAC/Ed25519）、限频、错误映射
      market_data.py       # MarketDataSource 实现（data-api）
      spot_account.py      # SpotAccount 实现（testnet/demo/live）
      rules.py             # exchangeInfo → SymbolRules，过滤器校验与取整
  data/
    store.py               # market.db：K 线缓存与增量更新
    vision.py              # data.binance.vision 下载、校验、解析
    segments.py            # 交易段切分
    holdout.py             # 保留集守卫
  strategy/
    indicators.py          # §4.1
    regime.py              # §4.2
    universe.py            # §2
    sleeves.py             # 核心袖子与卫星袖子的 D_t（§4.3）
    smoothing.py           # S_t（§4.4）
    portfolio.py           # W_t 与 cap_weights（§4.5）
    rebalance.py           # should_skip：研究引擎与订单计划共用的跳过判断（§6.3）
    base.py                # Strategy Protocol
    momentum_v1.py         # 本策略：组合上面各模块，实现 Strategy
  risk/
    ledger.py              # NAV、份额、出入金（§5.2）
    guards.py              # 熔断、异常保护、数据健全性检查、系统状态（§5）
    stops.py               # ATR 追踪止损（§5.7）
    killswitch.py
  execution/
    planner.py             # 订单计划（§6.3）
    spot_executor.py       # 下单、未知状态处理、对账（§6.5～6.8）
    client_order_id.py     # §6.4
  persistence/
    models.py, repo.py, migrations/
  notify/
    telegram.py            # 只发送通知（sendMessage），不接收命令
    heartbeat.py           # M6：外部心跳 ping
  backtest/
    engine.py, replay.py, sim_account.py, report.py, research.py, stats.py (DSR/bootstrap)
  app/
    run_once.py            # §6.2 编排
    daemon.py              # 可选的常驻模式：按 run_time_utc 每日调用 run_once
config/
  config.yaml
  assets.yaml
doc/                       # 全部文档
  DECISIONS.md             # 设计与技术决策记录
  binance_momentum_bot_spec.md  dev_plan.md  strategy_review.md  strategy_research.md  open_questions.md
  research/
    log.md                 # 研究日志（见 dev_plan §4）
    experiments.yaml       # 实验网格定义
    trials.csv             # 全部试验记录（DSR 用）
    holdout_runs.log       # 保留集运行记录
    proposal_v1.md         # M3 结束时的参数提案
    reports/<实验编号>/     # 每个实验的 summary.md 与关键图表（提交）
    scratch/               # 文档阶段的一次性探索脚本
reports/                   # 完整回测报告（不进 git）
scripts/                   # check.ps1 / check.sh、drills/
tests/
.env.example  .gitignore  requirements.txt (或 pyproject.toml)  README.md  CLAUDE.md  Dockerfile
```

### 11.2 关键接口（为多策略、多账户预留）

```python
class AccountType(Enum):
    SPOT = "SPOT"
    USDM_FUTURES = "USDM_FUTURES"    # 第二阶段

@dataclass(frozen=True)
class InstrumentKey:
    account_type: AccountType
    symbol: str                      # 例如 "BTCUSDT"；同一 symbol 在现货和合约中是两个不同的 InstrumentKey

@dataclass(frozen=True)
class TargetPortfolio:
    strategy_id: str
    as_of: date                      # t
    # 账户 → 品种 → 目标。现货为权重（≥0）；合约预留为带符号的名义敞口比例
    targets: dict[str, dict[InstrumentKey, Decimal]]

class Strategy(Protocol):
    strategy_id: str
    def required_symbols(self, ctx: "StrategyContext") -> set[InstrumentKey]: ...
    def compute_targets(self, ctx: "StrategyContext") -> TargetPortfolio: ...   # 纯函数，不得做 I/O

class MarketDataSource(Protocol):
    def get_daily_klines(self, symbol: str, start: date, end: date) -> pd.DataFrame: ...
    def get_exchange_info(self) -> dict: ...

class SpotAccount(Protocol):
    account_id: str
    def get_balances(self) -> dict[str, Balance]: ...
    def get_rules(self, symbol: str) -> SymbolRules: ...
    def get_book_ticker(self, symbol: str) -> tuple[Decimal, Decimal]: ...
    def get_avg_price(self, symbol: str) -> Decimal: ...
    def get_prices(self, symbols: list[str]) -> dict[str, Decimal]: ...
    def place_order(self, req: OrderRequest) -> OrderResult: ...      # 超时、5xx 时抛出 UnknownOrderStatus
    def get_order(self, symbol: str, client_order_id: str) -> OrderResult | None: ...  # -2013 → None
    def cancel_order(self, symbol: str, client_order_id: str) -> OrderResult: ...
    def get_open_orders(self, symbol: str) -> list[OrderResult]: ...
    def get_trades(self, symbol: str, order_id: int) -> list[Fill]: ...

class FuturesAccount(Protocol):     # 第二阶段：只定义接口，v1 不实现
    def get_positions(self) -> dict[str, "FuturesPosition"]: ...
    def get_margin_ratio(self) -> Decimal: ...
    def get_funding_rate(self, symbol: str) -> Decimal: ...
    def place_order(self, req: "FuturesOrderRequest") -> OrderResult: ...

class Transfer(Protocol):           # 第二阶段：现货 ↔ U 本位合约划转
    def transfer(self, asset: str, amount: Decimal, from_: AccountType, to: AccountType) -> str: ...
```

**约束**：

- `strategy/`、`risk/`、`backtest/engine.py` **不得** import `exchange/binance/`。
- 执行器按账户类型分开实现，v1 只有 `SpotExecutor`。
- 多个策略的组合由 `app/run_once.py` 中的分配器完成：`Σ strategy_capital_weight ≤ 1`。v1 只有一个策略，分配器直接透传。
- 第二阶段（资金费率套利）需要增加：
  - `FuturesAccount` 的实现；
  - 允许带符号持仓的 `positions` 视图；
  - 资金费用写入 `capital_flows`（kind=`FUNDING`）；
  - 两条腿的对冲执行器（处理单腿成交风险）；
  - 保证金率风控；
  - 划转。

  这些**现在都不实现**。

---

## 12. 告警与日志

- 日志按天滚动写入文件，同时输出到控制台，保留 90 天。**日志里不得出现密钥、签名或完整的 API Key**。API Key 只显示前 4 位和后 4 位。所有日志经过同一个过滤器统一处理。
- Telegram 只用到 `sendMessage` 接口，**只发通知，不接收任何命令**（Q5 选 C）。没有配置 token 时，只写日志，不报错。
- **外部心跳**（M6）：每次运行结束且 run 状态为 `DONE` 时，请求一次 `notify.heartbeat_url`（例如 healthchecks.io 的 ping 地址），超时 10 秒，失败只记录 WARN，不影响运行。外部服务在约定周期（例如 26 小时）内没有收到 ping 时由它发出告警。这弥补了 `MISSED_RUN` 的盲区：程序没有运行时，它自己无法告警。

| 告警类型 | 级别 | 触发条件 |
|---|---|---|
| `DAILY_REPORT` | INFO | 每次运行结束。内容包括：NAVu、NAV、当日收益、回撤、Regime、各袖子与总体仓位、持仓明细、当日成交与成本、灰尘与非受管资产 |
| `REGIME_SWITCH` | WARN | 大盘状态切换 |
| `ASSET_REVIEW_NEEDED` | INFO | §2.2 |
| `ORDER_INCOMPLETE` / `ORDER_STATUS_UNKNOWN` | WARN | §6.5、§6.6 |
| `RECONCILE_DIFF`（差额 > 1 USDT） | WARN | §6.8 |
| `DAILY_ANOMALY` / `UNEXPLAINED_BALANCE_CHANGE` | WARN | §5.2、§5.3 |
| `DATA_NOT_READY` / `RATE_LIMITED` | WARN | §3.2、§3.5（418 为 CRITICAL） |
| `HELD_SYMBOL_NOT_TRADING` / `DATA_ANOMALY` / `DRAWDOWN_HALT` / `KILLED` | CRITICAL | §2.5、§5.4、§5.5 |
| `UNHANDLED_EXCEPTION` | CRITICAL | 任何未捕获的异常。run 状态记为 FAILED |
| `MISSED_RUN` | WARN | 最近一次 DONE 的 run 距今超过 30 小时。在下次运行时检查；常驻模式下每小时检查一次 |

---

## 13. 安全要求

- API Key 只开启"读取"和"现货交易"，**禁止开启提现**。live Key 必须绑定服务器 IP 白名单（README 中说明操作步骤）。
- 提现权限检查：仅在 live 环境，用 `GET /sapi/v1/account/apiRestrictions` 检查。如果 `enableWithdrawals == true`，拒绝启动。**这个接口的字段名在实现时须对照官方文档核实**（本次没有核实）。testnet 没有 `/sapi`，demo 是否支持不确定，这两个环境跳过检查并记录 INFO。
- `env == live` 时，必须**同时**满足以下条件才允许启动：
  1. 命令行带 `--live-confirm=I_UNDERSTAND`；
  2. 控制台输入 `YES` 确认（Docker 环境用 `--yes` 跳过这一步，但第 1 条不能省略）；
  3. 配置中 `max_capital_usdt` 已显式设置。
- `.env`、`data/`、`logs/`、`reports/`、`*.pem` 加入 `.gitignore`。Ed25519 私钥文件的权限检查只在 Linux 上执行：权限宽于 600 时给出警告。
- 所有外部请求只发往本文档列出的域名，以及：Telegram 的 `api.telegram.org`；心跳服务 healthchecks.io 的 ping 域名 `hc-ping.com`（M6 实现时对照其官方文档核实；改用其他心跳服务时，必须先更新本列表）。

---

## 14. 测试与验收（详细清单见 `dev_plan.md` §6）

1. **单元测试**（pytest，不访问网络），至少覆盖：
   - 过滤器校验与 Decimal 取整（tickSize 和 stepSize 向上、向下取整，NOTIONAL，PERCENT_PRICE_BY_SIDE 的收窄）；
   - 签名：percent-encode 之后的 HMAC 和 Ed25519 结果，与官方示例一致；
   - 指标计算、交易段切分、标的池排名、资产审核规则；
   - 大盘状态机：缓冲带、重放结果的确定性；
   - 卫星和核心袖子的 D_t；`cap_weights`；波动率目标；
   - 平滑窗口 S_t：Risk-Off 清零、止损剔除和冷却期结束后的行为、`ramp` 与 `immediate` 两种模式；
   - `should_skip`：absolute 与 relative 两种模式、金额下限、灰尘，以及研究引擎与订单计划调用的是同一个函数；
   - 订单计划：最小金额、先卖后买、资金不足时按比例缩小；
   - clientOrderId：格式、长度、重启后序号继续递增；
   - **执行状态未知的处理**：用 mock 覆盖超时后查到订单、超时后查不到、5xx、`-1007` 这几种情况；
   - NAV 份额与出入金；回撤熔断与数据健全性检查；kill 流程；
   - 保留集守卫；回测无前视。
2. **回测**：本地跑通，输出 §9.4 的全部报告。研究引擎与执行回放的结果一致。
3. **testnet 联调**：`python -m bot run --once --force-rebalance` 完整执行一次，订单全部成交或过期，数据库记录完整，日志清晰；再立即运行一次 `run --once`，确认没有任何重复下单。
4. **故障演练**（写成脚本 `scripts/drills/`，并在 README 中给出步骤）：
   - 下单过程中杀掉进程，然后重启：状态恢复正确，没有重复下单；
   - 用 mock 注入下单超时：按 §6.6 处理；
   - 创建 `KILL` 文件：撤单、清仓、进入 KILLED 状态；
   - 断网后重跑：失败、告警，下次运行正常恢复；
   - testnet 余额被重置：对账能识别并告警（作为无法解释的余额变化）。
5. **demo 前向运行**：至少连续运行 4 周，每天对账无差错，日报完整。

---

## 15. 开发阶段

见 `doc/dev_plan.md`：里程碑 M0～M7、任务的输入/输出/验收标准、依赖关系、风险点、研究实验顺序、防过拟合规则和检查点。

---

## 16. 后续版本清单（v1 不做）

| 版本 | 内容 | 前置条件 |
|---|---|---|
| v1.1 | `maker_then_ioc` 执行模式 | testnet/demo 运行稳定 2 周 |
| v1.1+ | Telegram 命令（`/status`、`/pause`、`/resume`、`/kill`） | 需要常驻进程才能及时响应；v1 已选 Q5-C |
| v1.1+ | 研究工具：PBO/CSCV（回测过拟合概率） | v1 只保留 DSR、block bootstrap 和保留集检验 |
| 上 live 前评估 | 开启 BNB 手续费抵扣（Q8 选 B） | 必须先把 BNB 从卫星袖子的 `approved_assets` 中移除，或者把"手续费储备 BNB"与"策略持仓 BNB"分开记账 |
| v1.1 | BNB 手续费抵扣与 BNB 储备管理 | Q8 |
| v1.2 | User Data Stream（WebSocket API `userDataStream.subscribe.signature`），用于实时成交回报 | 有了日内执行需求时再做 |
| v1.2 | USDT 活期理财（方案 F） | 先完成接口 spike |
| v1.2 | 灰尘兑换 BNB | — |
| v1.2 | 下架公告监控（自动填写 force_exit） | 需要找到可靠的数据源，**目前不确定** |
| v2 | 核心袖子扩展到 BTC+ETH；多周期趋势集成；CTREND 式综合信号 | `strategy_research.md` §5 |
| v2 | 资金费率套利袖子（现货多 + U 本位永续空） | `FuturesAccount`、对冲执行器、保证金风控 |
| — | 多交易所（例如 Hyperliquid） | 视需求 |

---

## 17. 风险提示（写入 README）

- 本策略是"带择时的加密市场多头敞口"：
  - 震荡市会出现连续的小幅亏损（大盘过滤反复切换）；
  - 熊市主要依靠空仓来保护本金；
  - 系统性闪崩时，所有币同涨同跌，分散化失效，日频策略来不及反应。
- 预期：
  - 在 BTC 强势上涨的阶段，绝对收益大概率**落后于** BTC 持有和定投；
  - 样本外的现实预期为年化 15%～30%、最大回撤 25%～40%，见 `strategy_review.md` §6。
- 回测表现不代表未来收益。默认参数均为预注册值，以正式研究和保留集检验的结果为准。
- 交易所、稳定币和 API Key 泄露都是回测无法体现的风险。上线前必须依次经过 testnet、demo（至少 4 周）和小资金 live。

---

## 18. Changelog：v2 → v3

| # | 变更 | 原因（对应评审编号） |
|---|---|---|
| 1 | 策略结构从"单一山寨动量轮动"改为"BTC 趋势核心 + 山寨动量卫星"双袖子，资金权重各 0.5【待研究 E7】 | 探索性回测显示，2022-10 之后山寨动量的夏普降到 0.5，而 BTC 趋势在两个半段都约为 1.4；两者组合后各子区间更均衡（review §5.4–5.5、§0） |
| 2 | 目标波动从 0.40 改为每个袖子 0.30【待研究 E4】 | 40% 的目标波动与"回撤 ≤ 30%"互相矛盾（R-01、R-19，review §6） |
| 3 | 新增 §1：目标、指标口径、基准；基准中加入"BTC 趋势"和"BTC 定投" | R-02、R-03、R-38 |
| 4 | 标的池从"绝对成交额 ≥ 2,000 万"改为"按排名取前 30，绝对下限 500 万" | R-04：2018–2020 年绝对门槛下池子里只有 2～18 个币 |
| 5 | 新增资产审核机制（`assets.yaml`：approved/excluded/patterns），新资产默认不交易 | R-05：代币化股票/ETF 上线；名称子串规则会误伤正常币 |
| 6 | 新增"交易段"概念，处理同名复用、迁移和数据缺口 | R-06 |
| 7 | 交易对筛选改用 `isSpotTradingAllowed` 和 `status == TRADING`；持仓币状态异常时冻结并告警，新增 `force_exit_assets` | R-08、R-09 |
| 8 | `momentum_skip` 从 5 改为 0【待研究 E3b】；涨幅上限的窗口改为跟随 lookback | R-11、R-13，文献（大币呈现短期动量），数据 |
| 9 | 把 tranche 重新定义为"最近 n 天每日目标的平均"，`weekly` 模式只用于研究对照；修正 `tranches = 1` 的矛盾 | R-10 |
| 10 | Risk-On 切换时默认逐步建仓（ramp），不再全量重算【待研究 E2c】 | R-18 |
| 11 | 大盘状态机加入"历史重放"初始化 | R-14 |
| 12 | 写明全部指标公式（收益类型、ddof、ATR 用 Wilder 平滑、数据完整性要求） | R-12、R-15、R-16 |
| 13 | ATR 止损改为默认关闭【待研究 E6】；止损后从所有存储目标中剔除该币，冷却期结束后不会按旧信号买回 | 数据显示止损为负贡献；R-17 |
| 14 | 新增 NAV 份额记账、出入金登记、无法解释的余额变化保护；回撤按单位净值计算 | R-21 |
| 15 | 熔断前加入数据健全性检查（数据异常时只暂停、不清仓）；`max_drawdown_halt` 在 M2 后重新设定 | R-23；样本内回撤已经到过 −44% |
| 16 | 单日异常保护写明比较时点和漏跑情况下的处理 | R-22 |
| 17 | 新增"受管资产"概念，启动时不再自动接管已有持仓；kill 只卖受管资产；建议 live 使用子账户 | R-24、R-25 |
| 18 | 持仓数量以交易所余额为准，记录手续费币种，卖出数量按 free 余额计算 | R-26 |
| 19 | 执行默认改为 LIMIT IOC，`maker_then_ioc` 推迟到 v1.1 | R-27：普通用户 maker 与 taker 费率相同（0.1%） |
| 20 | 写明下单价格、数量和估值都使用执行环境的盘口和 ticker；信号使用 live 只读行情 `data-api.binance.vision` | R-28 |
| 21 | 新的 clientOrderId 格式（不超过 36 个字符，与时钟无关，使用 symbol 哈希，重启后序号继续递增），并采用 write-ahead 意图日志 | R-29，官方文档：只保证在当前挂单中唯一 |
| 22 | 执行状态未知时：按 1/3/10/30/60 秒查询，永不盲目重下，下一次运行再核对 | R-30，官方文档：`-1007` 和 5xx 表示状态未知 |
| 23 | 删除 listenKey 和 User Data Stream（v1 不使用 WebSocket），放入后续版本 | R-31；listenKey 已于 2026-02-20 停用 |
| 24 | 技术栈：去掉 `binance-connector`，改为自研客户端；签名前先做 percent-encode；支持 Ed25519 | R-32；官方已弃用该 SDK；2026-01-15 起强制要求 percent-encode |
| 25 | 过滤器：去掉 `MIN_NOTIONAL` 主路径，新增 `PERCENT_PRICE_BY_SIDE` 校验和收窄，全部计算使用 Decimal | review §7.4 |
| 26 | 限频：限额从 exchangeInfo 读取；遇到 429/418 时整个进程退避 | review §7.6 |
| 27 | 回测：必须包含已下架币；数据源解析规则（没有表头、微秒时间戳、CHECKSUM）；下架时按 haircut 退出；两层引擎（研究引擎 + 执行回放） | R-35、R-37 |
| 28 | 研究流程：v2 的 2,160 组全量网格改为"一次只改一个变量"的预注册实验（E0–E11），并加入 DSR 和保留集守卫（PBO 见下方 v3 修订 #5） | R-36 |
| 29 | 架构：所有表加上 `strategy_id`/`account_id`；新增 `AccountType`、`InstrumentKey`、`TargetPortfolio`，预留 `FuturesAccount` 和 `Transfer` 接口；运行模式改为幂等的 `run --once` | R-40、R-41 |
| 30 | 环境说明：testnet 已有 500 多个 USDT 交易对，没有 `/sapi`，约每月重置一次；demo 环境的说明与限制 | review §7.2 |
| 31 | 开发阶段计划移到 `dev_plan.md`，新增后续版本清单和风险提示的更新 | 用户要求 |

### v3 修订（2026-11-01，M0 开工前）

| # | 变更 | 原因 |
|---|---|---|
| 1 | Telegram 只发通知、不接收命令；远程紧急停止改为 SSH 执行 `python -m bot kill`（立即执行；有运行在进行时先写 `KILL` 文件再等锁）（§5.5、§10.1、§12） | 用户决定 Q5 选 C：v1 没有常驻进程，`/kill` 无法及时响应 |
| 2 | 调仓容忍带新增 `threshold_mode: absolute / relative`（`threshold_rel` 默认 0.25），`min_trade_usdt` 两种模式都保留；研究引擎与订单计划共用 `should_skip`；默认值由 E8 决定（§6.3、§9.3、§10.1） | 核心–卫星结构下卫星单币权重只有约 3%～4%，绝对阈值 0.02 过粗 |
| 3 | 回测报告新增调仓统计（实际调仓频率、跳过原因）；E9 新增 500 / 2000 USDT 真实资金量回测（§9.3、§9.4） | 量化小资金下最小下单额和容忍带的影响 |
| 4 | 目录统一：文档全部在 `doc/`；`DECISIONS.md`、研究日志、`experiments.yaml`、`trials.csv`、`holdout_runs.log` 放在 `doc/` 或 `doc/research/`；完整报告不进 git，实验摘要复制到 `doc/research/reports/`（§3.6、§9.4、§9.5、§11.1） | 用户要求 |
| 5 | 统计工具精简为 DSR、block bootstrap、保留集检验；PBO/CSCV 移到后续版本（§9.5、§16） | 用户要求，降低 v1 工作量 |
| 6 | 新增外部心跳（M6，`notify.heartbeat_url`），`hc-ping.com` 加入允许域名（§12、§13） | 程序没运行时无法自我告警 |
| 7 | 后续版本清单新增 BNB 抵扣与 `approved_assets` 冲突的处理要求（§6.9、§16） | 用户要求 |

### v3 实现阶段修订（2026-11-01，M1～M7）

| # | 变更 | 原因 |
|---|---|---|
| 1 | §7 补充"外部基线"：账户里原有的资产记为基线，策略视角余额 = 交易所余额 − 基线；非受管资产每次刷新基线，受管资产在首次买入时冻结（D-062） | testnet 联调发现原有资产被整份计入策略持仓 |
| 2 | §5.2 无法解释的余额变化：以快照记录的"已包含的最后一笔成交"为界（D-063） | 依赖时间先后会在时钟分辨率不足时误报 |
| 3 | §9.1 执行回放的对齐口径（D-064）；验收在三个区间均达到期末差异 < 1%（0.38% / 0.05% / 0.00%） | — |
| 4 | §5.7 实盘流程未接入 ATR 止损：开启时报错（D-065） | E6 结论为关闭 |
| 5 | §13 提现权限检查已实现（`/sapi/v1/account/apiRestrictions`，缺字段即拒绝，D-061） | — |
| 6 | 研究结论：卫星回看期 28 → 21、两个袖子目标波动 0.30 → 0.35，其余保留默认；保留集检验夏普 0.17，触发"不进入实盘"规则（`doc/research/log.md`） | M3、M4 |
| 7 | §3.6、§10.1 研究窗口重新划分：第一段保留集（2025-07-01 ～ 2026-10-31）已用过，并入研究集（`backtest.end: 2026-10-31`）；`research.holdout_start` 改为 `2026-11-01`，之后的行情是新保留集；保留集运行记录按起点区分（D-085） | 用户 2026-11-02 选择 Q16 方案 B |
