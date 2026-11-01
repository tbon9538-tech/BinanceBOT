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

## 四、M1 实现决策（开发者，2026-11-01）

| 编号 | 问题 | 选择 | 理由 |
|---|---|---|---|
| D-037 | K 线在内存与数据库中的格式 | `get_daily_klines` 与 market.db 都使用 raw frame：价格、数量保留交易所给出的十进制字符串；计算时用 `to_float_frame` 转为 float | 入库无精度损失（dev_plan M1-3 验收）；指标计算允许用浮点 |
| D-038 | 同一天数据来自不同来源时 | vision 覆盖 REST；REST 不覆盖 vision | vision 是官方归档并有 CHECKSUM；REST 只用来补 vision 尚未发布的最近几天 |
| D-039 | 交易段间隔的计算 | 相邻两根日线的日历天数差 > `segment_gap_days`（7）时切段；上市天数按日历天数计（含首尾） | spec §2.3；7 天整的停牌不切段，8 天才切 |
| D-040 | §2.4 中"最近 30 根日线"的窗口 | 取 `[t−29, t]` 这 30 个日历日内存在的日线，至少 25 根才计算中位数 | 按日历窗口计数，缺失日自然体现为"有效值不足" |
| D-041 | vision 日文件的检查范围 | 只对仍在交易、或最近两个月内仍有月文件的交易对，逐月列出"没有月文件的月份"的日文件 | 避免为多年前已下架的交易对逐月列目录；当月数据在月文件发布前用日文件补 |
| D-042 | 下载文件保存 | zip 原样保存在 `data/raw/`（与 S3 路径一致），每次复用前重新校验 CHECKSUM；已入库的文件记录在 `vision_files` 表，再次同步时跳过 | 断点续传与审计；官方说明归档文件可能被事后更新，需要时可删除记录重新同步 |
| D-043 | 下载并发与写库 | 下载、解析在线程池中进行（≤ 8），写库只在主线程 | 避免 SQLite 多线程写入冲突 |
| D-044 | `data` 子命令 | 新增 `data report`（只输出各年份的交易对数和行数，不输出价格）；`data sync` 支持 `--symbols`、`--workers`、`--db`、`--raw-dir` | 便于核对覆盖情况和局部重同步 |
| D-045 | CLI 退出码 | 0 成功；1 配置错误；3 尚未实现；4 部分失败（例如个别交易对同步失败）；5 保留集守卫拒绝；6 被限频（429/418） | 便于脚本和调度器判断 |
| D-046 | `holdout` 子命令在 M1 的行为 | 先执行 spec §3.6 的启动检查（`--final`、`--confirm` 等于当天、非首次运行须加确认参数），通过后提示 M4 才实现；检查本身不写日志 | 守卫逻辑在 M1 就可测试；真正运行保留集时才写 `doc/research/holdout_runs.log` |
| D-047 | 资产审核输出 | `assets review` 把完整清单写到 `doc/assets_review/review_<日期>.md`，把建议草稿写到 `doc/assets_review/assets.draft.yaml`；**不修改** `config/assets.yaml` | spec §2.2：审核结果只能由人工编辑生效 |
| D-048 | 资产审核的辅助标记 | "疑似股票/ETF"：最近 90 天周末平均绝对日收益不到工作日的 35%；"疑似稳定币"：90 日年化波动 < 5% 且价格与 1 的偏差 < 5% | exchangeInfo 没有资产类别字段；代币化股票在美股休市的周末几乎不动。标记只用于提示人工，不参与自动决策 |
| D-049 | 审核清单的范围 | 列出所有通过"上市天数 ≥ 90 且成交额中位数 ≥ 下限"的 TRADING 资产，并给出流动性排名，而不只是当天前 30 名 | 排名每天变化，提前审核排名 30 名以外的资产，可以避免它们进入前 30 时因"待审核"而被跳过 |

## 五、M2 实现决策（开发者，2026-11-01）

