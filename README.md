# TradeAI

A 股主板价格监控与 AI 复盘系统（个人自用）。

当前处于 **个人版 P0 增强** 阶段：行情、监控预警、全市场发现、AI 计划与每日复盘均已跑通，后续再考虑多用户与 Linux 常驻部署。

## 近期更新（P0 第一批）

- **市场情绪**：全市场涨跌家数、涨跌停、涨超 5%、中位数、成交额，进程内缓存 180 秒（`GET /api/market/sentiment`）。
- **自定义组合预警**：价格 / 涨跌幅 / 成交额 / 换手率任意组合，支持 AND/OR、每日次数上限、到期日、按规则选择飞书 / 邮件 / 两者。
- **自选股分组与标签**：分组筛选、标签、拼音与首字母搜索、批量导入（`代码,分组,备注,标签`）、CSV 导出。
- **结构化 AI 研报卡**：个股页一键生成结论 / 评分 / 趋势 / 支撑压力 / 风险 / 催化 / 操作清单，同一交易日缓存，重复点击不重复计费。
- **盘前简报（08:45）**：昨日复盘 3 条 + 贴近建仓区的自选 + 全部自选关键价位 + 纪律提醒，零 AI 调用，可一键推送飞书。
- **板块资金页**：`/sectors` 展示全部行业板块的主力净流入/净占比/领涨股，东财限流时降级为新浪板块涨幅。
- **个股资金流**：个股页展示当日主力、超大单/大单/中单/小单与近 10 日曲线，东财限流自动切换新浪口径。
- **公告聚合**：个股页接入巨潮资讯最近 45 天公告，按业绩/分红/股东/风险/经营自动打标签，附 PDF 链接。
- **涨停梯队与龙虎榜席位**：`/limit-board` 按连板高度分组（首板/2/3/4/5+ 动态扩展），附封板资金、炸板次数、涨停统计；同页展示活跃营业部净买卖前 10。
- **自选卡片技术诊断**：卡片顶部显示行业板块，主体用灰底圆角分区（结论/评分、均线、支撑/压力/密集区、计划），只保留 **看多（红）/ 中性 / 看空（绿）**、评分和 2 条关键信号；完整诊断在个股页，零 AI。
- **持仓默认隐藏**：模块代码保留，`FEATURE_HOLDINGS=false` 时导航与仪表盘不显示持仓面板。

### 数据源验证结论（2026-11-10）

| 数据源 | 结果 |
|---|---|
| 腾讯实时行情 | ✅ 7/7 只，约 1.3 s |
| 新浪实时行情 | ✅ 7/7 只，约 0.37 s |
| 多源降级 + 缓存 | ✅ 腾讯超时自动切换新浪 |
| AkShare 股票列表 | ✅ 主板 3195 只（约 50 s，需落库缓存） |
| AkShare 日线 / 财务 / 公告 / 交易日历 / 指数 | ✅ 全部可用 |

详细字段与注意事项见 `docs/项目大纲.md` 第 7 节。

## 技术栈

| 层 | 选型 |
|---|---|
| 语言 | Python 3.12 |
| Web | FastAPI + Jinja2 + HTMX + ECharts |
| 数据 | pandas + AkShare（日线/财务/公告） |
| 实时行情 | 腾讯 `qt.gtimg.cn` / 新浪 `hq.sinajs.cn` |
| 数据库 | SQLite（SQLAlchemy，WAL；可切 PostgreSQL） |
| AI | DeepSeek（研报卡 / 计划 / 复盘叙述） |
| 通知 | 飞书 Webhook / SMTP 邮件兜底 |

## 目录结构

