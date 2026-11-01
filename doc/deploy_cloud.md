# 云服务器部署（demo 前向观察，Q15）

> 目的：把机器人和管理界面搬到一台 24 小时在线的云服务器上，笔记本合盖、关机都不影响每天 00:10 UTC（北京时间 08:10）的运行。
> 管理界面只在你自己的设备之间通过 **Tailscale**（私有网络）访问，不开放到公网。
> 同一个 demo 账户**只能有一处在运行**：搬到云上之后，本机不再启动 `bot` 服务。

---

## 0. 需要你先决定 / 准备的

| 项 | 说明 |
|---|---|
| 云服务商与地区（Q6） | 选你所在地法规允许、且币安不限制访问的地区。币安对部分国家和地区的 IP 拒绝访问（例如美国），**上线前必须在服务器上实测**（第 6 步）。地区离币安服务器越近延迟越低，但本策略一天只下一次单，延迟不重要 |
| 服务器规格 | 1 vCPU、1～2 GB 内存、20 GB 磁盘足够（每天运行几分钟）；系统选 Ubuntu 24.04 LTS |
| SSH 密钥 | 创建服务器时只用密钥登录，不用密码 |
| Tailscale 账号 | 免费个人版即可；服务器、笔记本、手机都登录同一个账号 |
| 管理界面账号 | 写进服务器上的 `.env`：`DASHBOARD_USERNAME`（默认 admin）、`DASHBOARD_PASSWORD`（demo 至少 8 位；**上实盘前换成至少 12 位的强密码**，否则界面拒绝启动，D-078） |

## 1. 服务器基础设置

```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install unattended-upgrades git
sudo timedatectl set-timezone UTC
timedatectl status                     # 确认 "System clock synchronized: yes"（签名请求依赖准确时间）
sudo ufw allow OpenSSH && sudo ufw enable   # 入站只放行 SSH；管理界面不对公网开放
```

## 2. 安装 Docker

按 Docker 官方文档安装 Docker Engine 与 compose 插件（docs.docker.com → Install Docker Engine on Ubuntu），然后：

```bash
sudo usermod -aG docker $USER   # 重新登录后生效
docker compose version
```

## 3. 安装 Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up               # 按提示在浏览器里登录你的 Tailscale 账号
tailscale ip -4                 # 记下服务器的 Tailscale 地址（100.x.y.z）
```

笔记本和手机安装 Tailscale 应用，登录同一个账号。之后 SSH 也可以走 Tailscale 地址；确认可用后，可以把公网 SSH 关掉（`sudo ufw delete allow OpenSSH && sudo ufw allow in on tailscale0`）。

## 4. 部署代码与数据

```bash
git clone https://github.com/tbon9538-tech/BinanceBOT.git && cd BinanceBOT
```

**先在本机停掉机器人**（已暂停的话跳过），确认本机不会再启动它：

```bash
docker compose down             # 在笔记本的项目目录执行
```

把本机的运行数据复制到服务器（在笔记本上执行，`<server>` 用 Tailscale 地址）：

```bash
scp .env <server>:BinanceBOT/
scp data/state-demo.db data/market.db <server>:BinanceBOT/data/
scp -r data/backup <server>:BinanceBOT/data/
```

复制前确认本机没有 `data/state-demo.db-wal`，或者它是 0 字节（`docker compose down` 正常退出时会合并并清空）。在服务器上执行 `chmod 600 .env`，并补上 `DASHBOARD_PASSWORD`。

## 5. 启动

```bash
docker compose up -d --build                       # bot（每天 00:10 UTC 运行）+ dashboard
sudo tailscale serve --bg 8080                     # 只在 Tailscale 网络内提供 HTTPS，转发到本机 8080
tailscale serve status                             # 显示访问地址 https://<机器名>.<tailnet>.ts.net
```

在手机或笔记本（已登录 Tailscale）上打开这个地址，用 `.env` 里的 `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` 登录。

## 6. 验证（全部通过才算迁移完成）

```bash
docker compose ps                                          # bot 与 dashboard 都是 Up
docker compose exec bot uv run --no-sync python -m bot status   # 能连上币安 demo，余额与本机最后一次一致
docker compose logs --tail 20 bot                          # 看到"常驻模式启动：每天 00:10 UTC 运行"
```

- `status` 报 HTTP 451 / 403 或连不上 `demo-api.binance.com`：该地区被币安限制，换地区，**不要**用代理绕过。
- 管理界面：概览与 `status` 一致；"实时持仓"能显示；"大盘"图有近一年曲线。
- 第二天 08:10（北京）之后：界面"最近一次运行"是当天日期且为 DONE；状态库备份份数 +1。
- 配置了心跳后：在 healthchecks.io 看到 ping。

## 7. 日常运维

| 要做的事 | 做法 |
|---|---|
| 看状态、暂停 / 恢复、紧急停止 | 管理界面（手机或电脑） |
| 更新代码 | SSH 到服务器：`git pull && docker compose up -d --build`，避开北京时间 08:10 前后 |
| 前向周报 | `docker compose exec bot uv run --no-sync python -m bot forward`，报告在服务器的 `doc/forward/` |
| 备份异地保存 | 定期在笔记本上 `scp -r <server>:BinanceBOT/data/backup ./backup-from-server/`（不要放进 git 仓库） |
| 重启服务器后 | 两个容器 `restart: unless-stopped` 会自动启动；`tailscale serve --bg` 的设置会保留 |

## 8. 安全要点

- 管理界面**能下单和清仓**（D-075）。只通过 Tailscale 访问，不要在云服务商的防火墙 / 安全组里开放 8080 端口。
- `.env` 只存在服务器和你自己的电脑上，权限 600，不进 git。
- 上实盘（阶段 D）时：实盘 Key 绑定这台服务器的公网 IP 白名单、禁止提现；那是另一个检查点，由你决定。