| 编号 | 问题 | 选择 | 理由 |
|---|---|---|---|
| D-050 | 核心袖子有多个资产时如何分配 | 风险等分：每个资产 `min(max_gross, target_vol / σ_i) / n` | spec 只定义了单一 BTC；等分是最简单、不引入新参数的扩展。v1 只有 BTC，不受影响 |
| D-051 | pandas 模块的类型检查 | `bot.strategy.*`、`bot.backtest.*`、`bot.app.research_commands` 关闭 pandas-stubs 引起的若干错误码（arg-type、call-overload、union-attr 等），其余 strict 检查保留；交易、执行、持久化、配置等模块保持完整 strict | pandas-stubs 把 `.at/.loc` 标量标成很宽的 Union，几十处报错与逻辑无关；策略正确性由单元测试保证 |
| D-052 | ruff 行宽 | 关闭 E501（行宽由 `ruff format` 控制，中文长字符串不强制折行） | 用户可见的中文提示拆行反而降低可读性 |
| D-053 | 研究面板的列 | 每个交易段一列：最新段用交易对名，更早的段用 `交易对~段首日`；成交时用 `column_symbol()` 取回交易对名 | 让所有滚动窗口天然不跨段（spec §2.3），同名复用的旧币可以在回测中照常交易和退出 |
| D-054 | 研究数据的读取范围 | 从最早数据读到研究集终点（不截掉起点），大盘状态机从数据起点以 RISK_OFF 重放 | 与实盘"从最早数据重放"一致（spec §4.2），回测起点不影响状态 |
| D-055 | 研究引擎中下单价格缺失时 | t 日开盘价缺失的列当天不交易（估值用最近收盘价） | 保守：没有成交价就不假设成交 |
| D-056 | walk-forward 的实现 | 先对候选网格分别跑完整回测，再在每个测试窗口选训练窗口 Calmar 最高的候选，拼接测试窗口日收益；切换成本忽略 | 与"只在 E4a 目标波动和 E7 核心权重上选择"一致（strategy_research.md E10）；忽略切换成本偏乐观，报告中注明 |
| D-057 | 回测中的最小下单额 | 统一近似为 5 USDT；"理想情况"（E9）为 0 且不设容忍带与 min_trade | 历史过滤器无法获得（spec §6.3） |

## 六、M1 收尾与初次资产审核（2026-11-01）

| 编号 | 问题 | 选择 | 理由 |
|---|---|---|---|
| D-058 | "疑似股票/ETF"标记误伤 BTC | 改为同时满足"周末平均波动 < 工作日的 35%"与"资产名以 B 结尾"才标记 | 2026 年 BTC 周末波动也很低（比值 0.32），只看周末会误伤；币安股票代币统一命名为 xxxB |
| D-059 | 初次资产审核由谁完成 | 用户授权"不需要经过我同意"，由 Claude 按 `doc/assets_review/review_2026-10-31.md` 完成初次审核：通过流动性筛选的 58 个待审核资产中，排除 10 个疑似股票/ETF 和 1 个疑似稳定币（U），其余 47 个写入 `approved_assets`；SYRUP 加入正则例外 | 空的 approved 名单会让卫星袖子在实盘中永远不交易；以后的新资产仍按 spec §2.2 告警、人工审核 |
| D-060 | `.gitignore` 误伤 | `data/`、`logs/`、`reports/` 改为只匹配根目录（`/data/` 等），并补交此前从未进入版本库的 `bot/data/` | 原规则同时匹配 `bot/data/`，全新检出无法运行（M5 子任务发现） |

## 七、M3～M7 实现决策（2026-11-01）

> M5 子任务的 47 条实现决策见 `doc/decisions_m5.md`（M5-D01～D47）。

