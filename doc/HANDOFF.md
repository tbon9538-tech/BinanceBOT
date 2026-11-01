# 交接文档（2026-11-02）

> 写给接手的 Claude Code 会话。先读完本文件，再读 `CLAUDE.md`（Claude Code 会自动加载），然后按"第 2 节"的顺序读文档。
> 与用户沟通一律用**中文**。

---

## 1. 一句话现状

币安现货"BTC 趋势核心 + 山寨动量卫星"机器人，**代码与文档 M0～M7 全部完成**（335 项测试通过）。工程上可以在 testnet / demo 稳定运行，**但策略没有通过保留集检验，不应投入真实资金**。目前在用户本机（macOS）用 Docker 常驻跑 **demo 前向观察**（2026-11-01 开始；2026-11-01 16:49 UTC 从原来的 Windows 电脑迁到本机，原电脑的容器已删除）。

| 项目 | 状态 |
|---|---|
| 代码仓库 | `/Users/yeto/Documents/BinanceBOT`（macOS），分支 `master`，远端 `origin` = https://github.com/tbon9538-tech/BinanceBOT （**公开仓库**），没有 tag |
| 运行中 | **本机** Docker 容器 `binancebot-bot-1`（`docker compose up -d`，`run --daemon`），每天 00:10 UTC 运行一次，`env: demo`。这是该 demo 账户**唯一**的运行处 |
| 未提交改动 | 无（`.dockerignore` 排除 `dist` 已经用户同意并提交） |
| 研究 | E0～E11 完成；保留集**已运行过一次，不得再运行**；R2（多周期均线集成）未通过；试验累计 121 个 |
| 待用户决定 | Q6 服务器地区、Q12 正式资金规模、C4（demo 观察满 4 周后是否上实盘） |

---

## 2. 文档阅读顺序

1. `CLAUDE.md`：项目约定、常用命令、硬性约束。
2. `README.md`：使用说明（安装、研究、testnet/demo、Docker、运维、监控、上线清单）。
3. `doc/ROADMAP.md`：**接下来怎么走**（阶段 A～D、研究线 R、功能线 F、阶段 2、用户决策点）。
4. `doc/dev_plan.md`：开头的**进度表**、检查点 C0～C4、§4 防过拟合规则。
5. `doc/open_questions.md`：顶部的**决策记录表**（Q0～Q14 都已决定，Q6 地区和 Q12 资金规模部分待定）。
6. `doc/DECISIONS.md`（D-001～D-070）与 `doc/decisions_m5.md`（M5-D01～D47，执行层细节）。
7. `doc/binance_momentum_bot_spec.md`：需求说明 v3；文末有"v3 修订"和"v3 实现阶段修订"两张表。
8. `doc/research/log.md`：研究日志，包括全部预注册、结论，以及 E11 保留集和 R2 结果。
9. `doc/testnet_log.md`、`doc/demo_log.md`：联调与前向观察记录。
10. 背景（需要时再看）：`doc/strategy_review.md`（v2 评审、币安接口核对）、`doc/strategy_research.md`（研究方案）。

---

## 3. 环境与命令

- macOS（Apple Silicon），shell 为 zsh。Python 3.13 由 uv 管理；`uv` 用 Homebrew 安装（`/opt/homebrew/bin/uv`），在 PATH 上。
- **`docker` 不在 PATH 上**：Docker Desktop 的命令行在 `/Applications/Docker.app/Contents/Resources/bin`，执行 docker 命令前先 `export PATH=/Applications/Docker.app/Contents/Resources/bin:$PATH`。
- 定时运行依赖电脑在 00:10 UTC（北京时间 08:10）**不休眠**、Docker Desktop 在运行；休眠期间错过的运行会在唤醒后补跑一次（daemon 按墙上时钟判断）。

```bash
uv sync
uv run pytest                                     # 单元测试（pytest-socket 禁止联网）
scripts/check.sh                                  # ruff + 格式 + mypy + pytest，一次跑完
uv run python -m bot --help
```

（原来的 Windows 电脑上用 `py -3.13 -m uv ...` 和 `.\scripts\check.ps1`。）

**容器运行时，所有 bot 命令都通过容器执行**，不要在主机上同时写同一个 SQLite 状态库：

```bash
docker compose exec bot uv run --no-sync python -m bot status
docker compose exec bot uv run --no-sync python -m bot forward     # 前向周报，写到主机 doc/forward/
docker compose exec bot uv run --no-sync python -m bot kill        # 紧急停止（或在主机创建 data/KILL）
docker compose logs -f bot
```

修改代码后要让容器生效：`docker compose up -d --build`。

---

## 4. 运行时数据与密钥（都不在 git 中）

| 路径 | 内容 |
|---|---|
| `.env` | **当前生效的是 demo Key**（HMAC）；testnet Key 以 `# testnet:` 注释形式保留。两套 Key 都曾在聊天中明文发送，**建议用户重新生成**。不要在回复里复述 Key |
| `data/market.db` | 行情缓存：775 个 USDT 交易对（含 255 个已下架），2018-01 起；容器每天通过 REST 增量更新 |
| `data/state-demo.db` | demo 状态库（受管持仓、外部基线、快照、订单意图、成交） |
| `logs/bot.log` | 运行日志（从原电脑搬来，接着写） |

