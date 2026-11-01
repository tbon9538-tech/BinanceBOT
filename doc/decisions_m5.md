# M5 决策记录（交易与执行层，不含编排）

> 格式同 `doc/DECISIONS.md`。spec 没有覆盖或有歧义的地方，按"更保守、更安全"的原则处理（spec 开头说明第 6 条）。日期均为 2026-11-01，开发者决定。

## 一、模块边界与接口

| 编号 | 问题 | 选择 | 理由 |
|---|---|---|---|
| M5-D01 | 过滤器与取整放在哪里 | 纯函数实现放在 `bot/domain/filters.py`；`bot/exchange/binance/rules.py` 负责解析 exchangeInfo，并重新导出这些函数 | CLAUDE.md 硬性约束：只有 `app/` 和 `exchange/` 可以 import `bot.exchange.binance`。订单计划（`execution/`）与 `SimSpotAccount`（`backtest/`）都要用同一份校验 |
| M5-D02 | `SpotAccount` Protocol 是否扩展 | 增加 `get_all_rules()`（全部交易对规则，含不可交易的）与 `get_commission_rates(symbol)`；新增领域类型 `CommissionRates`（费率 = 三类费率的 taker/maker + buyer/seller 之和，按官方 commission FAQ） | spec §6.2 第 3 步需要执行环境的全部规则与实际费率；编排层只依赖 Protocol |
| M5-D03 | 新增枚举值 | `OrderStatus.PENDING_NEW`（官方枚举有）、`SymbolStatus.UNKNOWN`；`SymbolRules.spot_allowed`（`isSpotTradingAllowed`），`is_trading = TRADING 且 spot_allowed` | 文档之外的状态、或不允许现货交易的交易对，一律按不可交易处理 |
| M5-D04 | 查询类请求最终失败的异常类型 | 新增 `ExchangeUnavailableError`：签名 GET 网络错误重试 3 次后、公开 GET 经账户层包装后、以及下单请求**发出之前**（对时失败）抛出 | 与 `ExchangeError`（交易所明确的业务错误）区分；执行器据此判断"订单肯定没有发出" |

## 二、签名与 HTTP

| 编号 | 问题 | 选择 | 理由 |
|---|---|---|---|
| M5-D05 | 签名 payload 的构造 | 所有签名请求的参数都放在 query string，body 为空；按插入顺序用 RFC 3986 percent-encode（只保留 `A-Za-z0-9-_.~`），`recvWindow`、`timestamp` 追加在最后；对这个字符串签名；签名本身再 percent-encode 后附加 `&signature=`；发送时 URL 原样传给 httpx（测试确认 httpx 不会重新编码） | 官方示例（rest-api.md）就是这样做的；"签名的字符串 = 发出去的字符串"最不容易出 -1022。签名器只接受 ASCII payload，未编码的非 ASCII 会直接报错 |
| M5-D06 | Ed25519 测试向量 | 官方 rest-api.md 中 Ed25519 示例的签名是 344 个 base64 字符（256 字节），且非 ASCII 示例与 RSA 示例完全相同，显然是从 RSA 示例复制的，无法作为 Ed25519 向量。改为：payload 字符串与官方示例逐字比对 + RFC 8032 §7.1 的 TEST 1/2 向量 + 用公钥验签 | HMAC 两个官方向量（ASCII 与 `１２３４５６`）均逐字通过 |
| M5-D07 | 视为"执行状态未知"的情况 | 下单 / 撤单：读超时、网络中断、HTTP 5xx、业务码 **-1006 与 -1007**、HTTP 200 但响应不是合法 JSON 或无法解析 → `UnknownOrderStatus` | spec 只列了 -1007；官方 errors.md 中 -1006 同样写明 "Execution status unknown"；响应无法解析时订单可能已被受理 |
| M5-D08 | 下单遇到 429/418 | 意图记为 `REJECTED`（error_code 为 429/418），异常继续向上抛出，整个运行停止 | 429/418 表示请求未被受理（spec §6.5 把它们排除在普通 REJECTED 之外，由 §3.5 处理：进程停止发送请求） |
| M5-D09 | 时间戳错误 -1021 | 签名 GET：重新对时后再试一次；下单：不重试，记为 REJECTED | 下单一律不自动重试（spec §6.1）；编排层应在每次运行开始时调用 `BinanceSpotAccount.sync_time()` |
| M5-D10 | ORDERS 限额 | 解析 exchangeInfo 的 ORDERS 限额，读取每个响应的 `X-MBX-ORDER-COUNT-*`；下单前若某窗口已用 ≥ 80%，等到下一个窗口；需要等待超过 60 秒时按限频处理（抛出 RateLimitedError） | 与 REQUEST_WEIGHT 的 80% 规则一致；日频策略不会触发，只是防护 |
| M5-D11 | 日志中的签名 | 带凭据的客户端把 `httpx`、`httpcore` 日志级别提高到 WARNING（httpx 在 INFO 级别会记录完整 URL，其中含签名）；客户端只记录方法和路径；非 JSON 错误响应与网络异常信息经 `redact_secrets` 处理；`ApiCredentials.__repr__` 只显示 Key 的前 4 位和后 4 位 | spec §12、§13 |
| M5-D12 | live 防护 | `BinanceSpotAccount` 在 `env=live` 且没有显式传入 `allow_live=True` 时拒绝构造（`LiveTradingNotAllowed`） | 编排层只在 spec §13 的确认全部通过后才传入 |
| M5-D13 | 凭据加载 | `load_credentials(environ=None, *, dotenv_path=None)`：只有传入 `dotenv_path` 时才（在函数内部）调用 `load_dotenv`，且不覆盖已有环境变量；`BINANCE_KEY_TYPE` 缺省为 hmac，容忍 `.env` 行内注释；Ed25519 私钥只支持不加密的 PEM（`.env.example` 没有口令变量）；Linux 上私钥权限宽于 600 时警告 | spec §10.2、§13 |