| 编号 | 问题 | 选择 | 理由 |
|---|---|---|---|
| D-061 | live 启动前的提现权限检查 | 调用 `GET /sapi/v1/account/apiRestrictions`；返回中没有 `enableWithdrawals` 字段、或其值不是 false，一律拒绝启动 | spec §13；字段名按官方文档实现，**本次没有用 live Key 验证**，缺字段即拒绝是保守选择 |
| D-062 | 账户里原有的资产 | 新增"外部基线"（`external_balances` 表，迁移 v002）：非受管资产每次运行刷新基线；策略第一次买入时基线冻结；策略视角余额 = 交易所余额 − 基线（`StrategyAccountView`），执行器、下单计划、净值、kill 都基于它；USDT 不设基线 | testnet 联调发现：账户预置 1 BTC 时，首次买入后整份 BTC 被当成策略持仓，净值从 1 万跳到 8.8 万。实盘中用户自己持有的币也会遇到 |
| D-063 | "无法解释的余额变化"的核对边界 | POST_TRADE / ACK 快照记下 `fills_through`（已包含的最后一笔成交的记录时间），核对时只统计它之后的成交 | 原实现依赖"快照时间晚于成交记录时间"；执行器的严格递增时钟在时钟静止时会把成交时间往后推几微秒，造成误报并禁止买入（执行回放发现） |
| D-064 | 执行回放的口径 | 研究引擎从 start−1 开始（与回放在 start 日开盘首次成交对齐）；回放时 `max_capital_usdt` 不设上限（与研究引擎一致）；模拟交易所使用回放时钟，并按价格设定 tickSize/stepSize | 不对齐时会出现与执行层无关的差异（首日建仓、资金上限、低价币无法下单） |
| D-065 | 实盘流程中的 ATR 止损 | E6 结论为关闭；若在 config 中开启，`run --once` 直接报错 | 止损在实盘流程中需要持久化止损事件，v1 未实现；避免"配置开了但实际没生效" |
| D-066 | 保留集未通过后的工程交付 | 工程交付继续（testnet 联调、演练、回放、部署），策略层面按规则"回到研究"；README 顶部注明"不建议投入实盘资金" | 用户要求开发到生产就绪；软件可用性与策略是否值得上线是两个问题，后者由用户决定 |

## 八、Q14 之后（2026-11-01）

