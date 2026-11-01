# testnet 联调记录（dev_plan M5-9、M6-7）

> 环境：Binance Spot Testnet（`https://testnet.binance.vision`），HMAC Key（用户提供，存于被 git 忽略的 `.env`）。
> 信号行情来自 live 只读接口 `data-api.binance.vision`；下单价格、数量和估值使用 testnet 自己的盘口与价格（spec §3.1）。

## 2026-11-01

| 步骤 | 命令 | 结果 |
|---|---|---|
| 1 签名自检 | `python -m bot status` | 读取余额成功（USDT ≈ 9999.22）；日志中 API Key 显示为 `Sq9g…Pefx` |
| 2 dry-run | `python -m bot run --once --dry-run` | 大盘 RISK_ON；计划买入 BTCUSDT ≈ 237.5 USDT、INJUSDT ≈ 24.9 USDT；1 个交易对 testnet 无报价（no_quote）、1 个低于最小金额，均被跳过 |
| 3 首次正式下单 | `python -m bot run --once --force-rebalance` | 2 笔 LIMIT IOC 买单全部 FILLED，但**发现问题**：testnet 账户预置了 1 BTC、66 INJ 等资产，首次买入后整份余额被登记为受管，净值从 1 万跳到 8.85 万（见下） |
| — 修复 | D-062 外部基线 | 新增 `external_balances` 表与 `StrategyAccountView`：策略视角余额 = 交易所余额 − 外部基线；回归测试 `test_preexisting_balances_are_external`。旧状态库备份为 `data/state.db.bak-testnet-before-D062` 后重建 |
| 4 重新下单 | `python -m bot run --once --force-rebalance` | 买入 BTC 0.00303 @ 77,982、INJ 3.42 @ 7.232，均 FILLED（testnet 手续费为 0）；净值 9,738 USDT、策略仓位 3%（其余为 max_capital 之外的 USDT）；无对账差异 |
| 5 幂等 | 立即再运行 `python -m bot run --once` | "今天已完成调仓，只更新快照"，**零订单** |
| 6 数据库核对 | `order_intents` / `fills` / `runs` | 2 个意图 FILLED（clientOrderId `mom1-261101-4314b47a-B001-1` 等），成交与手续费币种完整，run 状态 DONE，无异常事件 |
| 7 紧急停止演练 | `python -m bot kill` | 撤单 → 以 K 类订单（`…-K003-1`、`…-K004-1`）卖出策略持仓，均 FILLED；testnet 预置的 1.003 BTC、69.41 INJ **原样保留**；`KILL` 文件被删除；系统进入 KILLED |
| 8 恢复 | `resume --confirm`（被拒：需 `--after-kill`）→ `resume --confirm --after-kill` | KILLED → RUNNING |

## 结论

- 下单、成交记录、幂等、对账、紧急停止在 testnet 上全部按 spec 工作。
- 联调发现并修复了一个实盘同样会出现的资金归属问题（D-062）。
- 尚未在 testnet 上做的：Ed25519 Key（当前为 HMAC）、testnet 月度重置后的恢复（已在模拟演练中覆盖，见 `scripts/drills/`）。