```
TradeAI/
├── app/
│   ├── main.py                 # FastAPI 入口
│   ├── config.py               # 配置（pydantic-settings）
│   ├── demo_data.py            # 演示数据（现仅用于播种与 AI 文案）
│   ├── db/
│   │   ├── base.py             # 引擎 / 会话 / 建表 / 健康检查
│   │   ├── models.py           # 17 张表的 SQLAlchemy 模型
│   │   └── seed.py             # 演示数据播种（幂等）
│   ├── providers/              # 行情数据源
│   │   ├── base.py             # Quote / Protocol
│   │   ├── tencent.py          # 腾讯实时行情
│   │   ├── sina.py             # 新浪实时行情
│   │   └── akshare_provider.py # AkShare 日线/财务/公告/日历
│   ├── services/
│   │   ├── quotes.py           # 行情服务（多源降级 + 批量抓取 + 缓存）
│   │   ├── portfolio.py        # 自选 / 持仓 / 交易流水业务逻辑
│   │   ├── calendar.py         # 交易日历缓存与盘中时段判断
│   │   ├── alerts.py           # 规则生成 + 预警状态机 + 自定义组合规则
│   │   ├── sentiment.py        # 全市场涨跌家数 / 涨跌停 / 成交额（带缓存）
│   │   ├── notifier.py         # 飞书卡片 + 邮件兜底 + 按规则分流 + 发送日志
│   │   ├── monitor.py          # 盘中轮询编排
│   │   ├── universe.py         # 股票池 / 上市日期 / 次新判断
│   │   ├── bars.py             # 日线回补与增量更新
│   │   ├── discovery.py        # 全市场发现编排与候选落库
│   │   ├── ai.py               # DeepSeek 客户端 + 预算 + 调用日志
│   │   ├── plans.py            # AI 计划生成 / 校验 / 人工确认
│   │   ├── report_card.py      # AI 个股研报卡（结论/评分/价位/清单）
│   │   ├── premarket.py        # 盘前简报（零 AI，昨日复盘 + 关键价位）
│   │   ├── market_flow.py      # 板块资金 / 龙虎榜 / 个股资金流（多源缓存）
│   │   ├── limit_board.py      # 涨停梯队（连板高度）与龙虎榜营业部席位
│   │   ├── tech_snapshot.py    # 技术诊断：均线/量能/支撑压力 + 看多看空评分
│   │   ├── industries.py       # 全市场行业分类（Baostock 批量，短标签）
│   │   ├── announcements.py    # 巨潮公告抓取与分类（按日缓存）
│   │   └── review.py           # 每日复盘（确定性摘要 + AI 叙述）
│   ├── analysis/
│   │   ├── context.py          # 单股上下文 + 惰性指标缓存
│   │   ├── features.py         # 指标构建器（可注册）
│   │   ├── factors.py          # 硬性过滤器 + 评分因子（可注册）
│   │   ├── pipeline.py         # 分层漏斗 + 综合评分
│   │   └── profiles/
│   │       └── default.yaml    # 阈值与权重（策略配置）
│   └── web/
│       ├── routes.py           # 页面路由
│       ├── templates/          # Jinja2 模板
│       └── static/             # CSS / JS
├── deploy/
│   ├── README.md               # 云服务器部署手册（Ubuntu 24.04）
│   ├── tradeai.service         # Web + 盘中轮询（systemd）
│   ├── tradeai-*.service/.timer# 盘前 / 收盘更新 / 复盘 / 备份
│   ├── backup.sh               # SQLite 备份（保留 14 天）
│   └── nginx-tradeai.conf      # 可选反向代理
├── scripts/
│   ├── init_db.py              # 建表 / 播种 / 重置数据库
│   ├── monitor_once.py         # 手动试跑一次轮询（--dry-run / --send）
│   ├── sync_universe.py        # 同步全主板股票列表与上市日期
│   ├── sync_industries.py      # 同步全市场行业分类（Baostock）
│   ├── backfill_bars.py        # 日线回补 / 增量更新
│   ├── discover.py             # 全市场技术面发现
│   ├── generate_plans.py       # 为候选生成 AI 交易计划
│   ├── daily_review.py         # 生成每日复盘
│   ├── premarket.py            # 盘前简报：打印 / 推送飞书（08:45）
│   ├── daily_update.py         # 盘后：增量日线 + 发现（可挂计划任务）
│   ├── verify_quotes.py        # 腾讯/新浪实时行情验证
│   ├── verify_akshare.py       # AkShare 日线/财务/公告验证
│   ├── verify_feishu.py        # 飞书通知验证
│   ├── fetch_vendor.py         # 下载 ECharts / HTMX 到本地
│   ├── run.ps1                 # 启动开发服务器
│   ├── restart.ps1             # 杀掉旧进程后重启（改路由后必用）
│   ├── package.ps1             # 生成 Linux 发布包（排除 .env/data）
│   ├── schedule_tasks.ps1      # 注册/查看/移除 Windows 每日任务
│   └── verify_all.ps1          # 一键跑全部数据源验证
├── tests/
│   ├── test_providers.py       # 行情解析 + 批量抓取 + 降级测试
│   ├── test_db.py              # 数据模型 / 约束 / 播种测试
│   ├── test_portfolio.py       # 自选 / 持仓 / 成本与盈亏测试
│   ├── test_calendar.py        # 交易日历与盘中时段测试
│   ├── test_alerts.py          # 规则生成 / 状态机 / 通知 / 轮询测试
│   ├── test_sentiment.py       # 市场情绪聚合与缓存测试
│   ├── test_custom_rules.py    # 自定义组合预警 / 上限 / 渠道测试
│   ├── test_watchlist_groups.py# 分组 / 标签 / 拼音搜索 / 导入 / 迁移测试
│   ├── test_report_card.py     # 研报卡载荷 / 校验夹取 / 缓存测试
│   ├── test_premarket.py       # 盘前简报分段 / 距离 / 推送测试
│   ├── test_industries.py      # 行业标签归一化 / 批量同步测试
│   ├── test_market_flow.py     # 板块资金 / 龙虎榜 / 个股资金流测试
│   ├── test_limit_board.py     # 涨停梯队 / 连板分组 / 席位解析测试
│   ├── test_tech_snapshot.py   # 均线 / 量能 / 支撑压力 / 密集区测试
│   ├── test_announcements.py   # 公告解析 / 分类 / 缓存 / 降级测试
│   ├── test_analysis.py        # 指标 / 因子 / 漏斗测试
│   ├── test_discovery.py       # 发现编排 / 日线存储 / 幂等测试
│   ├── test_ai.py              # AI 客户端 / 预算 / 计划校验 / 复盘测试
│   └── test_web.py             # 表单增删改 HTTP 流程测试
├── docs/
│   ├── 项目大纲.md              # 技术选型、页面地图、验证结果
│   └── 策略选股设计.md          # 前端策略选股的预留设计
├── .env.example
├── requirements.txt
├── pyproject.toml
└── 价格预警.md                  # 需求规格说明书 V2
```

