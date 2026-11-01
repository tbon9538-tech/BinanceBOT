# binance-bot：币安现货趋势–动量轮动机器人

只做多、不加杠杆、不用合约的日频策略：**BTC 趋势核心（50%）+ 山寨币横截面动量卫星（50%）**，两个袖子共用 BTC 100 日均线大盘过滤，各自按波动率目标定仓，7 日平滑调仓。需求说明见 [doc/binance_momentum_bot_spec.md](doc/binance_momentum_bot_spec.md)。

> ## ⚠️ 当前结论：不建议投入实盘资金
>
> 预注册的保留集检验（2025-07-01 ～ 2026-10-31，熊市）结果为年化 +1.4%、夏普 0.17、最大回撤 −23%：跑赢了全部基准（BTC 持有同期 −20.7%），但夏普低于预先约定的 0.3，**触发"不进入实盘、回到研究"的规则**（[doc/research/log.md](doc/research/log.md) E11）。工程部分已经可以在 testnet / demo 上稳定运行；是否、何时投入真实资金由你决定。

---

## 1. 安装

需要 Python 3.13（由 [uv](https://docs.astral.sh/uv/) 管理）。

```bash
uv sync                       # 按 uv.lock 安装依赖
cp .env.example .env          # 填写 API Key（只用 testnet / demo）
uv run python -m bot --help
```

Windows 下如果 `uv` 不在 PATH 上，用 `py -3.13 -m uv` 代替 `uv`。所有命令都在项目根目录执行。

## 2. 配置

| 文件 | 内容 |
|---|---|
| `config/config.yaml` | 全部参数（spec §10.1）。研究定下的取值都有注释，见 [doc/research/proposal_v1.md](doc/research/proposal_v1.md) |
| `config/assets.yaml` | 资产审核名单：只有 `approved_assets` 中的资产能进入卫星标的池；新资产默认不交易并告警 |
| `.env` | `BINANCE_API_KEY`、`BINANCE_KEY_TYPE`（hmac / ed25519）、`BINANCE_API_SECRET` 或 `BINANCE_PRIVATE_KEY_PATH`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`。**不得提交** |

切换环境：修改 `config.yaml` 的 `env: testnet | demo | live`。testnet Key 在 testnet.binance.vision 申请，demo Key 在币安 Demo Trading 页面创建。

## 3. 数据

```bash
uv run python -m bot data sync      # 下载全部 USDT 交易对日线（含已下架），写入 data/market.db；可重复执行
uv run python -m bot data report    # 各年份覆盖情况
uv run python -m bot assets review  # 待审核资产清单与草稿（doc/assets_review/）；审核结果需人工写入 config/assets.yaml
```

历史数据来自 data.binance.vision（校验 CHECKSUM，兼容 2025 年起的微秒时间戳），最近几天用 REST 补齐。

## 4. 回测与研究

```bash
uv run python -m bot backtest --name my_run          # 当前配置，2020-01-01 ～ 2025-06-30，报告在 reports/my_run/
uv run python -m bot experiment --id E3a              # 运行 doc/research/experiments.yaml 中的预注册实验
uv run python -m bot walkforward                      # 只在目标波动与核心权重上选择的 walk-forward
uv run python -m bot bootstrap                        # 最大回撤的 block bootstrap 分布
uv run python -m bot dsr                              # Deflated Sharpe Ratio（试验数取 doc/research/trials.csv）
```

研究命令**强制过滤 2025-07-01 之后的保留集**（spec §3.6）。保留集只能用 `holdout --final --confirm <当天日期>` 运行，且已于 2026-11-01 运行过一次。研究规则见 [doc/dev_plan.md](doc/dev_plan.md) §4，过程与结论见 [doc/research/log.md](doc/research/log.md)。

## 5. testnet / demo 运行

```bash
uv run python -m bot status                          # 签名自检：余额、受管资产、系统状态
uv run python -m bot run --once --dry-run            # 只输出订单计划
uv run python -m bot run --once --force-rebalance    # 立即按目标调仓（忽略容忍带，用于联调）
uv run python -m bot run --once                      # 每日流程（同一天重复运行不会重复下单）
```

- 信号行情始终来自 live 只读接口 `data-api.binance.vision`；下单价格、数量与估值使用执行环境自己的盘口。
- 账户里原有的资产（例如 testnet 预置的币）记为**外部基线**，不会被当成策略持仓，也不会被卖出（D-062）。
- testnet 联调记录：[doc/testnet_log.md](doc/testnet_log.md)。

## 6. 部署（每天运行一次）

**Docker（推荐，云服务器）**

```bash
docker compose up -d --build        # 常驻模式：每天 rebalance.run_time_utc（默认 00:10 UTC）执行一次
docker compose logs -f
```

`data/`、`logs/`、`reports/` 挂载为卷；`config/` 只读挂载；`.env` 通过 `env_file` 注入。镜像以非 root 用户运行。

容器运行时，**所有命令都通过容器执行**（不要在主机上同时操作同一个 SQLite 状态库）：

```bash
docker compose exec bot uv run --no-sync python -m bot status     # 查看状态
docker compose exec bot uv run --no-sync python -m bot forward    # 前向周报（写到主机的 doc/forward/）
docker compose exec bot uv run --no-sync python -m bot kill       # 立即紧急停止
docker compose logs -f bot                                        # 查看日志（也写在 logs/bot.log）
```

不方便执行命令时，在主机项目目录创建 `data/KILL` 文件，常驻进程会在 60 秒内检测到并执行紧急停止。

Docker Desktop 需要设置为开机自动启动（Settings → General → Start Docker Desktop when you sign in），容器的 `restart: unless-stopped` 才会在开机后自动恢复。电脑关机或休眠期间会漏跑，漏跑的天数会出现在 `forward` 周报里。

**cron（Linux，不用常驻进程）**

```cron
10 0 * * * cd /opt/binance-bot && /usr/local/bin/uv run python -m bot run --once >> logs/cron.log 2>&1
```

**Windows 任务计划程序**：新建每日 08:10（北京时间，即 00:10 UTC）的任务，操作为在项目目录执行 `py -3.13 -m uv run python -m bot run --once`。电脑休眠、关机会漏跑，只适合测试。

## 7. 运维

| 命令 | 作用 |
|---|---|
| `python -m bot kill` | **紧急停止**：撤单、卖出全部受管资产、进入 KILLED。正在运行时先写 `KILL` 文件，最多等 120 秒。远程时 SSH 登录服务器执行 |
| 创建 `KILL` 文件 | 下一次运行开始时（或正在运行的下一笔订单前）执行同样的停止流程 |
| `python -m bot pause` / `resume --confirm` | 暂停 / 恢复交易；KILLED 后恢复需加 `--after-kill` |
| `python -m bot capital --deposit 100` / `--withdraw 50` | 登记出入金，按当时单位净值申购 / 赎回份额 |
| `python -m bot capital --acknowledge` | 确认一次"无法解释的余额变化"（例如 testnet 月度重置），作为新的核对基准 |
| `python -m bot adopt SOL` | 把账户中已有的资产纳入策略管理 |

风控自动动作：单日净值下跌 ≥ 15% 当天禁止买入；单位净值回撤 ≥ 40% 熔断清仓（数据异常时只暂停不清仓）；无法解释的余额变化当天禁止买入。

## 8. 监控

- **Telegram**：只发通知（日报、大盘切换、告警），**不接收任何命令**（v1 决定，见 doc/DECISIONS.md D-006）。在 `.env` 中配置 token 与 chat_id；未配置时只写日志。
- **外部心跳**：在 [healthchecks.io](https://healthchecks.io) 建一个周期 1 天、宽限 2 小时的检查，把 ping 地址（`https://hc-ping.com/<uuid>`）填入 `notify.heartbeat_url`。程序没有运行时由它告警。
- **日志**：`logs/bot.log`，按天滚动、保留 90 天，密钥自动打码。

## 9. 故障演练与执行回放

```bash
uv run python scripts/drills/run_drills.py                    # 5 项离线演练：进程崩溃、下单超时、KILL、断网、余额重置
uv run python scripts/replay_check.py 2024-10-01 2024-12-31   # 用真实 run_once 流程逐日回放，与研究引擎对比（差异 < 1%）
```

testnet 上的演练步骤：`run --once --force-rebalance` → 立即再运行一次（应为零订单）→ `kill` → `resume --confirm --after-kill`，记录见 [doc/testnet_log.md](doc/testnet_log.md)。

## 10. 上线检查清单（在你决定上线之前）

1. 策略：重新评估 [doc/research/log.md](doc/research/log.md) 的 E11 结论；保留集未通过，建议先用 demo 做至少 4 周前向观察。
2. 账户：使用**专用子账户**；API Key 只开"读取"和"现货交易"，**禁止提现**，绑定服务器 IP 白名单；推荐 Ed25519 Key。
3. 资金：`max_capital_usdt` 显式设置；首次 ≤ 500 USDT，运行满 8 周、对账无差错后再考虑加仓。
4. 启动：`env: live` 时必须加 `--live-confirm=I_UNDERSTAND`，并在控制台输入 YES（Docker 用 `--yes`）；程序会检查 Key 是否开启提现，开启则拒绝启动。
5. 监控：Telegram 与心跳都已配置并验证。
6. 演练：在 demo 上至少完成一次 kill 与恢复。

## 11. 风险提示

本策略是"带择时的加密市场多头敞口"：震荡市会连续小亏（大盘过滤反复切换），熊市靠空仓保护本金，系统性闪崩时所有币同涨同跌、日频策略来不及反应。在 BTC 强势上涨阶段，绝对收益大概率落后于 BTC 持有和定投。样本外现实预期：年化 20%～35%、最大回撤 30%～40%（[doc/research/proposal_v1.md](doc/research/proposal_v1.md)）。回测不代表未来；交易所、稳定币与 API Key 泄露都是回测无法体现的风险。

## 12. 开发

```bash
.\scripts\check.ps1     # Windows：ruff、格式、mypy、pytest
scripts/check.sh        # Linux / WSL
```

- 单元测试禁止访问网络（pytest-socket）；HTTP 一律用 MockTransport。
- 项目约定见 [CLAUDE.md](CLAUDE.md)；设计与技术决策见 [doc/DECISIONS.md](doc/DECISIONS.md)、[doc/decisions_m5.md](doc/decisions_m5.md)。

## 13. 文档索引

| 文档 | 内容 |
|---|---|
| [doc/strategy_review.md](doc/strategy_review.md) | v2 策略评审、文献、接口核对 |
| [doc/strategy_research.md](doc/strategy_research.md) | 研究方案与实验设计 |
| [doc/binance_momentum_bot_spec.md](doc/binance_momentum_bot_spec.md) | 需求说明 v3（含修订记录） |
| [doc/dev_plan.md](doc/dev_plan.md) | 里程碑、验收标准、防过拟合规则 |
| [doc/open_questions.md](doc/open_questions.md) | 决策记录与待定事项 |
| [doc/research/](doc/research/) | 研究日志、实验定义、试验记录、参数提案、各实验摘要 |
