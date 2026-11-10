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
│   ├── demo_data.py            # 页面演示数据（后续替换为数据库）
│   ├── providers/              # 行情数据源
│   │   ├── base.py             # Quote / Protocol
│   │   ├── tencent.py          # 腾讯实时行情
│   │   ├── sina.py             # 新浪实时行情
│   │   └── akshare_provider.py # AkShare 日线/财务/公告/日历
│   ├── services/
│   │   └── quotes.py           # 行情服务（多源降级 + 缓存）
│   └── web/
│       ├── routes.py           # 页面路由
│       ├── templates/          # Jinja2 模板
│       └── static/             # CSS / JS
├── scripts/
│   ├── verify_quotes.py        # 腾讯/新浪实时行情验证
│   ├── verify_akshare.py       # AkShare 日线/财务/公告验证
│   ├── verify_feishu.py        # 飞书通知验证
│   ├── fetch_vendor.py         # 下载 ECharts / HTMX 到本地
│   ├── run.ps1                 # 启动开发服务器
│   └── verify_all.ps1          # 一键跑全部数据源验证
├── tests/
│   └── test_providers.py       # 解析逻辑单元测试
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

1. 数据库表与 SQLAlchemy 模型
2. 自选股 / 持仓 / 交易计划 CRUD
3. 交易日历 + 盘中轮询 + 预警状态机
4. 飞书 / 邮件通知
5. 技术指标 + 全主板股票发现
6. DeepSeek 交易计划与每日复盘
7. Linux 部署与 7×24 运行

## 免责声明

本工具仅供个人研究，不构成任何投资建议。股市有风险，投资需谨慎。