| 编号 | 问题 | 选择 | 理由 |
|---|---|---|---|
| D-067 | 不同环境的状态库 | `data/state-<env>.db`（testnet / demo / live 各一个）；已有的 testnet 状态迁移为 `data/state-testnet.db` | 不同账户的受管持仓、外部基线、净值快照不能混用 |
| D-068 | 前向观察工具 | 新增子命令 `forward`：运行情况（漏跑）、执行质量（实际手续费率、相对开盘价的成交偏移）、与研究引擎同期模拟的对比；报告写入 `doc/forward/` | dev_plan M7-3 的周报要求；前向数据只用于观察与检验，不用于调参 |
| D-069 | 研究框架中的大盘过滤 | 新增 `regime.mode: ensemble`（R2），按处于 Risk-On 的均线比例缩放两个袖子的原始目标；sma / tsmom 模式结果不变（已用 v3 默认配置回测核对） | R2 预注册；未通过，默认仍为 sma |
| D-070 | 容器中的 KILL 文件位置 | 路径可由环境变量 `BOT_KILL_FILE` 指定（默认项目根目录 `KILL`）；docker-compose 设为 `/app/data/KILL`，主机创建 `data/KILL` 即可生效；周报目录 `doc/forward/` 也挂载到主机 | 原来的 KILL 文件在容器内部，主机上创建无效；容器运行时所有命令通过 `docker compose exec` 执行，避免主机与容器同时操作同一个 SQLite |
| D-071 | 心跳地址的存放位置 | 从环境变量 `HEARTBEAT_URL`（`.env`）读取；`config.yaml` 只保留开关 `notify.heartbeat_enabled`；配置里再写 `notify.heartbeat_url` 直接报错并提示新位置。域名白名单不变（只允许 https://hc-ping.com），不合规时启动报错，错误信息与日志都不含地址 | 地址里的 uuid 相当于密钥（拿到就能冒充 ping，让宕机不被发现），而仓库是公开的；做法与 Telegram token 一致 |
| D-072 | 状态库自动备份（ROADMAP A3） | 每次 `run --once`（含常驻模式）正常结束（没有抛异常；LOCKED 不算）后，在进程锁内用 SQLite 在线备份 API 复制状态库到 `backup.dir`（默认 `data/backup/`），文件名 `state-<env>-<UTC 时间>.db`，通过 `integrity_check` 后再改名，每个环境保留最近 `backup.keep`（30）份；失败记 `BACKUP_FAILED` 事件并发 WARN，不改变运行结果 | 在线备份得到一致快照（含 WAL），不影响运行中的连接；FAILED 时不备份，避免损坏的库把好备份挤掉；kill 不备份（紧急路径不增加步骤） |
| D-073 | MAX_POSITION 过滤器（ROADMAP C5） | 解析 `MAX_POSITION.maxPosition`；买单计划用**整个账户**（不扣外部基线）该资产的 free + locked 校验"买入后持仓 ≤ 上限"，超出则放弃该笔（`filter_rejected`）。只在有候选受此限制时多查一次原始余额；交易对有上限而计划层拿不到账户持仓时保守跳过。未成交买单不计入（本程序只下 IOC，没有挂单）；重试时数量只会变小，执行器不再重复校验。SimSpotAccount 按同样规则拒单 | 币安按整个账户计算持仓上限，策略视角余额会低估；下单前检查，避免交易所拒单 |
| D-074 | 过滤器不通过的记录 | 卖单、买单计划中的 `filter_rejected` 都写 WARN 日志和 `FILTER_REJECTED` 事件（WARN 级），日报中的买单一行也列出跳过原因计数 | spec §6.3 要求"放弃该笔，记录 WARN"；此前买单计划的跳过原因没有任何记录 |
| D-075 | 管理界面（Q15） | 新增子命令 `dashboard`（Starlette + uvicorn + Jinja2，服务端生成 SVG 图，页面不含脚本、不引用外部资源）：概览、单位净值与回撤、大盘图（行情库按当前配置重新计算，只用于展示）、实时持仓（交易所余额，60 秒缓存）、目标权重、运行记录、事件、成交、通知与备份状态。操作（用户选"看 + 全部操作"）：暂停、恢复（KILLED 后需另外确认）、立即运行一次（勾选确认；live 环境不提供）、确认当前余额、紧急停止（输入 KILL）、删除 KILL 文件。安全：整站 HTTP Basic 登录（`.env` 的 `DASHBOARD_PASSWORD`，至少 12 位，没有则拒绝启动，失败等待 1 秒）；POST + CSRF 令牌 + Origin 检查；CSP、禁止嵌入；只监听本机，远程通过 Tailscale；每次操作写 `DASHBOARD_OP` 事件。耗时操作在后台线程执行，同一时间一个。运维逻辑抽到 `bot/app/ops.py`，命令行与界面共用。docker-compose 增加 `dashboard` 服务，端口只映射到 127.0.0.1:8080。pytest 增加 `--allow-unix-socket`（asyncio 事件循环需要 Unix 域套接字，网络套接字仍然禁止） | 用户需要手机和电脑都能看的可视化界面，并能直接操作；界面能清仓，所以网络层（Tailscale）与应用层（密码、CSRF、确认词）都设防 |
| D-076 | 上云时机（Q15） | 恢复 demo 前向观察之前先部署到云服务器（`doc/deploy_cloud.md`），原计划在阶段 D 才做的 D1 提前；服务商与地区（Q6）由用户决定，上线前在服务器上实测能访问币安 demo 接口 | 用户的电脑是笔记本，保证不了每天 08:10（北京）在线；C4 要求连续 28 天、漏跑 ≤ 2 天 |
| D-077 | R3 的实现细节 | 卫星 `score: ctrend`（默认不启用）：信号周期按预注册固定为代码常量（与 `BLEND_LOOKBACKS` 相同的做法），回归窗口 W 与锚点间隔 h 放在配置；得分只用于排序，入选资格仍是"回看期收益 > 0"（与 ret_over_vol 的"得分 > 0"等价）；系数不足 W/2 个时当天卫星不选币；横截面回归要求样本数 ≥ 信号数 + 2；下架币在预测期内没有收盘价时从该期回归中剔除（学习样本有轻微幸存者偏差，只影响系数估计，不影响回测成交） | R3 预注册；未通过后代码保留为研究选项，便于以后在前向数据上复核 |
| D-078 | 管理界面的登录账号 | 用户要求使用固定用户名和较短的密码：新增 `DASHBOARD_USERNAME`（默认 admin，用户名也校验）；密码最短长度改为 demo / testnet 8 位、live 12 位（不满足拒绝启动）。用户名与密码都用常量时间比较，两项比较完再合并结果 | 用户的选择；界面只对本机开放、远程走 Tailscale，demo 的风险有限；实盘时界面能清真钱，仍要求强一些的密码 |
| D-079 | R4 的实现细节 | 核心袖子 `asset_filter: own_sma`（默认 `none`，不启用）：每个核心资产另外要求自身对 SMA 的带缓冲状态为 Risk-On（`regime_series`，与大盘过滤同一个函数）；未通过的资产不持有，它那份风险预算留作现金，n 仍按全部核心资产（D-050 不变）；大盘 Risk-Off 仍立即清零，单资产转弱按平滑逐步退出 | R4 预注册；未通过后代码保留为研究选项 |
| D-080 | 管理界面改版（视觉与交互） | 页面改为加载同源的 `/static/app.js`、`app.css`（CSP `script-src 'self'`、`connect-src 'self'`；仍不允许内联脚本、不引用外部资源；不开脚本时页面仍可用）。图表按 dataviz 规范：单一纵轴（单位净值与回撤分成两张图）、均线改为第二系列实线并加图例、十字准线提示与键盘查看、时间范围切换（1 月 / 3 月 / 6 月 / 1 年）；配色用参考配色并经验证脚本检查（浅色 / 深色）。实时持仓改为 `/partials/live` 异步加载，并按"占策略资金"对比目标与实际。操作改为按状态显示、确认框（HTML popover）、结果提示与后台操作自动轮询（`/api/ops`）。常驻进程每 60 秒写 `data/daemon.alive`（运行期间标记为 running），界面据此显示定时运行是否在进行 | 用户要求优化视觉和交互；原页面在机器人停止时仍显示"下次定时运行"，容易误解；双纵轴叠图容易误读 |
| D-081 | 补齐告警与数据检查（spec §12、§3.3、§4.2 的实现缺口） | ① 告警推送：执行器所有 WARN 级事件（`ORDER_STATUS_UNKNOWN`、`ORDER_INCOMPLETE`、差额 > 1 USDT 的 `RECONCILE_DIFF`、`INFERRED_FILL`、`INTENT_UNRESOLVED`、`ORDER_REJECTED`、`OPEN_ORDER_CANCELED`）经 `alert` 回调推送；风控的 `UNEXPLAINED_BALANCE_CHANGE`、`DAILY_ANOMALY` 推送 WARN；`HELD_SYMBOL_NOT_TRADING` 推送 CRITICAL；原来已有专门通知的（熔断、暂停、kill、数据未就绪、大盘切换、运行失败）不重复推送。② `MISSED_RUN`：最近一次 DONE 距今超过 `notify.missed_run_hours`（30）时告警，运行开始时（写入本次 STARTED 之前）与常驻模式每小时各检查一次，同一段空档只告警一次。③ §3.3：大盘标的在最近 `sma_period` 天内缺有效日线时中止本次运行（`DATA_NOT_READY` 附原因）；不满足 `0 < low ≤ min(open, close) ≤ max(open, close) ≤ high` 的日线剔除并记 WARN，窗口内出现缺口的币按 §3.4 当天不参与计算；单日收盘变化超过 `data.price_jump_warn`（0.6）只记 WARN。这三项只用于实盘每日流程，研究引擎不变（执行回放 2024Q4 差异仍为 0.38%）。④ `REGIME_REPLAY_MISMATCH`：重放结果与状态库最近一条大盘状态不一致时告警，以重放为准写入新的一天，旧记录保留。⑤ 日报补齐当日收益（相邻两次 PRE_TRADE）、核心 / 卫星仓位、成交额、手续费（按成交价折算成 USDT；用其他币支付的手续费不计入）、灰尘 | 2026-11-02 对照 spec 审查发现这些要求未实现 |
| D-082 | 审查发现、暂未修复的小偏差 | 记录在案，影响小：持仓币停止交易时不在 `positions.frozen` 中持久化，恢复交易后也不强制卖出（按当天目标处理）；`force_exit_assets` 在平滑阶段剔除而不是在流动性排名阶段排除（当天可能少选一个币）；待审核资产不写 `asset_review` 表，"7 天提醒一次"按整批而不是按资产；运行时不拉取实际手续费率（§6.2 第 3 步，当前没有用途）；`strategies` 表未登记；报告没有"扣除 USDT 理财收益"的夏普参考值（理财按 D-008 推迟） | 2026-11-02 审查；需要时再修 |
