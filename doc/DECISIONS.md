# 决策记录

> 格式：编号、日期、问题、选择、理由。策略与产品层面的决策来自 `open_questions.md`（由用户确认）；技术决策由开发者做出，记录在这里。
> spec 没有覆盖的细节按"更保守、更安全"的原则处理，也记录在这里（spec 开头说明第 6 条）。

## 一、用户确认的决策（2026-11-01）

| 编号 | 问题 | 选择 | 理由 / 备注 |
|---|---|---|---|
| D-001 | Q0 第一版策略结构 | 核心–卫星：BTC 趋势核心 + 山寨动量卫星，各 50%（E7 在 {0.3, 0.5, 0.7} 中确认） | 分散化；纯山寨动量在 2022-10 之后明显衰减（`strategy_review.md` §5） |
| D-002 | Q1 收益与风险目标 | 风险为硬约束、收益为期望（spec §1.1） | 原目标在只做多现货约束下不能同时达成 |
| D-003 | Q2 实盘账户形态 | live 使用专用子账户 | 对账最简单，不会误卖其他资产 |
| D-004 | Q3 下单方式 | v1 只用 LIMIT IOC；`maker_then_ioc` 放 v1.1 | 普通用户 maker、taker 费率相同；挂单逻辑最容易出 bug |
| D-005 | Q4 交易所客户端 | 自研轻量 REST 客户端（HMAC + Ed25519），不用 `binance-sdk-spot` | 只需约 10 个接口；精确控制超时、未知状态和限频；便于 mock |
| D-006 | Q5 Telegram 命令 | **C**：只发通知，不接收任何命令 | v1 没有常驻进程，`/kill` 无法及时响应；紧急停止用 SSH 执行 `python -m bot kill` 或创建 `KILL` 文件 |
| D-007 | Q6 部署位置 | 云服务器 + Docker；服务器地区待定（M7 之前确定） | 本地电脑断电、休眠会漏跑 |
| D-008 | Q7 空仓 USDT | v1 留在现货账户；理财放 v1.2，先做接口验证 | 降低故障面 |
| D-009 | Q8 BNB 抵扣 | v1 关闭；上 live 前再评估。若开启，须先把 BNB 从卫星 `approved_assets` 移除，或分开记账 | 账务最简单 |
| D-010 | Q9 新币审核 | 规则排除 + 人工审核白名单，新资产默认不交易 | exchangeInfo 无资产类别字段；代币化股票已上线 |
| D-011 | Q10 保留集 | 2025-07-01 至 M2 结束时的最新数据；代码强制禁读；定参后只跑一次，跑完不改参数 | 防止过拟合 |
| D-012 | Q11 ATR 止损 | 实现但默认关闭，由 E6 决定 | 初步数据显示为负贡献 |
| D-013 | Q12 起始资金 | 先用 ≤ 500 USDT 验证系统；正式资金另定 | 小资金影响由 E9 量化 |

## 二、开工前的 spec 修订（2026-11-01，用户要求）

| 编号 | 问题 | 选择 | 理由 |
|---|---|---|---|
| D-014 | 调仓容忍带 | 新增 `rebalance.threshold_mode: absolute \| relative`；relative 模式下 \|W − cw\| < `threshold_rel` × max(W, cw) 时跳过（`threshold_rel` 默认 0.25）；`min_trade_usdt` 两种模式都保留；研究引擎与订单计划共用 `bot/strategy/rebalance.py::should_skip`；**暂定默认 relative**，最终由 E8 决定 | 卫星单币目标权重只有约 3%～4%，绝对阈值 0.02 过粗 |
| D-015 | 目录约定 | 文档全部放在 `doc/`；研究日志、`experiments.yaml`、`trials.csv`、`holdout_runs.log`、`proposal_v1.md` 放在 `doc/research/`；完整报告 `reports/` 不进 git，每个实验的 `summary.md` 与关键图表复制到 `doc/research/reports/<编号>/` 并提交 | 用户要求 |
| D-016 | 研究统计工具 | v1 只保留 DSR、block bootstrap、保留集检验；PBO/CSCV 移到后续版本 | 用户要求，降低工作量 |
| D-017 | E9 实验范围 | 增加 500 / 2000 USDT 真实资金量回测，严格应用最小下单额、`min_trade_usdt` 和容忍带 | 量化小资金的影响 |
| D-018 | 先验隔离 | 参数决定只依据研究集数据和事先写好的理由，不受 2025-07 之后行情印象的影响 | 保留集纪律的延伸（dev_plan §4 第 9 条） |
| D-019 | 外部心跳 | M6 增加心跳（例如 healthchecks.io，每次运行成功后 ping），`hc-ping.com` 加入 spec §13 允许域名 | 程序没有运行时无法自我告警 |