## 环境准备

项目使用 conda 在项目内创建独立环境（`.venv`），避免污染系统 Python。

```powershell
# 1. 创建环境（已创建可跳过）
& 'D:\DownLoad\Miniconda3\Scripts\conda.exe' create -p 'D:\MyProject\TradeAI\.venv' python=3.12 -y

# 2. 安装依赖
& 'D:\MyProject\TradeAI\.venv\python.exe' -m pip install -r requirements.txt

# 3. 复制配置
Copy-Item .env.example .env

# 4. 前端静态依赖（ECharts / HTMX，已提交到仓库，可跳过）
& 'D:\MyProject\TradeAI\.venv\python.exe' scripts\fetch_vendor.py
```

> ECharts 与 HTMX 不走 CDN，直接由 `/static/vendor/` 提供，避免网络波动导致图表空白。

## 运行

```powershell
& 'D:\MyProject\TradeAI\.venv\python.exe' -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

> Windows 上 `--reload` 偶发“显示 Reloading 但旧进程仍在服务”（模板会更新、路由不更新）。改了 `app/**/*.py` 后如果页面还是旧的，用 `powershell -ExecutionPolicy Bypass -File scripts\restart.ps1` 强制重启。

打开 <http://127.0.0.1:8000>。

页面：

- `/` 仪表盘（市场情绪、风格、预警、自选）
- `/watchlist` 自选股（分组 / 标签 / 搜索 / 批量导入 / 导出）
- `/holdings` 持仓（默认关闭，`FEATURE_HOLDINGS=true` 才显示）
- `/stock/{code}` 个股详情（含 AI 研报卡）
- `/candidates` 候选池（全市场发现结果）
- `/plans` AI 交易计划（生成 / 确认 / 拒绝）
- `/alerts` 预警（规则、事件、自定义组合规则）
- `/reports` 每日复盘
- `/sectors` 板块资金（全部行业板块 + 涨跌/流入/领涨股）
- `/limit-board` 涨停梯队（连板高度 + 龙虎榜活跃席位）
- `/premarket` 盘前简报（预览 + 推送飞书）

功能开关（`.env`）：

```ini
FEATURE_HOLDINGS=false   # 持仓模块默认关闭；true 才显示导航、仪表盘持仓面板与 /holdings 入口
```

## 每日自动任务（Windows）

```powershell
# 注册三个任务（周一到周五，登录用户下运行）
powershell -ExecutionPolicy Bypass -File scripts\schedule_tasks.ps1

