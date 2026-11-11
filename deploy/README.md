# TradeAI 云服务器部署（Ubuntu 24.04）

目标：腾讯云轻量 2C2G / Ubuntu 24.04 LTS，单进程 uvicorn + SQLite，systemd 常驻，定时任务负责盘前/盘后。

> 数据库仍是 SQLite（WAL），单用户/几个朋友完全够用；后续多人版再换 PostgreSQL。

---

## 0. 准备清单

- 服务器：2 核 2G、40GB+ SSD、Ubuntu 24.04 LTS
- 安全组：只放行 22（最好限制来源 IP）；**不要**对公网开放 8000
- 访问方式：优先 Tailscale 内网（`http://100.x.x.x:8000`），域名 80/443 等备案后再开
- 本地准备好的东西：`DEEPSEEK_API_KEY`、`FEISHU_WEBHOOK`、本地 `data/tradeai.db`（可选）

---

## 1. 系统初始化

```bash
sudo timedatectl set-timezone Asia/Shanghai
sudo apt update
sudo apt install -y python3.12-venv python3-pip sqlite3 rsync curl nginx

# 运行账号（不给登录 shell 也可以）
sudo useradd --system --create-home --shell /bin/bash tradeai
sudo mkdir -p /opt/tradeai/data /opt/tradeai/backups
sudo chown -R tradeai:tradeai /opt/tradeai
```

> Ubuntu 24.04 自带 Python 3.12，直接用它建 venv。

---

## 2. 上传代码

在 Windows 本地仓库根目录：

```powershell
# 生成不含 .venv / .git / data / .env 的发布包
powershell -ExecutionPolicy Bypass -File scripts\package.ps1

# 上传（把 IP 换成服务器公网 IP）
scp dist\tradeai.tar.gz ubuntu@1.2.3.4:/tmp/
```

服务器上：

```bash
sudo -u tradeai tar -xzf /tmp/tradeai.tar.gz -C /opt/tradeai
sudo -u tradeai mkdir -p /opt/tradeai/data /opt/tradeai/backups
```

---

## 3. 安装依赖与配置

```bash
sudo -u tradeai python3.12 -m venv /opt/tradeai/.venv
sudo -u tradeai /opt/tradeai/.venv/bin/pip install --upgrade pip
sudo -u tradeai /opt/tradeai/.venv/bin/pip install -r /opt/tradeai/requirements.txt
```

创建 `.env`：

```bash
sudo -u tradeai cp /opt/tradeai/deploy/env.example /opt/tradeai/.env
sudo -u tradeai nano /opt/tradeai/.env   # 填 key / webhook
sudo chmod 600 /opt/tradeai/.env
```

关键项：

```ini
APP_ENV=prod
MONITOR_ENABLED=true
DEEPSEEK_API_KEY=sk-...
FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/...
AI_DAILY_BUDGET_CNY=5
```

初始化数据库（二选一）：

```bash
# A. 全新开始：建表 + 同步股票列表 + 回补日线（约 10 分钟）
sudo -u tradeai bash -c 'cd /opt/tradeai && .venv/bin/python scripts/init_db.py'
sudo -u tradeai bash -c 'cd /opt/tradeai && .venv/bin/python scripts/sync_universe.py'
sudo -u tradeai bash -c 'cd /opt/tradeai && .venv/bin/python scripts/backfill_bars.py'

# B. 从 Windows 迁移：本地先做一致性备份
#    sqlite3 data/tradeai.db ".backup 'tradeai-sync.db'"
#    然后 scp tradeai-sync.db 到服务器 /opt/tradeai/data/tradeai.db
```

> 迁移方式能保留自选、研报卡、复盘、缓存目录（缓存可不迁移，会重新生成）。

---

## 4. 安装 systemd 服务

```bash
sudo APP_DIR=/opt/tradeai bash /opt/tradeai/deploy/install.sh
```

会安装并启用：

| 单元 | 时间 | 作用 |
|---|---|---|
| `tradeai.service` | 常驻 | Web + 盘中轮询（`MONITOR_ENABLED=true`） |
| `tradeai-premarket.timer` | 周一至五 08:45 | 盘前简报 → 飞书 |
| `tradeai-daily-update.timer` | 周一至五 15:35 | 收盘 K 线 + 全市场发现 + 涨停池预热 |
| `tradeai-daily-review.timer` | 周一至五 18:00 | 每日复盘（AI 叙述） |
| `tradeai-backup.timer` | 每天 23:30 | SQLite 备份，保留 14 天 |