## 三、M0 技术决策（开发者，2026-11-01）

| 编号 | 问题 | 选择 | 理由 |
|---|---|---|---|
| D-020 | Python 版本与依赖管理 | 用 **uv** 管理：`pyproject.toml` + `uv.lock` + `.python-version`（固定 3.13）；`requires-python = ">=3.11,<3.14"` | 用户要求使用 uv。3.13 已在开发机上安装，pandas/numpy/matplotlib/cryptography 都有现成 wheel；上限 <3.14 避免依赖尚未适配新版本 |
| D-021 | 类型检查工具 | **mypy**（`strict = true`，启用 `pydantic.mypy` 插件；第三方库缺类型时用 `pandas-stubs`、`types-PyYAML`） | 纯 Python，用 `uv run` 即可运行，不需要 Node.js（pyright 的 PyPI 包会下载 Node 运行时）；对 pydantic 有官方插件；在 CI、Docker、Windows 上行为一致 |
| D-022 | Lint 与格式化 | ruff（lint + format），行宽 110 | 速度快，一个工具覆盖 lint 与格式化 |
| D-023 | 测试 | pytest + hypothesis（属性测试）+ pytest-socket（默认 `--disable-socket`，测试中访问网络直接失败） | 用户要求；保证单元测试不依赖网络 |
| D-024 | 进程锁 | `filelock`（跨平台） | 用户要求；Windows 与 Linux 行为一致 |
| D-025 | CLI 框架 | 标准库 `argparse` | 子命令数量有限，不需要额外依赖 |
| D-026 | 配置中的金额类参数 | 金额、价格比例类参数（`max_capital_usdt`、`min_trade_usdt`、`max_slippage`、`kill_slippage`、`fee_buffer`）在配置模型中用 `Decimal`，YAML 浮点数先转成字符串再转 `Decimal`；策略类参数用 `float` | 下单路径全程使用 Decimal（spec §6.1），避免二进制浮点误差；指标计算允许用浮点 |
| D-027 | 配置校验 | pydantic 模型一律 `extra="forbid"` | 配置项拼写错误必须报错，而不是被静默忽略 |
| D-028 | `execution.mode: maker_then_ioc` | v1 配置校验直接拒绝，提示"v1.1 才支持" | 保守：未实现的模式不允许被配置启用 |
| D-029 | `backtest.end` 与保留集 | 配置校验要求 `backtest.end < research.holdout_start` | 在配置层面就挡住误用保留集 |
| D-030 | `env: live` 的配置校验 | 要求在 YAML 中**显式**写出 `max_capital_usdt`（不能依赖默认值），否则拒绝加载；`dry_run` 未显式设置时给出警告 | spec §10.1、§13 |
| D-031 | 配置中的相对路径 | `assets_file`、`logging.dir` 等相对路径，以运行命令时的工作目录（项目根目录）为基准 | 所有命令都从项目根目录运行（CLAUDE.md），规则最简单 |
| D-032 | 外部域名白名单在配置层强制 | `data.market_data_rest` 只允许 `https://data-api.binance.vision`；`notify.heartbeat_url` 只允许 `https://hc-ping.com/...` | spec §13：外部请求只发往列出的域名；在加载配置时就挡住 |
| D-033 | ruff 的检查范围 | 排除 `doc/` 和 `*.md` | 新版 ruff 会格式化 Markdown 中的 Python 代码块，曾意外改动 spec 中的示例代码 |
| D-034 | `pattern_exceptions` 默认值 | 只保留 `JUP`；spec 示例中的 `SUPER` 已删除 | 正则匹配的是后缀，`SUPER` 不以 `UP` 结尾，不会被命中；其余命中项由 M1-6 列出后人工确认 |
| D-035 | CLI 骨架行为 | M0 阶段每个子命令先加载并校验配置，再提示"尚未实现"；退出码：0 成功、1 配置错误、3 尚未实现（argparse 用法错误为 2） | 便于在 M0 就验证配置，并区分各类失败 |
| D-036 | uv 的安装方式（开发机） | 用 `py -3.13 -m pip install uv` 安装到本机 Python 3.13；`uv` 不在 PATH 上时用 `py -3.13 -m uv`，`scripts/check.ps1` 会自动退回 | 不修改系统 PATH；服务器和 Docker 中按官方方式安装 |
