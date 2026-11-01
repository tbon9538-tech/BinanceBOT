# demo 前向观察记录（Q14）

> 环境：Binance Spot Demo Trading（`https://demo-api.binance.com`），HMAC Key（存于被 git 忽略的 `.env`），虚拟资金。
> 状态库：`data/state-demo.db`。配置：`config/config.yaml` 的 `env: demo`，策略资金上限 `max_capital_usdt: 500`（Q12）。
> 周报：`python -m bot forward`，写入 `doc/forward/`。前向数据只用于观察与检验，不用于调参（D-018）。

## 2026-11-01 启动

| 步骤 | 命令 | 结果 |
|---|---|---|
| 签名自检 | `python -m bot status` | 成功；虚拟余额 5000 USDT（另有 5000 USDC，记为外部基线，不计入策略） |
| dry-run | `run --once --dry-run` | 大盘 RISK_ON；计划买入 BTC ≈ 237.5、INJ ≈ 24.9、PEPE ≈ 41.8 USDT（PEPE 在 testnet 上无报价，demo 上有） |
| 建仓 | `run --once --force-rebalance` | 3 个意图全部 FILLED（INJ 分 2 笔成交）；手续费 0.1%，从买到的币中扣除（与实盘一致）；策略仓位约 6%（资金上限 500 / 净值 5000） |
| 幂等 | 立即再运行 `run --once` | "今天已完成调仓"，零订单；无异常事件 |

## 每周观察（追加）

| 周 | 运行天数 / 漏跑 | 前向收益 | 研究引擎同期（折算） | 手续费率 / 成交偏移 | 备注 |
|---|---|---|---|---|---|
