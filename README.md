# TradeAI

A 股主板价格监控与 AI 复盘系统（个人自用）。

当前处于 **项目框架搭建 + 数据源验证** 阶段，尚未接入数据库与 AI。

## 今日已完成

- 初始化 Git 仓库
- 建立 Python 3.12 独立环境（`.venv`）
- 搭建 FastAPI + Jinja2 + HTMX + ECharts 的前后端一体框架
- 完成腾讯 / 新浪实时行情、AkShare 日线/财务/公告的验证脚本
- 完成仪表盘、自选股、持仓、个股详情四个页面原型（使用演示数据 + 实时行情）

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
| 数据库 | 暂未接入，计划 SQLite（SQLAlchemy，后续可迁 PostgreSQL） |
| AI | DeepSeek（后续接入） |
| 通知 | 飞书 Webhook / SMTP 邮件（后续接入） |

## 目录结构

```
TradeAI/
├── app/
│   ├── main.py                 # FastAPI 入口
│   ├── config.py               # 配置（pydantic-settings）
│   ├── demo_data.py            # 演示数据（现仅用于播种与 AI 文案）
│   ├── db/
│   │   ├── base.py             # 引擎 / 会话 / 建表 / 健康检查
│   │   ├── models.py           # 16 张表的 SQLAlchemy 模型
│   │   └── seed.py             # 演示数据播种（幂等）
│   ├── providers/              # 行情数据源
│   │   ├── base.py             # Quote / Protocol
│   │   ├── tencent.py          # 腾讯实时行情
│   │   ├── sina.py             # 新浪实时行情
│   │   └── akshare_provider.py # AkShare 日线/财务/公告/日历
│   ├── services/
│   │   ├── quotes.py           # 行情服务（多源降级 + 缓存）
│   │   └── portfolio.py        # 自选 / 持仓 / 交易流水业务逻辑
│   └── web/
│       ├── routes.py           # 页面路由
│       ├── templates/          # Jinja2 模板
│       └── static/             # CSS / JS
├── scripts/
│   ├── init_db.py              # 建表 / 播种 / 重置数据库
│   ├── verify_quotes.py        # 腾讯/新浪实时行情验证
│   ├── verify_akshare.py       # AkShare 日线/财务/公告验证
│   ├── verify_feishu.py        # 飞书通知验证
│   ├── fetch_vendor.py         # 下载 ECharts / HTMX 到本地
│   ├── run.ps1                 # 启动开发服务器
│   └── verify_all.ps1          # 一键跑全部数据源验证
├── tests/
│   ├── test_providers.py       # 行情解析单元测试
│   ├── test_db.py              # 数据模型 / 约束 / 播种测试
│   ├── test_portfolio.py       # 自选 / 持仓 / 成本与盈亏测试
│   └── test_web.py             # 表单增删改 HTTP 流程测试
├── docs/
│   └── 项目大纲.md              # 技术选型、页面地图、验证结果
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

打开 <http://127.0.0.1:8000>。

页面：

- `/` 仪表盘
- `/watchlist` 自选股
- `/holdings` 持仓
- `/stock/{code}` 个股详情
- `/candidates` 候选池（占位）
- `/reports` 复盘（占位）

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

- `/watchlist`：输入 6 位代码加入自选，卡片右下角可移除；无计划的股票显示"未设置计划"。
- `/holdings`：录入成本、股数、止损、止盈；表格下方可修改止损价或删除持仓。
- 校验规则：仅主板代码、股数 100 的整数倍、止损必须低于成本价、止盈必须高于成本价。

## 测试

```powershell
& '.venv\python.exe' -m pytest
```

当前 31 个测试全部通过，覆盖行情解析、数据库约束、自选/持仓业务逻辑与表单 HTTP 流程。

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
3. 交易日历 + 盘中轮询 + 预警状态机
4. 飞书 / 邮件通知
5. 技术指标 + 全主板股票发现
6. DeepSeek 交易计划与每日复盘
7. Linux 部署与 7×24 运行

## 免责声明

本工具仅供个人研究，不构成任何投资建议。股市有风险，投资需谨慎。