# 查看状态 / 卸载
powershell -ExecutionPolicy Bypass -File scripts\schedule_tasks.ps1 -List
powershell -ExecutionPolicy Bypass -File scripts\schedule_tasks.ps1 -Remove
```

| 时间 | 任务 | 说明 |
|---|---|---|
| 08:45 | `premarket.py --send` | 盘前简报推飞书（零 AI） |
| 15:35 | `daily_update.py` | 收盘快照 K 线 + 全市场发现 + 行业分类（每周）+ 涨停池/席位预热 |
| 18:00 | `daily_review.py` | 每日复盘（AI 叙述，约 0.015 元/天） |

日志落在 `data\logs\tradeai-*.log`。任务只在登录状态下运行，电脑需开机；盘中预警仍由 `MONITOR_ENABLED=true` 的常驻进程负责。

## 云服务器部署（Ubuntu 24.04）

发布包与 systemd 单元已准备好，完整步骤见 [`deploy/README.md`](deploy/README.md)。最短路径：

```powershell
# 1. 本地打包（不含 .env / data）
powershell -ExecutionPolicy Bypass -File scripts\package.ps1
scp dist\tradeai.tar.gz ubuntu@1.2.3.4:/tmp/
```

```bash
# 2. 服务器解压 + 建 venv + 配 .env（见 deploy/env.example）
sudo -u tradeai tar -xzf /tmp/tradeai.tar.gz -C /opt/tradeai
sudo -u tradeai python3.12 -m venv /opt/tradeai/.venv
sudo -u tradeai /opt/tradeai/.venv/bin/pip install -r /opt/tradeai/requirements.txt
sudo -u tradeai cp /opt/tradeai/deploy/env.example /opt/tradeai/.env  # 再填 key

# 3. 安装常驻服务 + 4 个定时器（08:45 / 15:35 / 18:00 / 23:30）
sudo APP_DIR=/opt/tradeai bash /opt/tradeai/deploy/install.sh
```

- `tradeai.service`：uvicorn 单 worker，监听 `127.0.0.1:8000`，`MONITOR_ENABLED=true` 时顺带跑盘中轮询；
- 定时器：盘前简报、收盘更新（K 线 + 发现 + 涨停池预热）、每日复盘、SQLite 备份；
- 对外访问优先 Tailscale；域名 80/443 需要 ICP 备案，nginx 配置已放在 `deploy/nginx-tradeai.conf`；
- 健康检查：`GET /health`（轻量存活）与 `GET /api/health`（数据库/日历/监控状态）。

## 数据库

SQLite（WAL）+ SQLAlchemy ORM，文件位于 `data/tradeai.db`（已 gitignore）。
换 PostgreSQL 只需改 `.env` 的 `DATABASE_URL`，模型与查询无需改动。

```powershell
# 建表（幂等，缺失的表会补建）
& '.venv\python.exe' scripts\init_db.py

# 建表 + 导入演示自选/持仓/计划
& '.venv\python.exe' scripts\init_db.py --seed