## 三、规则、过滤器与取整

| 编号 | 问题 | 选择 | 理由 |
|---|---|---|---|
| M5-D14 | NOTIONAL 与 MIN_NOTIONAL | 优先用 NOTIONAL；只有 MIN_NOTIONAL 时用它；两者都有时取较大的下限；maxNotional 为 0 或缺失表示不检查 | spec §6.3：MIN_NOTIONAL 只做解析容错；取较大值更保守 |
| M5-D15 | 旧版 PERCENT_PRICE | 没有 PERCENT_PRICE_BY_SIDE 而有 PERCENT_PRICE 时，买卖两侧用同一区间 | 解析容错；2026-11 的 testnet/demo/live 快照中全部是 BY_SIDE |
| M5-D16 | 有价格区间过滤器但缺少 avgPrice | 校验判为不通过；订单计划记为 `no_reference_price` 跳过 | 无法确认价格是否合法时不下单 |
| M5-D17 | 收窄后的取整方向 | 超过上限：收窄到上限并向下取整；低于下限：收窄到下限并向上取整（保证仍在区间内）；没有收窄时买价向下、卖价向上。收窄后买价 < ask 或卖价 > bid 时标记 `may_not_fill`，照常下单 | spec §6.3 "收窄到区间边界，再重新取整"；按原方向取整可能又越界 |
| M5-D18 | 值为 0 的过滤器项 | 所有 min/max/tick/step 为 0 时都不检查（LOT_SIZE 也一样） | filters.md 对 PRICE_FILTER 有明确说明；spec §6.3 统一为"值为 0 的项不检查" |
| M5-D19 | MAX_POSITION 过滤器 | 不解析、不预检；若被交易所拒绝，按 REJECTED 处理 | demo 上有 302 个交易对带此过滤器，但上限远高于 v1 的资金量 |

## 四、订单计划

| 编号 | 问题 | 选择 | 理由 |
|---|---|---|---|
| M5-D20 | 计划范围与非受管资产 | 范围 = `W>0 的交易对 ∪ {受管资产且余额>0 的 ASSET+USDT}`；非受管资产的余额既不计入当前持仓，也永远不会被卖出 | spec §6.3、§7 |
| M5-D21 | 卖出数量 | 只用 free 余额（locked 不动），按 stepSize 向下取整；超过 maxQty 等过滤器不通过时放弃该笔（`filter_rejected`），不截断 | spec §6.3 "其他校验不通过时，放弃该笔订单" |
| M5-D22 | 计划层自己的跳过原因 | `PlanSkip`：`not_tradable`、`blocked`、`no_quote`、`no_reference_price`、`filter_rejected`、`zero_capital`；调仓与否只由 `should_skip` 判断（测试用 spy 确认每个交易对都经过它） | spec §6.3：每次跳过都按原因计数（`count_skips`） |
| M5-D23 | 资金不足时的缩小 | 先剔除被阻断 / 不可交易 / 没有报价的候选，再对剩余候选按同一比例缩小；缩小后的金额再交给 `should_skip(ignore_band=True)` 判断金额下限；被跳过的那部分资金不再分配给其他币 | "每个跳过都经过 should_skip"；不再分配更保守、也更简单 |
| M5-D24 | 清仓计划 | `plan_liquidation`：受管资产目标为 0，滑点 `kill_slippage`，clientOrderId 类型 K；低于最小下单额的记为灰尘（同样经过 `should_skip`） | spec §5.5（只做与下单有关的部分） |