检查：

```bash
systemctl status tradeai --no-pager
curl -s http://127.0.0.1:8000/api/health | head
journalctl -u tradeai -n 50 --no-pager
systemctl list-timers 'tradeai-*' --no-pager
```

手动试跑：

```bash
sudo -u tradeai bash -c 'cd /opt/tradeai && .venv/bin/python scripts/premarket.py'
sudo systemctl start tradeai-premarket.service
journalctl -u tradeai-premarket -n 50 --no-pager
```

---

## 5. 对外访问

### 方案 A：Tailscale（推荐，先跑起来）

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale ip -4
```

手机/电脑装 Tailscale 后访问 `http://100.x.x.x:8000`，不需要备案，也不暴露公网。

### 方案 B：域名 + nginx

```bash
sudo cp /opt/tradeai/deploy/nginx-tradeai.conf /etc/nginx/sites-available/tradeai
sudo ln -s /etc/nginx/sites-available/tradeai /etc/nginx/sites-enabled/tradeai
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

- 把 `server_name` 改成你的域名，安全组放行 80/443
- 中国大陆服务器绑定域名需要 ICP 备案
- 证书可用 `certbot --nginx -d your.domain`

> 继续保留 uvicorn 监听 `127.0.0.1:8000`，不要让 8000 直接对公网开放。

---

## 6. 备份与恢复

备份文件：`/opt/tradeai/backups/tradeai-YYYYmmdd-HHMMSS.db.gz`

```bash
# 手动备份
sudo -u tradeai /opt/tradeai/deploy/backup.sh

# 恢复（先停服务）
sudo systemctl stop tradeai
sudo -u tradeai bash -c 'gunzip -c /opt/tradeai/backups/tradeai-20261111-233000.db.gz > /opt/tradeai/data/tradeai.db'
sudo systemctl start tradeai
```

建议再复制一份到对象存储或另一台机器（可选 `rclone`），防止整机故障。

---

## 7. 更新发布

```powershell
# Windows：改完代码、测试通过后
powershell -ExecutionPolicy Bypass -File scripts\package.ps1
scp dist\tradeai.tar.gz ubuntu@1.2.3.4:/tmp/
```

```bash
sudo systemctl stop tradeai
sudo -u tradeai tar -xzf /tmp/tradeai.tar.gz -C /opt/tradeai
sudo -u tradeai /opt/tradeai/.venv/bin/pip install -r /opt/tradeai/requirements.txt
sudo systemctl start tradeai
sudo systemctl status tradeai --no-pager
```

数据库结构变更由启动时的 `init_db()` 自动补列/建表，无需手工迁移。

---

## 8. 安全与运维要点

- SSH 用密钥登录，`PasswordAuthentication no`；安全组 22 只开自己的 IP
- 不把 `.env`、`data/`、`backups/` 提交到 Git（已在 `.gitignore`）
- `MONITOR_ENABLED=true` 时必须保证只有一个 uvicorn worker，否则每个 worker 都会跑轮询
- 东财 push2 限流是常态：页面会降级到新浪，且 10 分钟内不重复请求；不要狂点刷新
- DeepSeek 预算由 `AI_DAILY_BUDGET_CNY` 控制，超预算会直接拒绝调用
- 定时任务按系统时区触发，部署后先确认 `timedatectl` 是 Asia/Shanghai

---

## 9. 验收清单

- [ ] `curl http://127.0.0.1:8000/api/health` 返回数据库与交易日历状态
- [ ] 浏览器打开 `/`，首页情绪卡片正常（首次约 2–3 秒）
- [ ] `/watchlist` 有自选与诊断；`/api/analysis/600036` 返回看多/看空
- [ ] `/limit-board` 有连板梯队（首次约 15 秒，之后走缓存）
- [ ] 手动执行一次盘前简报，飞书收到卡片
- [ ] 手动执行一次复盘，`/reports` 能看到
- [ ] `systemctl list-timers 'tradeai-*'` 四个定时器为 active
- [ ] 备份目录出现 `.db.gz`，`gunzip -t` 校验通过
- [ ] 重启服务器后服务自动恢复，预警状态机不丢
