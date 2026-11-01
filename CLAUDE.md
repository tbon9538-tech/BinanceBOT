# CLAUDE.md

> **交接**：新会话请先读 `doc/HANDOFF.md`（现状、运行中的 demo 容器、规则、踩过的坑、待办），再看 `doc/ROADMAP.md`（接下来的阶段、退出条件、用户决策点）。

币安现货趋势–动量轮动机器人（核心–卫星：BTC 趋势核心 + 山寨动量卫星）。只做多、不加杠杆、不用合约。

## 先读这些文档（全部在 `doc/`）

| 文档 | 内容 |
|---|---|
| `doc/binance_momentum_bot_spec.md` | 需求说明 v3，实现以它为准。开头的"给 Claude Code 的说明"必须遵守 |
| `doc/dev_plan.md` | 里程碑 M0～M7、任务验收标准、检查点 C0～C4、防过拟合规则 |
| `doc/DECISIONS.md` | 已确认的决策和技术选择；spec 没覆盖的细节做了保守选择后也记在这里 |
| `doc/open_questions.md` | 待决问题及决策记录 |
| `doc/strategy_research.md` | 研究方案与实验 E0～E11 |

## 当前进度（2026-11-01）

- M0～M7 的代码与文档全部完成，进度表见 `doc/dev_plan.md` 开头。
- 研究：参数提案 `doc/research/proposal_v1.md`；保留集已运行一次（**不得再运行**），夏普 0.17，触发"不进入实盘"规则。
- testnet 联调通过（`doc/testnet_log.md`）。Q14 已定：不投入实盘，demo 前向观察 + 新研究。
- R2（多周期均线集成）、R3（CTREND 式选币信号）已完成，都未通过预注册标准，配置不变。新研究必须先在 `doc/research/log.md` 预注册；保留集不得再运行。
- 状态库按环境拆分：`data/state-<env>.db`；demo 周报：`python -m bot forward`。

## 常用命令（在项目根目录执行）

```bash
uv sync                                   # 按 uv.lock 安装依赖（Python 版本见 .python-version）
uv run pytest                             # 单元测试（默认禁止网络访问）
uv run ruff check . && uv run ruff format --check .
uv run mypy                               # 类型检查（strict，只检查 bot/）
uv run python -m bot --help               # 命令行
```

一条命令跑全部检查：Windows 用 `.\scripts\check.ps1`，Linux / WSL 用 `scripts/check.sh`。
如果 `uv` 不在 PATH 上，用 `py -3.13 -m uv` 代替 `uv`。

## 目录

```
bot/            代码（结构见 spec §11.1）
  config.py     配置模型与校验（pydantic，extra="forbid"）
  domain/       纯数据类型与异常
  exchange/     base.py 为 Protocol；binance/ 为实现（只有 app/ 和 exchange/ 可以 import）
  strategy/ risk/ execution/ persistence/ notify/ backtest/ app/   各里程碑逐步填充
config/         config.yaml（全部参数）、assets.yaml（资产审核名单，只能人工编辑 approved）
tests/          pytest
scripts/        check.ps1 / check.sh
doc/            全部文档；doc/research/ 放研究日志、实验定义、trials.csv、实验摘要
reports/        完整回测报告（不进 git）
data/ logs/     运行时数据与日志（不进 git）
```

## 硬性约束

1. **只用 testnet / demo**。不得以 `env: live` 运行任何会下单的代码，不得索要 live Key。
2. **参数一律放在 `config/config.yaml`**，代码中不得硬编码。标【待研究 Exx】的参数由实验决定，开发者不得自行调参。
3. **保留集纪律**：研究命令不得读取 `research.holdout_start`（2025-07-01）及之后的数据；保留集只在检查点 C1 之后跑一次。做参数决定时不受对 2025-07 之后行情印象的影响。
4. **检查点必须停下来**等用户确认（dev_plan §7）。发现 spec 矛盾、回测结果异常（Sharpe > 3、曲线异常平滑）、需要访问未列出的外部域名时，也要停下来汇报。
5. 价格、数量、金额用 `Decimal`；时间一律 UTC。
6. 单元测试不得访问网络（pytest-socket `--disable-socket`），HTTP 用 `httpx.MockTransport`。
7. 下单状态未知时绝不换新 clientOrderId 重下同一意图（spec §6.6）。
8. 策略、风控、研究引擎不得 import `bot.exchange.binance`。
9. 每个里程碑结束：检查全部通过 → git commit → 用中文汇报。spec 没覆盖的细节记入 `doc/DECISIONS.md`。
10. ruff 不检查 `doc/` 和 `*.md`（新版 ruff 会格式化 Markdown 里的 Python 代码块，会改动文档）。