本机只搬来了上面几项。原 Windows 电脑上还有 `data/raw/`（data.binance.vision 原始 zip）、`data/state-testnet.db`（testnet 状态库及 D-062 之前的备份）、`reports/`（完整回测报告，摘要已在 `doc/research/reports/`）和旧部署包 `dist/binance-bot-0.1.0-21f7e18/`，**本机没有**；做 testnet 联调或重跑研究前需要另行复制或重新生成。

账户情况：
- demo：5000 USDT + 5000 USDC 虚拟资金。USDC 记为外部基线；策略资金上限 500 USDT；已持有少量 BTC、INJ、PEPE。
- testnet：预置了大量资产，全部记为外部基线；kill 演练后策略持仓为空，系统状态 RUNNING。

从 GitHub 克隆接手时：克隆下来的只有代码和文档，`.env` 和 `data/` 都不在仓库里。2026-11-02 已验证：全新克隆后 `uv sync` + `check.ps1` 全部通过（ruff、mypy、335 项测试），不依赖任何未入库的文件；保留集守卫读取的 `doc/research/holdout_runs.log` 和试验记录 `trials.csv` 都在仓库里。

- **只做开发**（推荐）：`git clone` 后执行 `uv sync` 和检查即可，单元测试不需要 `.env` 和 `data/`。demo 容器继续在本机（macOS）运行；代码推到 GitHub 后，在本机 `git pull` 再 `docker compose up -d --build`。
- **要把 demo 再搬到别的电脑**：从本机复制 `.env`（或按 `.env.example` 重新填写）和 `data/state-demo.db`；行情库可以复制 `data/market.db`，也可以用 `python -m bot data sync` 重新下载。**同一时间只能有一处运行容器**：先在本机执行 `docker compose down`，再在新电脑执行 `docker compose up -d --build`，否则两处会对同一个 demo 账户重复下单。不带 `state-demo.db` 直接启动，账户里已有的 BTC/INJ/PEPE 会被当成外部资产，与旧的状态库对不上。2026-11-01 从 Windows 迁到本机时就是按这个流程做的，迁移后 `status` 与原电脑一致。

仓库是**公开**的：推送前确认没有 Key、`.env`、`data/` 混进提交（2026-11-01 推送前已扫描全部历史，没有发现 Key；`tests/test_signing.py` 中的 64 位字符串是币安文档里公开的示例 Key）。

---

## 5. 代码地图

```
bot/cli.py                 子命令注册（HANDLERS），保留集守卫在 _holdout_checks
bot/config.py              pydantic 配置（extra=forbid）；域名白名单；v1 拒绝 maker_then_ioc
bot/data/                  vision 下载、market.db、交易段切分、保留集守卫（ResearchMarketView）
bot/strategy/              指标面板（按交易段拆列）、大盘状态机（sma/tsmom/ensemble）、袖子 D_t、平滑 S_t、组合 W_t、
                           should_skip（研究与实盘共用）、资产审核规则
bot/backtest/              研究引擎、报告、统计（DSR/bootstrap）、实验框架、基准、SimSpotAccount、执行回放、sim_env
bot/exchange/binance/      HTTP/签名（先 percent-encode）、过滤器、SpotAccount、只读行情
bot/execution/             订单计划（先卖后买）、执行器（write-ahead 意图、未知状态处理、对账）、clientOrderId
bot/persistence/           state 库模型、仓库、迁移（v001 初始、v002 外部基线）
bot/risk/                  NAV 份额、风控判定、ATR 止损（默认关闭）
bot/app/                   run_once（每日流程）、account_view（外部基线视图）、daemon、运维命令、数据/研究/前向命令
bot/notify/                Telegram（只发不收）、心跳
scripts/                   check.ps1/.sh、drills/run_drills.py（5 项离线演练）、replay_check.py（执行回放）
```

---

## 6. 必须遵守的规则

1. **绝不碰实盘**：不以 `env: live` 运行任何会下单的代码，不索要 live Key。仓库自带的 config 只允许 testnet/demo（有测试守着）。
2. **保留集已用过**：`holdout --final` **不得再运行**，测试中也已经用替身隔离。新方案的样本外检验只能用 demo 前向数据（Q10、Q14）。
3. **研究纪律**：任何新研究**先在 `doc/research/log.md` 预注册**（假设、网格、通过标准），再运行；试验会自动记入 `trials.csv`；不能用 2025-07 之后行情的印象作为理由（D-018）；只用研究集（≤ 2025-06-30）。
4. 参数只放在 `config/config.yaml`；`config/assets.yaml` 的 approved 名单只能人工审核后修改（D-059 是用户授权的一次例外）。
5. 用户曾授权"不需要经过我同意"连续开发，但实盘、资金、地区等决定仍属于用户。
6. 提交规范：中文提交信息，每个里程碑一次提交，结尾加会话系统提示给出的 Co-Authored-By 署名。