# 删库重建 + 播种（会清空数据）
& '.venv\python.exe' scripts\init_db.py --reset --seed
```

自选股与持仓的增删改都在页面上完成：

- `/watchlist`：输入 6 位代码加入自选，可填分组与标签；卡片顶部显示行业板块，主体为分区信息框（结论/评分、均线、支撑/压力/密集区、计划）；右下角可移除，点“查看分析”看完整诊断。
- 分组在添加或批量导入时设置，页面卡片保留分组徽章；不再占用卡片底部空间。
- `/watchlist` 支持按分组筛选、按代码 / 名称 / 拼音 / 首字母搜索，`批量导入` 每行 `代码,分组,备注,标签`（重复自动跳过），`导出 CSV` 下载当前自选。
- `/holdings`：录入成本、股数、止损、止盈；表格下方可修改止损价或删除持仓（默认不显示，见 `FEATURE_HOLDINGS`）。
- 校验规则：仅主板代码、股数 100 的整数倍、止损必须低于成本价、止盈必须高于成本价。
- 旧数据库升级：启动时 `init_db()` 会自动补 `watchlist.group_name` / `watchlist.tags` 两列，无需手工迁移。

## 监控与预警

默认关闭，避免在规则未确认时误发通知。开启方式（`.env`）：

```ini
MONITOR_ENABLED=true
POLL_INTERVAL_SECONDS=120
FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/...
```

规则自动生成，无需手工配置：

| 来源 | 规则 | 首次确认 |
|---|---|---|
| 持仓 | 止损（`current_stop`/计划止损）、移动止盈、止盈一/二 | 止损 1 次，其余 2 次 |
| 自选 | 进入建仓区间、当日涨跌幅超过 5% | 2 次 |
| 手工 | 自定义组合条件（`COMPOSITE`） | 1 次 |

- 盘中 09:30–11:30、13:00–15:00 每 120 秒轮询，集合竞价只记录不告警。
- 冷却期 30 分钟；价格反向恢复超过 0.5% 才重新武装；同一触发带唯一去重键，不会重复入库。
- 通知先走飞书卡片，失败重试 3 次（指数退避），再尝试 SMTP 邮件；每次请求与响应写入 `notification_log`。
- 自定义组合预警：在 `/alerts` 顶部填写代码与条件（价格 / 涨跌幅 / 成交额(亿) / 换手率），可选 AND/OR、每日上限、到期日、渠道；同一只股票只有一条自定义规则，重复保存即覆盖。自动生成的规则带 `auto=true` 标记，同步时只清理自己，不会误删手工规则。
- 页面：`/alerts` 查看规则数量、触发历史与通知状态；`/api/health` 返回交易日历时段与最近轮询结果。

手动试跑（不发送）：

```powershell
& '.venv\python.exe' scripts\monitor_once.py --sync-calendar --dry-run
```

真实推送把 `--dry-run` 换成 `--send`。也可以调用 `POST /api/monitor/tick?force=true&dry_run=true`。

## 全市场发现

### 数据准备（只需一次）

```powershell
# 1. 同步主板股票列表（含上市日期、ST 标记）
& '.venv\python.exe' scripts\sync_universe.py

# 2. 同步行业分类（Baostock，一次性约 90 秒；之后每周自动刷新）
& '.venv\python.exe' scripts\sync_industries.py

# 3. 回补日线（320 天，8 线程；全市场约 8 分钟，仅首次需要）
& '.venv\python.exe' scripts\backfill_bars.py
```

> 这一步是**一次性初始化**，不是每天运行。之后每天用收盘快照拼出当日 K 线，不再逐只请求历史。

日常盘后（建议每个交易日 15:35 跑一次，约 10 秒）：

```powershell
& '.venv\python.exe' scripts\daily_update.py
```

它会用一次批量收盘行情写入当日 K 线，然后直接跑全市场发现；行业分类每 7 天自动刷新一次（Baostock，约 90 秒），并预热涨停池/龙虎榜。建议每周日额外执行一次
`scripts\backfill_bars.py --force`（约 8 分钟），修复除权除息造成的前复权衔接偏差。

### 运行发现

```powershell
# 试点 300 只，不写库
& '.venv\python.exe' scripts\discover.py --limit 300 --no-persist