## 五、执行器

| 编号 | 问题 | 选择 | 理由 |
|---|---|---|---|
| M5-D25 | 意图的粒度 | 每次尝试一行 `order_intents`（cid 为主键），同一意图的各次尝试共享 `(strategy_id, run_date, seq)`；一批订单的第 1 次尝试在发送任何订单之前全部写为 PLANNED；每次发送前改为 SENT | write-ahead（spec §6.2 第 8 步、§6.5） |
| M5-D26 | 何时追剩余数量 | 只有上一次尝试的结果明确（PARTIAL / EXPIRED）时，才用 attempt+1、新 cid、刷新后的 bookTicker 重新定价追单；经历过状态未知（即使随后查到了订单）的意图，本次运行不再追单 | 绝不出现"同一意图因不知道是否成交而换新 ID 重下"（spec §6.6 第 4 条）；不追单只会让仓位暂时低于目标，下次运行按余额重算 |
| M5-D27 | 买单重试的金额上限 | 重试的数量 = min(剩余数量, (第 1 次的数量 × 价格 − 已花费) / 新价格) | 行情上涨时不超过原预算 |
| M5-D28 | ORDER_INCOMPLETE 告警 | 只在剩余数量仍可下单（能通过过滤器）而次数已用完时告警；剩余部分低于过滤器下限（例如部分成交后剩下的零头）只记 INFO | 避免每天为不可交易的零头告警 |
| M5-D29 | 状态未知时的查询结果 | 按 `unknown_status_query_delays_sec` 依次查询：查到已结束的订单即停止并用 myTrades 补全；**每次都明确返回 -2013** 才记 NOT_FOUND；只要有一次查询失败（网络 / 5xx）就保持 UNKNOWN。两种情况都阻断该交易对并发送 `ORDER_STATUS_UNKNOWN`（WARN） | spec §6.6 第 2 条写的是"全部返回 -2013"；查询失败不能当作"不存在" |
| M5-D30 | IOC 订单返回未结束状态（NEW 等） | 按同样的间隔查询；仍未结束则撤单；仍无法确认则记 UNKNOWN 并阻断该交易对 | IOC 不应停留在挂单状态；保守处理 |
| M5-D31 | 下一次运行处理的遗留意图 | PLANNED、SENT、UNKNOWN、NOT_FOUND 都处理，每个意图查询一次：查到 → 按结果更新并补全成交；查不到 → 余额核对；查询失败 → 保持原状态，本次运行阻断该交易对 | PLANNED 理论上没有发出（SENT 先于 POST 落盘），查一次成本很低，可以兜住断电导致的写入丢失 |
| M5-D32 | 余额核对的口径 | 意图创建时记录 base 资产余额 `base_balance_before`（本批开始时的交易所余额 + 本意图之前各次尝试的成交净额）；核对时：`当前余额 − (base_balance_before + 意图创建之后记录的已知成交净额)`，差额不超过 1 个 stepSize 视为可以解释 → CLOSED；否则写 `INFERRED_FILL`（WARN），意图同样 CLOSED，以交易所余额为准 | spec §6.6 第 3 条；以后的补单只通过"按实际余额重算差额"完成 |
| M5-D33 | 本地时间的先后顺序 | 执行器使用严格递增的 `MonotonicClock`；"某个时点之后的成交"按本地写入时间 `fills.recorded_at` 判断，不用交易所成交时间 | 避免本地与交易所时钟偏差、以及 Windows 时钟分辨率导致的边界错误 |
| M5-D34 | 撤单（§6.7） | 按前缀 `{strategy_id}-` 识别本策略的挂单并撤销（WARN）；其他挂单不动，只记 INFO；查询挂单失败或撤单状态未知时阻断该交易对；撤单被拒（如 -2011）时改为查单确认 | spec §5.5、§6.7 |
| M5-D35 | 对账（§6.8） | 受管资产（含 USDT）：本地 = 上次对账数量 + 之后记录的成交净额（含手续费）；差额 > 1 个 stepSize 写 `RECONCILE_DIFF`（市值 > 1 USDT 为 WARN，否则 INFO；无法估值时按 WARN）；无论是否有差额，一律用交易所数值覆盖并更新灰尘标记。USDT 的容差为 0；USDT 第一次对账时直接登记，不算差额 | 交易所余额是唯一真相 |
| M5-D36 | 首次买入登记受管资产 | 买单有成交时 `ensure_managed_position`，新行的 `updated_at` 取意图创建时间，使这笔买入的成交在随后的对账中被计入 | spec §7 |
| M5-D37 | KILL 检查 | 执行器在每笔订单（含重试）之前调用 `should_abort()`；触发后已写入但未发送的意图记为 CLOSED（"KILL：未发送"），返回 `report.aborted=True`。清仓本身另建 `should_abort` 恒为 False 的执行器，并沿用本次运行的 `blocked_symbols`；`KILL_MAX_ATTEMPTS = 3` 是 spec §5.5 的固定值，不是可调参数 | spec §5.5 |
| M5-D38 | 其他下单异常 | `ExchangeUnavailableError`（请求未发出）→ 意图 CLOSED 后上抛；其他任何未预期的异常（包括进程被杀）不捕获，意图保持 SENT，由下一次运行处理 | 崩溃安全 |