---

## 7. 踩过的坑（务必注意）

| 坑 | 后果 | 做法 |
|---|---|---|
| `git add -A` | 曾把别人生成的 317 MB `dist/` 和 `.claude/worktrees` 子仓库提交进去 | **只 add 明确的路径**，提交前先看 `git status` |
| 在 Bash heredoc 里写含反斜杠的 Python 代码 | 反斜杠被转义，`\x00` 变成真实的 NUL 字节，导致模块无法导入 | 含反斜杠的代码用 Write / Edit 工具写，不要用 heredoc |
| 新版 ruff 会格式化 Markdown 中的 Python 代码块 | 曾改动 spec 示例 | `pyproject.toml` 已排除 `doc/` 与 `*.md` |
| `.gitignore` 写成 `data/` | 误伤 `bot/data/`，该模块曾长期未进 git | 已改为 `/data/`、`/logs/`、`/reports/`、`/dist/` |
| 余额核对依赖"快照时间晚于成交时间" | 时钟静止或分辨率不足时误报"无法解释的余额变化"并禁止买入 | 已改为快照记录 `fills_through`（D-063） |
| 账户原有资产 | 首次买入后整份余额被当成策略持仓 | 外部基线 + `StrategyAccountView`（D-062） |
| 测试中真的运行保留集或 kill | 会消耗保留集、在项目根目录留下 KILL 文件 | CLI 测试用替身；不要写会触发真实 holdout/kill 的测试 |
| Windows 上 SQLite 文件被占用 | `TemporaryDirectory` 清理失败 | 使用 `ignore_cleanup_errors=True` |
| pandas-stubs 类型过宽 | mypy 报大量无关错误 | 对 `bot.strategy.*`、`bot.backtest.*` 等关闭若干错误码（D-051）；核心交易模块保持完整 strict |
| testnet 每月重置 | 余额被外部改写，当天禁止买入 | 用 `capital --acknowledge` 确认后恢复 |

---

## 8. 待办（按优先级）

> 完整路线图（阶段、退出条件、依赖、用户决策点）见 `doc/ROADMAP.md`；本节是其中近期部分的摘要。

**P0：监控与数据安全（目前没有任何告警）**

1. 配置 Telegram：需要用户提供 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`，写入 `.env`。
2. 配置心跳：需要用户在 healthchecks.io 创建检查，把 `https://hc-ping.com/<uuid>` 填入 `notify.heartbeat_url`。
3. 状态库自动备份：建议每次运行成功后，把 `data/state-<env>.db` 用 SQLite backup API 复制到 `data/backup/`，保留 30 份。尚未实现。
4. ~~代码异地备份~~：已推送到 https://github.com/tbon9538-tech/BinanceBOT （公开仓库，用户指定）。

**P1：demo 观察与收尾**

5. 每周运行 `forward` 周报，把结果追加到 `doc/demo_log.md` 的周表；满 4 周、对账无差错后到达检查点 C4，向用户汇报。
6. 打版本 tag（例如 `v1.0.0`），写 `CHANGELOG.md`；如有需要，按当前提交重新生成部署包。
7. ~~问用户如何处理 `.dockerignore` 的未提交改动~~：已提交（aa0b79e）。
8. 用户在 testnet 注册 Ed25519 公钥后，实测 Ed25519 签名（代码已支持：`BINANCE_KEY_TYPE=ed25519`、`BINANCE_PRIVATE_KEY_PATH`）。

**P2：只在用户决定之后**

9. 实盘路径验证：`/sapi/v1/account/apiRestrictions` 的字段名未用 live Key 验证（D-061），只能由用户提供只读 Key 或自行验证。
10. 云服务器部署（Q6 地区待定）：同一套 docker-compose 即可，注意 IP 白名单。
11. 研究候选（`doc/strategy_research.md` §5）：CTREND 式综合信号、核心资产扩展到 BTC+ETH、USDT 理财等。每一项都必须先预注册。

**已知小缺口**

- 下单前不检查 `MAX_POSITION` 过滤器（交易所会拒单）。
- `forward` 命令没有单元测试。
- `tests/test_run_once.py` 与 `bot/backtest/sim_env.py` 有重复的构造代码，可以合并。
- 日报没有显示 ensemble 模式下的仓位乘数（当前配置未使用 ensemble，不影响运行）。

---

## 9. 接手后的快速自检

```bash
git status && git log --oneline -3
scripts/check.sh
export PATH=/Applications/Docker.app/Contents/Resources/bin:$PATH
docker compose ps
docker compose exec bot uv run --no-sync python -m bot status
docker compose logs --tail 20 bot
```

预期：335 项测试通过；容器状态为 Up；`status` 显示 `环境 demo`、系统状态 RUNNING、受管资产 BTC/INJ/PEPE/USDT。