# 全市场，结果写入 candidate 表
& '.venv\python.exe' scripts\discover.py
```

页面上也可以点 `/candidates` 的"运行全市场发现"，或调用 `POST /api/discovery/run`。

盘后一键执行（建议 15:35 挂 Windows 任务计划）：

```powershell
& '.venv\python.exe' scripts\daily_update.py
```### 分层漏斗

| 阶段 | 内容 | 是否需要日线 |
|---|---|---|
| 1 | 主板、非 ST、上市 ≥ 60 个交易日、有有效行情 | 否 |
| 2 | 股价、成交额、换手率 | 否 |
| 3 | MA20 拐头向上、价格在 MA20 附近/上方、涨停状态 | 是 |
| 4 | 量比 1.2–3.0、接近/突破 20 日新高 | 是 |
| 5 | RSI12、MACD 多头、ATR 区间、20 日涨幅/回撤、低开 | 是 |
| 6 | 趋势 / 量价 / 动量 / 相对强度 / 波动质量加权评分，取前 30 | 复用 |

早期阶段淘汰的股票不会加载日线。实测：300 只试点约 2 秒；全市场在日线已缓存时约 4–12 秒（行情复用收盘快照时约 4 秒）。

### 策略配置

所有阈值和权重在 `app/analysis/profiles/default.yaml`：

- `enabled: false` 可直接关闭某个硬条件；
- 新增指标：在 `features.py` 加一个 `@feature_builder`；
- 新增因子：在 `factors.py` 加一个 `@hard_filter` 或 `@scorer`，再写进配置；
- 主流程、页面、数据库都不需要改。

> 后续可做成前端的"策略选股"页面：每个因子一个勾选项/滑块，选完生成一份 profile，后端用同一套 pipeline 执行。当前架构已经把策略与执行解耦，届时只需新增配置存储和表单。详细设计见 `docs/策略选股设计.md`。

> 已知限制：深市股票带行业字段，沪市交易所列表不含行业，因此部分沪市股票行业显示为“—”。后续可用东方财富行业板块接口批量补齐。

## AI 交易计划与复盘

先在 `.env` 配置：

```ini
DEEPSEEK_API_KEY=sk-...
AI_DAILY_BUDGET_CNY=5
```

重启服务后进入 `/plans`，选择 Top 3/5/10 生成计划；也可以命令行执行：

```powershell
& '.venv\python.exe' scripts\generate_plans.py --limit 5
& '.venv\python.exe' scripts\generate_plans.py --usage
```

生成的是 `draft` 计划，必须人工确认：

- 确认后计划变为 `confirmed`，自动加入自选并绑定计划，下一次盘中轮询开始按该计划的止损/止盈生成规则；
- 拒绝后标记为 `rejected`，不会进入监控；
- 同一股票同一天同一 prompt 版本默认复用缓存，不重复计费。

每日复盘（**不含候选池**）：

```powershell
& '.venv\python.exe' scripts\daily_review.py            # 摘要 + AI 叙述
& '.venv\python.exe' scripts\daily_review.py --no-ai    # 只生成确定性摘要
```

复盘内容：

| 模块 | 数据 |
|---|---|
| 大盘 | 四大指数涨跌、市场风格、仓位建议 |
| 板块资金 | 行业主力净流入/流出前 6（东财，按日缓存；接口不可用时降级为板块涨跌幅） |
| 龙虎榜 | 当日上榜股票、净买卖额、上榜原因（东财） |
| 自选 | 涨跌幅、是否进入建仓区 |
| 持仓 | 成本、浮动盈亏、今日盈亏、止损距离 |
| 个人操作 | 今日买入/卖出流水、已实现盈亏、每只持仓的明日操作建议 |

明日建议是规则生成的（接近止损、到达止盈、浮盈保护、浮亏检查），AI 只在此基础上写叙述，不改变数值。

也可以访问 `/reports` 手动生成；建议每个交易日 18:00 挂计划任务。

板块资金/龙虎榜接口被限流时：

```powershell
# 手动重试并刷新当日缓存
curl.exe -X GET "http://127.0.0.1:8000/api/market/sector-flow?refresh=true"
curl.exe -X GET "http://127.0.0.1:8000/api/market/lhb?refresh=true"
```

**系统侧校验**：AI 输出会强制修正止损/止盈/仓位关系（止损低于买入下沿、止盈高于买入上沿、最小盈亏比 1.2、仓位 3%–15%），修正记录写入计划的 `raw_response`，不会直接生效。

## AI 个股研报卡

交易计划回答“怎么买”，研报卡回答“这股正在发生什么”。在个股页 `/stock/{code}` 点击“生成研报卡”：

- 输入：现价、涨跌幅、成交额、换手率，最近日线指标（MA5/10/20/60、布林、RSI12、MACD、ATR、20 日涨跌幅与回撤、量比），20 日高低点参考位，已有计划（如有）与当前市场风格。
- 输出：结论（看多 / 中性 / 回避）、0–100 评分、趋势（上升 / 震荡 / 下降）、支撑 / 压力 / 失效位、摘要、风险、催化、2–5 条操作清单。
- 校验：结论与趋势收敛到枚举；评分夹取 0–100；支撑必须低于现价、压力必须高于现价、失效位不高于支撑，越界时用 20 日低/高点或当前价 ±5% 兜底；所有修正写入 `stock_report.raw`。
- 缓存：`trade_date + code + prompt_version` 唯一，同一交易日重复点击返回缓存；需要覆盖时用“重新生成”。
- 成本：单张约 0.005 元（约 1.2k 输入 / 0.3k 输出）。

接口：

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/reports/600036/generate"
curl.exe "http://127.0.0.1:8000/api/reports/600036"
curl.exe "http://127.0.0.1:8000/api/reports?limit=20"
```