## 六、持久化

| 编号 | 问题 | 选择 | 理由 |
|---|---|---|---|
| M5-D39 | SQLite 设置 | WAL、`synchronous=FULL`、`busy_timeout=30s`、`check_same_thread=False` | write-ahead 意图必须在 POST 之前真正落盘；WAL 下 NORMAL 断电可能丢最后的事务 |
| M5-D40 | 序号分配 | `INSERT … ON CONFLICT DO UPDATE SET next_seq = next_seq + 1 RETURNING`，单条语句原子完成；测试覆盖重启与 4 个引擎 × 8 线程并发 | dev_plan M5-4 |
| M5-D41 | 迁移 | `schema_version` 表 + `migrations/v001_initial.py` 显式 DDL（全部 `IF NOT EXISTS`，可重跑）；不用 `metadata.create_all`；测试检查模型与迁移后的表结构一致 | 以后模型加字段时，新库仍须先建 v1 再逐个迁移 |
| M5-D42 | 相对 spec §8 增加的字段 | `order_intents`：kind、run_date、executed_qty、cumm_quote_qty、base_asset、base_balance_before、note；`orders`：strategy_id、account_id、side；`fills`：strategy_id、account_id、base_asset、quote_asset、recorded_at；`nav_snapshots`、`events`：run_id；`stop_events`：account_id；`regime_state` 主键改为 (strategy_id, date) | 执行与对账需要；spec §7 要求所有业务表带 strategy_id 和 account_id |
| M5-D43 | `daily_targets` 主键 | 改为 (strategy_id, sleeve, date, symbol, **kind**) | spec 的主键不含 kind，同一行无法同时保存 D、S、W |
| M5-D44 | 类型 | Decimal ↔ TEXT（`format(d, "f")`，拒绝 float）；时间 ↔ UTC ISO 8601（拒绝 naive datetime）；日期 ↔ YYYY-MM-DD。repo 返回已脱离会话的 ORM 对象，修改须通过 repo 方法 | spec §8 |

## 七、SimSpotAccount

| 编号 | 问题 | 选择 | 理由 |
|---|---|---|---|
| M5-D45 | 成交模型 | 单档盘口；买价 ≥ ask 按 ask 成交、卖价 ≤ bid 按 bid 成交；成交量不超过盘口数量且会消耗盘口；未成交部分 EXPIRED；手续费按费率计算、保留 8 位小数（四舍五入），买单扣 base、卖单扣 quote | 与实盘 IOC 行为一致且可预测；执行回放（M6-6）可在此基础上扩展 |
| M5-D46 | 拒单 | 过滤器不通过 → -1013；余额不足、交易对不可交易、clientOrderId 重复 → -2010；只支持 LIMIT IOC | 重复 ID 被拒可以在测试中直接暴露重复下单 |
| M5-D47 | 故障注入 | `inject_place_fault(kind, executed=…, symbol=…, times=…)`（timeout / 5xx / -1007 / crash，可选择在执行前或执行后出错）、`hidden_queries`（每个订单的前 N 次查询返回 -2013）、`fail_next_queries(n)`、`fail_next_cancels(n)`、`reject_next_order(code)`、`adjust_balance(asset, delta)`、`add_open_order(...)`、`set_book(...)`；`placed` 与 `stats` 记录全部请求 | dev_plan §6.3 |

## 八、待确认 / 遗留问题

| 编号 | 问题 | 现状 |
|---|---|---|
| M5-O01 | `.gitignore` 中的 `data/` 同时匹配 `bot/data/` | `bot/data/` 从未进入 git（M1 的代码只存在于主工作区），干净检出时 `import bot.data` 失败。建议改为 `/data/` 并补交 `bot/data/`。本里程碑没有修改（不在 M5 范围内，也不得改动 `bot/data/`） |
| M5-O02 | 官方 Ed25519 示例签名有误 | 见 M5-D06；testnet 联调（M5-9）时用真实 Ed25519 Key 再验证一次 |
| M5-O03 | 提现权限检查（`/sapi/v1/account/apiRestrictions`） | 只在 live 需要，留给编排层（spec §13） |
