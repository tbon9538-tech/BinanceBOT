# 更新记录

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。
决策编号（D-xxx）见 `doc/DECISIONS.md`，实验编号（Exx、Rx）见 `doc/research/log.md`。

## [未发布]

### 新增
- 每次运行拉取实际费率（高于回测假设时提示）并登记 `strategies` 表；回测报告增加"扣除 USDT 理财收益"的参考夏普（D-083）。
- 漏跑告警 `MISSED_RUN`：最近一次成功运行距今超过 30 小时时告警，运行开始时与常驻模式每小时检查（D-081）。
- 数据质量检查（spec §3.3）：大盘标的近 `sma_period` 天缺有效日线时中止运行；不合规的日线剔除；单日变化超过 60% 记 WARN（D-081）。
- 大盘状态重放核对 `REGIME_REPLAY_MISMATCH`（spec §4.2）（D-081）。
- 日报补齐当日收益、核心 / 卫星仓位、成交额、手续费、灰尘（D-081）。
- 管理界面改版：顶栏状态与分区导航、指标卡（日变化、距均线）、可交互图表（十字准线、时间范围）、持仓"目标 vs 实际"对比、按状态显示的操作与确认框、操作结果提示、记录页签与筛选、浅色 / 深色切换；实时持仓异步加载；常驻进程存活标记，界面显示定时运行是否在进行（D-080）。
- 管理界面 `python -m bot dashboard`：净值与回撤、大盘、实时持仓、目标权重、运行记录、事件、成交；可暂停、恢复、立即运行一次、确认余额、紧急停止。登录密码 `DASHBOARD_PASSWORD`，CSRF 与确认词保护，每次操作留审计事件；docker-compose 新增 `dashboard` 服务（只开放本机端口）（D-075）。
- 云服务器部署说明 `doc/deploy_cloud.md`（Tailscale 访问）（D-076）。
- 下单前检查 `MAX_POSITION` 过滤器：按整个账户（含外部基线）的持仓计算，超出上限的买单在下单前放弃（D-073）。
- 过滤器不通过的订单写 WARN 日志与 `FILTER_REJECTED` 事件；日报的买单一行列出跳过原因计数（D-074）。
- 日报在 `regime.mode: ensemble` 时显示集成仓位乘数。
- `forward` 命令的单元测试。

### 研究
- R10：资金费率套利袖子与组合的研究回测（`bot/backtest/carry.py`、`bot/data/futures.py`、`scripts/research_r10.py`），只做研究，不涉及交易代码；未通过预注册标准（D-088）。
- R7：回测引擎支持闲置 USDT 计息（`backtest.cash_yield_annual`，默认 0）；按 4% 估算年化 +3.3 个百分点，demo 不支持理财接口（D-087）。
- 研究窗口重新划分（Q16，D-085）：第一段保留集并入研究集（2020-01 ～ 2026-10），新保留集从 2026-11-01 开始；报告新增段 A / 段 B 指标（D-086）。
- R8：卫星新增山寨季过滤 `alt_season_lookback`，默认不启用。新研究集上未通过预注册标准（Calmar 均值 1.19 对照 1.45，两段都变差），配置不变（D-086）。
- R4：核心袖子新增单资产趋势过滤 `asset_filter: own_sma`，默认不启用。核心 BTC + ETH 在研究集上未通过预注册标准（Calmar 均值 1.70 对照 1.75），配置不变（D-079）。
- R3：卫星选币新增 `score: ctrend`（CTREND 式价格 + 成交量信号，滚动横截面回归合成），默认不启用。研究集上未通过预注册标准（Calmar 均值 1.17 对照 1.67），配置不变（D-077）。

### 修复
- 待审核资产提醒（`ASSET_REVIEW_NEEDED`）原来从不触发：判断时要求资产在行情面板中，而面板只加载已批准资产。现在与已批准资产一起排名，进入标的池范围的写入 `asset_review` 表，逐资产 7 天内最多提醒一次（D-083）。
- 持仓币停止交易：冻结状态持久化，恢复交易后自动卖出再解冻；`force_exit_assets` 在流动性排名阶段排除（D-083）。
- 告警推送：订单状态未知、订单未完成、对账差额超过 1 USDT、无法解释的余额变化、单日异常、持仓币停止交易等 WARN / CRITICAL 告警原来只写进事件表，现在同时推送通知（spec §12，D-081）。
- `forward` 周报：同一天有多个 PRE_TRADE 快照（例如手动补跑）时，原来取的是最后一个而不是第一个，累计收益与相关系数会算错。
- `forward` 周报：`--since` 之后没有快照时不再报错；报告文件名按 UTC 日期。

### 变更
- 管理界面登录校验用户名（`DASHBOARD_USERNAME`，默认 admin）；密码最短 demo / testnet 8 位、live 12 位（D-078）。
- 暂停、恢复、紧急停止、确认余额的核心逻辑移到 `bot/app/ops.py`，命令行与管理界面共用。
- pytest 允许 Unix 域套接字（`--allow-unix-socket`），网络访问仍然禁止。
- `tests/test_run_once.py` 改用 `bot/backtest/sim_env.py` 的模拟环境，去掉重复的构造代码。

## [1.0.0] - 2026-11-02

第一个完整版本：币安现货"BTC 趋势核心 + 山寨动量卫星"轮动机器人。只做多、不加杠杆、不用合约。

> **策略没有通过保留集检验（E11，夏普 0.17 < 0.3），不建议投入真实资金。**
> 1.0.0 表示软件功能完整、能在 testnet / demo 上稳定运行，不表示策略可以上实盘。是否上实盘由用户在检查点 C4 决定。