## 确定性技术诊断（看多 / 看空）

自选卡片和个股页的诊断全部由程序计算，**不调用 AI**，同样输入永远得到同样结果：

- 事实：现价、涨跌幅、成交额、换手率、量比（盘中按已交易时段折算）、MA5/10/20/30/60、MA20 三日斜率、RSI12、MACD 柱、ATR、20 日高低点、支撑/压力、筹码密集区。
- 支撑/压力：支撑取现价下方最近的（20 日低点、MA20、密集区下沿）最大值；压力取上方最近的（20 日高点、MA20、密集区上沿）最小值。
- 评分：站上/跌破 MA20 ±2，MA20 斜率 ±2，均线排列 ±1，MA5/MA10 ±1，MACD 柱 ±1，RSI 区间 ±1，MA60 ±1，突破 20 日高点 +2，放量上涨 +1 / 放量下跌 -2，接近压力 -1，回撤过大 -1。
- 结论：得分 ≥ +3 为 **看多（红）**，≤ -3 为 **看空（绿）**，其余为中性；自选卡片只展示结论、评分、2 条权重最高的信号和支撑/压力，完整均线、RSI、MACD、ATR 与全部信号在个股页“技术诊断”面板。
- 接口：

```powershell
curl.exe "http://127.0.0.1:8000/api/analysis/600036"
curl.exe "http://127.0.0.1:8000/api/analysis?codes=600036,601318,002594"
```

> AI 研报卡保留为可选项：它负责风险/催化/操作清单这类叙述性内容，基础事实与多空判断不再依赖模型。

## 盘前简报（08:45）

盘后复盘看“今天发生了什么”，盘前简报看“今天该盯什么”。全部由本地数据生成，**不调用 AI**：

- 昨日复盘 3 条：市场风格与指数、板块资金、龙虎榜（取昨日复盘已存摘要）。
- 今日关注：把自选里已确认计划的股票按“距建仓区远近”排序，进入建仓区或距离 ≤3% 的排前面。
- 自选关键价位：建仓区间、止损、止盈，价格取最近一根日线收盘。
- 纪律提醒：不追高开、跌破止损执行、单票 ≤15%，附当前风格仓位建议。

```powershell
# 只打印
& '.venv\python.exe' scripts\premarket.py

# 推送飞书（非交易日自动跳过，--force 强制）
& '.venv\python.exe' scripts\premarket.py --send

# 指定日期 / 输出 JSON
& '.venv\python.exe' scripts\premarket.py --date 2026-11-12
& '.venv\python.exe' scripts\premarket.py --json
```

页面：`/premarket` 可预览全文并一键推送；接口 `GET /api/premarket?day=YYYY-MM-DD`、`POST /api/premarket/send?force=false`。推送结果写入 `notification_log`（`event_id` 为空表示这是简报而不是预警）。

## 板块资金、涨停梯队与龙虎榜

- `/sectors`：东财行业资金流按主力净流入排序，支持按板块/领涨股搜索；每日缓存 30 分钟。东财限流时自动降级为新浪板块涨幅，并在页面上明确标注。
- `/limit-board`：涨停梯队 + 龙虎榜活跃席位。
  - 连板高度来自东财涨停池（`stock_zt_pool_em`）的“连板数”，按实际高度动态分组（首板、2 板、3 板…6 板），同一高度内用横向密集网格排布，避免板块拉长；
  - 席位取东财活跃营业部（`stock_lhb_hyyyb_em`）最近上榜日，展示净买入/净卖出前 10 与关联个股；
  - 两者均按日缓存；首次生成约 15 秒，`daily_update.py` 收盘后会自动预热。