### 新增

**数据（M1）**
- 从 data.binance.vision 下载日线并校验 CHECKSUM；REST 每天增量补齐；行情缓存 `data/market.db`（775 个 USDT 交易对，含 255 个已下架，2018-01 起）。
- 交易段切分：相邻日线间隔超过 7 天即切段（例如旧币下架后同名新币上市），指标只在段内计算（D-039）；保留集守卫：研究命令读不到 2025-07-01 及之后的数据。
- 资产审核：`config/assets.yaml` 的 approved 名单只能人工编辑；"疑似股票/ETF""疑似稳定币"辅助标记（D-048、D-058）。

**策略与研究引擎（M2、M3）**
- 纯函数策略：BTC 大盘过滤（SMA100 ± 2% 缓冲，Risk-On 逐步建仓）、核心 BTC 趋势袖子、卫星横截面动量袖子（回看 21 天、ret/vol 得分、前 5 等权、单币上限 0.25）、每个袖子目标波动 0.35、7 天平滑、相对容忍带 0.25。
- 研究引擎与实盘共用 `should_skip` 等调仓函数；报告、DSR、block bootstrap、walk-forward、基准对比。
- 实验 E0～E10（107 个试验）与参数提案 v1（`doc/research/proposal_v1.md`）：研究集 2020-01 ～ 2025-06 年化 50.3%、夏普 1.68、最大回撤 −26.9%。

**保留集检验（M4）**
- `holdout --final` 一次性检验命令，记录每次运行；测试中用替身隔离，不会被误触发。

**交易与执行（M5）**
- 币安现货签名客户端（HMAC 与 Ed25519，先 percent-encode 再签名）、交易规则与过滤器、限频处理。
- 订单计划（先卖后买）、执行器（write-ahead 订单意图、clientOrderId 规则、状态未知时只查询不换号重下、对账）。
- 状态库（SQLite，WAL）：持仓、外部基线、净值快照、订单意图、成交、事件；带迁移。
- 每日流程 `run --once` 与常驻模式 `run --daemon`（每天 00:10 UTC）；运维命令 `status`、`kill`、`pause`、`resume`、`capital`、`adopt`。
- live 启动需要显式确认，并检查 API Key 没有开提现权限（D-061）；仓库自带配置只允许 testnet / demo。

**风控、通知与验证（M6）**
- NAV 份额记账；单日下跌 ≥ 15% 当天禁止买入，单位净值回撤 ≥ 40% 熔断清仓，数据异常只暂停不清仓。
- 紧急停止：`kill` 命令或 KILL 文件（撤单 + 清仓受管资产）。
- Telegram 通知（只发不收，D-006）、外部心跳（healthchecks.io）、日志密钥打码。
- 执行回放：用真实 `run_once` 流程逐日回放，三个区间与研究引擎差异 ≤ 0.4%；5 项离线故障演练（进程崩溃、下单超时、KILL、断网、余额重置）。

**部署与运维（M7 及之后）**
- Docker 镜像（非 root、数据卷）与 docker-compose 常驻运行；KILL 文件路径可配置并挂载到主机（D-070）。
- 状态库按环境拆分为 `data/state-<env>.db`（D-067）。
- 前向观察周报 `forward`：漏跑、实际手续费率、成交偏移、与研究引擎同期模拟的对比（D-068）。
- 状态库自动备份：每次运行正常结束后在线备份到 `data/backup/`，保留 30 份（D-072）。
- `status` 显示 Telegram / 心跳是否已配置、备份份数。

**研究框架**
- `regime.mode: ensemble`：多周期均线集成大盘过滤（R2，D-069）。

### 变更
- 心跳地址从 `config.yaml` 的 `notify.heartbeat_url` 移到 `.env` 的 `HEARTBEAT_URL`，配置里只保留开关 `notify.heartbeat_enabled`；旧配置项会报错并提示新位置（D-071）。

### 修复
- **D-062 账户原有资产**：testnet 联调发现，首次买入后账户里原有的整份余额被当成策略持仓。新增外部基线（迁移 v002）与 `StrategyAccountView`，策略只看到"交易所余额 − 基线"。
- **D-063 余额核对误报**：核对依赖"快照时间晚于成交时间"，时钟静止时误报"无法解释的余额变化"并禁止买入（执行回放发现）。改为快照记录 `fills_through`。
- `.gitignore` 的 `data/` 误伤 `bot/data/`，改为只忽略根目录（D-060）。

### 研究结论（不改代码，记录在案）
- **E11 保留集**（2025-07 ～ 2026-10，只运行一次，不得再运行）：年化 +1.4%、夏普 0.17、最大回撤 −23.0%，跑赢全部基准（BTC 持有年化 −20.7%），但触发预注册退化规则，策略不进入实盘。
- **R2 多周期均线集成**：更稳健，但平均水平不高于典型单一均线，未通过预注册标准；默认仍为 SMA100 / 2%。
- 试验累计 121 个（`doc/research/trials.csv`）。

### 已知限制
- 下单前不检查 `MAX_POSITION` 过滤器；`forward` 命令没有单元测试；日报不显示 ensemble 模式的仓位乘数（ROADMAP C5）。
- live 路径中 `apiRestrictions` 的字段名未用 live Key 验证（D-061）。
- Ed25519 签名尚未在 testnet 实测（ROADMAP C4'）。

[未发布]: https://github.com/tbon9538-tech/BinanceBOT/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/tbon9538-tech/BinanceBOT/releases/tag/v1.0.0