- 个股页“资金流”：当日主力（超大单 + 大单）、超大/大/中/小单净额，以及近 10 日主力净流入柱状图。
  - 主源东方财富（AkShare），每日缓存 3 小时；
  - 限流时降级新浪资金流（主力 = 超大单 + 大单，历史曲线为全单净额，页面会注明口径）；
  - 刚失败过的 10 分钟内不重复请求，避免延长 IP 限流。
- 公告：巨潮资讯 JSON 接口，最近 45 天，按标题关键词打标签（业绩 / 分红 / 股东 / 风险 / 经营 / H股），附 PDF 直链，按日缓存 6 小时。AkShare 的公告接口单只约 60 秒，页面不使用它。

> 东财 push2 对同一 IP 限流很激进：首次约 5 分钟，反复触发可能要 40 分钟以上。任何刷新失败都会先读最近一次成功缓存。

## AI 使用与成本

| 控制项 | 实现 |
|---|---|
| 调用范围 | 只分析候选池 Top 3–10，不发全市场 |
| 输入 | 结构化 JSON（价格、指标、评分），单只约 1–2k tokens |
| 频率 | 每个交易日一次；盘中监控不调用 AI |
| 缓存 | `code + trade_date + prompt_version`，确认/草稿均命中 |
| 预算 | 调用前检查 `ai_call_log` 当日累计，超预算直接拒绝 |
| 记账 | 每次调用记录输入/输出 tokens、耗时、费用、错误 |
| 失败兜底 | AI 失败不影响发现、监控与预警；复盘仍生成确定性摘要 |

## 测试

```powershell
& '.venv\python.exe' -m pytest
```

当前 145 个测试全部通过，覆盖行情解析与批量抓取、数据库约束、自选/持仓业务、分组/标签/拼音搜索与批量导入导出、技术诊断与看多看空评分、市场情绪聚合与缓存、交易日历与盘中时段、预警状态机与自定义组合规则、飞书卡片与失败重试、技术指标/因子/漏斗、全市场发现编排、板块资金/龙虎榜/个股资金流多源降级、涨停梯队与席位解析、公告分类与缓存、AI 预算/计划校验/研报卡/复盘、盘前简报分段与推送。

## 数据源验证

```powershell
# 实时行情（腾讯 + 新浪）
& '.venv\python.exe' scripts\verify_quotes.py

# AkShare 日线 / 财务 / 公告 / 交易日历
& '.venv\python.exe' scripts\verify_akshare.py

# 飞书通知（需要先配置 FEISHU_WEBHOOK）
& '.venv\python.exe' scripts\verify_feishu.py
```

## 关于腾讯/新浪是否能拉日线与财报

- 腾讯、新浪的**实时行情接口**只提供快照（价格、成交量、盘口等），不提供完整日线历史与财务报表。
- 它们另有历史行情/财经页面接口，但结构不稳定、无文档、易变，不适合作为主数据源。
- 因此：**实时行情用腾讯/新浪，日线、财务、公告、交易日历用 AkShare**。AkShare 本身也是对这些公开接口的封装，需做好多源降级与异常处理。

## 后续计划

1. ✅ 数据库表与 SQLAlchemy 模型
2. ✅ 自选股 / 持仓 CRUD（页面表单 + 服务层校验）
3. ✅ 交易日历 + 盘中轮询 + 预警状态机
4. ✅ 飞书 / 邮件通知
5. ✅ 技术指标 + 全主板股票发现
6. ✅ DeepSeek 交易计划与每日复盘（真实 key 验证，单日约 ¥0.05）
7. ✅ 市场情绪、组合预警、自选分组、研报卡、盘前简报、板块资金、涨停梯队、公告、确定性技术诊断
8. 🚧 腾讯云部署：发布包与 systemd/timer/备份脚本已准备（`deploy/`），等待购买服务器后按手册执行
9. ⏸️ 多人版改造：用户体系、自选隔离、通知渠道绑定、PostgreSQL（未开始，见 `docs/产品演进-多人版.md`）

## 免责声明

本工具仅供个人研究，不构成任何投资建议。股市有风险，投资需谨慎。
