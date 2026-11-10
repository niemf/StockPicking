"""Demo data for the UI skeleton.

All numbers here are illustrative only and will be replaced by database-backed
data in the next stage. The dashboard overlays real-time quotes from Tencent /
Sina when the network is available.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

MARKET_REGIME = {
    "regime": "neutral",
    "label": "中性",
    "score": 1,
    "position_advice": "建议仓位 50%~70%，单票不超过 15%",
    "reasons": [
        "上证指数站上 MA20，但成交额未明显放大",
        "涨跌家数比约 1.2，市场情绪偏暖",
        "指数波动率处于中位，暂无系统性风险信号",
    ],
}

INDICES = [
    {"code": "sh000001", "name": "上证指数", "fallback_price": 3668.03, "fallback_pct": 0.20},
    {"code": "sz399001", "name": "深证成指", "fallback_price": 11258.17, "fallback_pct": 1.01},
    {"code": "sz399006", "name": "创业板指", "fallback_price": 2498.80, "fallback_pct": 1.64},
    {"code": "sh000300", "name": "沪深300", "fallback_price": 4065.16, "fallback_pct": 0.22},
]

WATCHLIST = [
    {
        "code": "600036",
        "name": "招商银行",
        "industry": "股份制银行",
        "fallback_price": 43.05,
        "fallback_pct": 0.09,
        "buy_low": 41.50,
        "buy_high": 42.50,
        "stop_loss": 39.80,
        "take_profit": 47.50,
        "position_pct": 10,
        "rating": "看多",
        "confidence": 82,
        "risk": 35,
        "plan_date": "2026-11-10",
        "source": "AI 候选",
        "reason": "零售银行龙头，不良率与拨备覆盖率保持行业领先；日线 MA5/MA10/MA20 多头排列，近 5 日量价齐升，北向资金连续净流入。",
        "tags": ["均线多头", "量价齐升", "北向增持"],
    },
    {
        "code": "601318",
        "name": "中国平安",
        "industry": "保险",
        "fallback_price": 54.37,
        "fallback_pct": -0.42,
        "buy_low": 52.00,
        "buy_high": 53.50,
        "stop_loss": 50.00,
        "take_profit": 62.00,
        "position_pct": 10,
        "rating": "看多",
        "confidence": 78,
        "risk": 40,
        "plan_date": "2026-11-10",
        "source": "AI 候选",
        "reason": "NBV 连续两季改善，地产风险逐步出清，估值处于历史低位；股价站稳 MA60，MACD 在零轴上方金叉。",
        "tags": ["估值低位", "MACD金叉", "保险龙头"],
    },
    {
        "code": "000858",
        "name": "五粮液",
        "industry": "白酒",
        "fallback_price": 74.18,
        "fallback_pct": -0.75,
        "buy_low": 71.00,
        "buy_high": 73.50,
        "stop_loss": 67.00,
        "take_profit": 84.00,
        "position_pct": 8,
        "rating": "中性",
        "confidence": 68,
        "risk": 48,
        "plan_date": "2026-11-10",
        "source": "规则候选",
        "reason": "高端白酒动销边际改善，但渠道库存仍需消化；股价在 MA20 附近震荡，等待放量确认方向。",
        "tags": ["消费复苏", "区间震荡", "等待放量"],
    },
    {
        "code": "600519",
        "name": "贵州茅台",
        "industry": "白酒",
        "fallback_price": 1442.88,
        "fallback_pct": -0.15,
        "buy_low": 1400.00,
        "buy_high": 1430.00,
        "stop_loss": 1350.00,
        "take_profit": 1580.00,
        "position_pct": 12,
        "rating": "看多",
        "confidence": 80,
        "risk": 38,
        "plan_date": "2026-11-10",
        "source": "AI 候选",
        "reason": "品牌壁垒与现金流稳定，批价企稳；日线回踩 MA60 后止跌，量能温和放大，属于典型回踩确认结构。",
        "tags": ["核心资产", "回踩MA60", "现金流稳健"],
    },
    {
        "code": "002594",
        "name": "比亚迪",
        "industry": "新能源汽车",
        "fallback_price": 84.48,
        "fallback_pct": -0.49,
        "buy_low": 82.00,
        "buy_high": 85.00,
        "stop_loss": 78.00,
        "take_profit": 98.00,
        "position_pct": 8,
        "rating": "中性",
        "confidence": 65,
        "risk": 55,
        "plan_date": "2026-11-10",
        "source": "规则候选",
        "reason": "销量维持增长但价格战压制毛利；股价处于箱体下沿，需观察是否放量突破 MA20。",
        "tags": ["新能源", "箱体下沿", "毛利承压"],
    },
    {
        "code": "600000",
        "name": "浦发银行",
        "industry": "股份制银行",
        "fallback_price": 10.16,
        "fallback_pct": 0.59,
        "buy_low": 9.80,
        "buy_high": 10.00,
        "stop_loss": 9.40,
        "take_profit": 11.20,
        "position_pct": 6,
        "rating": "观察",
        "confidence": 60,
        "risk": 45,
        "plan_date": "2026-11-10",
        "source": "规则候选",
        "reason": "估值低、股息率高，但资产质量改善仍需验证；股价在 MA20 上方缩量整理，等待方向选择。",
        "tags": ["低估值", "高股息", "缩量整理"],
    },
]

HOLDINGS = [
    {
        "code": "600036",
        "name": "招商银行",
        "industry": "股份制银行",
        "cost": 38.20,
        "shares": 1000,
        "fallback_price": 43.05,
        "fallback_pct": 0.09,
        "stop_loss": 36.50,
        "take_profit_1": 45.00,
        "take_profit_2": 48.00,
        "trailing_high": 43.20,
        "open_date": "2026-09-15",
        "plan_id": "PLAN-0915-01",
    },
    {
        "code": "601318",
        "name": "中国平安",
        "industry": "保险",
        "cost": 58.00,
        "shares": 800,
        "fallback_price": 54.37,
        "fallback_pct": -0.42,
        "stop_loss": 53.00,
        "take_profit_1": 65.00,
        "take_profit_2": 70.00,
        "trailing_high": 57.80,
        "open_date": "2026-10-20",
        "plan_id": "PLAN-1020-02",
    },
    {
        "code": "000858",
        "name": "五粮液",
        "industry": "白酒",
        "cost": 78.50,
        "shares": 200,
        "fallback_price": 74.18,
        "fallback_pct": -0.75,
        "stop_loss": 70.00,
        "take_profit_1": 88.00,
        "take_profit_2": 95.00,
        "trailing_high": 80.20,
        "open_date": "2026-10-28",
        "plan_id": "PLAN-1028-03",
    },
    {
        "code": "600519",
        "name": "贵州茅台",
        "industry": "白酒",
        "cost": 1420.00,
        "shares": 20,
        "fallback_price": 1442.88,
        "fallback_pct": -0.15,
        "stop_loss": 1350.00,
        "take_profit_1": 1600.00,
        "take_profit_2": 1700.00,
        "trailing_high": 1528.00,
        "open_date": "2026-08-18",
        "plan_id": "PLAN-0818-04",
    },
]

ALERTS = [
    {
        "time": "10:35",
        "code": "600036",
        "name": "招商银行",
        "rule": "接近止盈一",
        "message": "现价 43.05，距止盈一 45.00 约 4.5%，浮盈 +12.7%",
        "level": "info",
        "status": "观察",
    },
    {
        "time": "10:12",
        "code": "601318",
        "name": "中国平安",
        "rule": "跌破 MA20",
        "message": "现价 54.37 跌破 MA20（55.86），趋势转弱",
        "level": "warning",
        "status": "观察",
    },
    {
        "time": "09:58",
        "code": "000858",
        "name": "五粮液",
        "rule": "接近建仓区",
        "message": "现价 74.18，接近建仓区间 71.00~73.50",
        "level": "info",
        "status": "观察",
    },
    {
        "time": "09:45",
        "code": "002594",
        "name": "比亚迪",
        "rule": "量比异动",
        "message": "量比 2.31，现价 84.48 位于建仓区间 82.00~85.00",
        "level": "info",
        "status": "已通知",
    },
]


def make_kline(base_price: float, seed: int, days: int = 60) -> list[dict]:
    """Generate deterministic demo OHLCV rows ending near ``base_price``."""
    rng = random.Random(seed)
    price = base_price * 0.88
    rows: list[dict] = []
    cursor = date.today() - timedelta(days=int(days * 1.6))
    while len(rows) < days:
        if cursor.weekday() < 5:
            open_price = price * (1 + rng.uniform(-0.008, 0.008))
            close_price = price * (1 + rng.uniform(-0.012, 0.018))
            high = max(open_price, close_price) * (1 + rng.uniform(0.002, 0.012))
            low = min(open_price, close_price) * (1 - rng.uniform(0.002, 0.012))
            volume = rng.randint(80_000, 320_000)
            rows.append(
                {
                    "date": cursor.isoformat(),
                    "open": round(open_price, 2),
                    "close": round(close_price, 2),
                    "low": round(low, 2),
                    "high": round(high, 2),
                    "volume": volume,
                }
            )
            price = close_price
        cursor += timedelta(days=1)

    # Scale the walk so the last close lands on the current price. This keeps
    # the demo chart visually consistent with the live quote.
    if rows:
        scale = base_price / rows[-1]["close"] if rows[-1]["close"] else 1.0
        for row in rows:
            for key in ("open", "close", "low", "high"):
                row[key] = round(row[key] * scale, 2)
    return rows


STOCK_DETAILS = {
    "600036": {
        "thesis": "零售银行龙头，资产质量与盈利能力在股份行中领先。当前估值处于历史中枢下方，股息率具备吸引力，技术面完成回踩确认后有望延续中期上行。",
        "fundamental_view": "2026Q3 营收同比 +4.8%，归母净利润同比 +6.2%，不良率 0.92%，拨备覆盖率 412%，核心一级资本充足率 13.6%。零售 AUM 保持双位数增长，财富管理收入边际改善。",
        "technical_view": "MA5/MA10/MA20 多头排列，MA20 斜率向上；近 5 日成交量较 20 日均量放大 28%；MACD 零轴上方红柱扩张；上方阻力 45.00，下方支撑 40.50。",
        "catalysts": ["降息周期下负债成本改善", "财富管理收入恢复", "高股息策略资金流入"],
        "risks": ["房地产风险暴露超预期", "净息差收窄快于预期", "零售信贷不良抬头"],
        "valuation": "PE(TTM) 6.4x，PB 0.92x，股息率 5.1%",
        "target_price": 47.50,
        "financials": [
            {"period": "2026Q3", "revenue": "1,286 亿", "revenue_yoy": "+4.8%", "net_profit": "452 亿", "profit_yoy": "+6.2%", "roe": "12.8%", "debt_ratio": "91.4%"},
            {"period": "2026Q2", "revenue": "1,248 亿", "revenue_yoy": "+4.1%", "net_profit": "438 亿", "profit_yoy": "+5.6%", "roe": "12.5%", "debt_ratio": "91.8%"},
            {"period": "2026Q1", "revenue": "1,210 亿", "revenue_yoy": "+3.6%", "net_profit": "421 亿", "profit_yoy": "+4.9%", "roe": "12.2%", "debt_ratio": "92.0%"},
            {"period": "2025年报", "revenue": "4,812 亿", "revenue_yoy": "+2.9%", "net_profit": "1,682 亿", "profit_yoy": "+4.2%", "roe": "12.0%", "debt_ratio": "91.6%"},
        ],
        "announcements": [
            {"date": "2026-11-08", "type": "定期报告", "title": "2026 年第三季度报告"},
            {"date": "2026-10-30", "type": "股东变动", "title": "关于股东增持计划的进展公告"},
            {"date": "2026-10-22", "type": "业绩说明", "title": "2026 年第三季度业绩说明会召开情况"},
        ],
    },
    "601318": {
        "thesis": "保险板块估值处于历史低位，NBV 连续两个季度改善，地产风险逐步出清。股价站稳 MA60 后出现 MACD 金叉，具备估值修复条件。",
        "fundamental_view": "2026Q3 归母营运利润同比 +5.4%，NBV 同比 +12.8%，代理人产能提升；综合偿付能力充足率 208%，地产敞口持续压降。",
        "technical_view": "股价站上 MA60，MA20 上穿 MA60；MACD 零轴上方金叉；成交量温和放大；阻力 58.00，支撑 52.00。",
        "catalysts": ["长端利率企稳", "NBV 持续改善", "地产风险出清"],
        "risks": ["资本市场大幅波动", "长端利率继续下行", "新单增长不及预期"],
        "valuation": "PEV 0.62x，PB 1.05x，股息率 4.6%",
        "target_price": 62.00,
        "financials": [
            {"period": "2026Q3", "revenue": "8,420 亿", "revenue_yoy": "+6.1%", "net_profit": "1,120 亿", "profit_yoy": "+8.4%", "roe": "11.2%", "debt_ratio": "89.6%"},
            {"period": "2026Q2", "revenue": "8,150 亿", "revenue_yoy": "+5.4%", "net_profit": "1,068 亿", "profit_yoy": "+7.2%", "roe": "10.8%", "debt_ratio": "89.9%"},
            {"period": "2026Q1", "revenue": "7,920 亿", "revenue_yoy": "+4.8%", "net_profit": "1,012 亿", "profit_yoy": "+6.5%", "roe": "10.4%", "debt_ratio": "90.2%"},
            {"period": "2025年报", "revenue": "31,200 亿", "revenue_yoy": "+3.9%", "net_profit": "3,980 亿", "profit_yoy": "+5.1%", "roe": "10.1%", "debt_ratio": "90.0%"},
        ],
        "announcements": [
            {"date": "2026-11-06", "type": "定期报告", "title": "2026 年第三季度报告"},
            {"date": "2026-10-28", "type": "分红", "title": "2026 年中期利润分配方案"},
            {"date": "2026-10-15", "type": "经营数据", "title": "2026 年 9 月保费收入公告"},
        ],
    },
    "000858": {
        "thesis": "高端白酒动销边际改善，但渠道库存仍需时间消化。股价处于区间震荡，建议等待放量突破 MA20 后再确认趋势。",
        "fundamental_view": "2026Q3 营收同比 +2.6%，归母净利润同比 +3.1%，毛利率 75.8%，合同负债环比改善；渠道库存约 2.5 个月，仍高于健康水平。",
        "technical_view": "股价围绕 MA20 震荡，MA5 与 MA10 粘合；成交量萎缩至 20 日均量的 72%；MACD 零轴附近走平；支撑 122.00，阻力 132.00。",
        "catalysts": ["春节备货催化", "渠道库存去化", "消费政策刺激"],
        "risks": ["渠道库存去化慢于预期", "高端酒批价下行", "消费复苏不及预期"],
        "valuation": "PE(TTM) 18.6x，PB 3.8x，股息率 3.2%",
        "target_price": 84.00,
        "financials": [
            {"period": "2026Q3", "revenue": "682 亿", "revenue_yoy": "+2.6%", "net_profit": "248 亿", "profit_yoy": "+3.1%", "roe": "22.4%", "debt_ratio": "22.8%"},
            {"period": "2026Q2", "revenue": "658 亿", "revenue_yoy": "+2.2%", "net_profit": "238 亿", "profit_yoy": "+2.8%", "roe": "21.8%", "debt_ratio": "23.1%"},
            {"period": "2026Q1", "revenue": "630 亿", "revenue_yoy": "+1.8%", "net_profit": "226 亿", "profit_yoy": "+2.4%", "roe": "21.2%", "debt_ratio": "23.5%"},
            {"period": "2025年报", "revenue": "2,420 亿", "revenue_yoy": "+1.5%", "net_profit": "892 亿", "profit_yoy": "+2.0%", "roe": "21.0%", "debt_ratio": "23.0%"},
        ],
        "announcements": [
            {"date": "2026-10-30", "type": "定期报告", "title": "2026 年第三季度报告"},
            {"date": "2026-10-18", "type": "经营数据", "title": "关于 2026 年前三季度主要经营数据的公告"},
        ],
    },
    "600519": {
        "thesis": "品牌壁垒与现金流稳定性仍是核心优势，批价企稳后估值具备修复空间。日线回踩 MA60 后止跌，量能温和放大，属于典型回踩确认结构。",
        "fundamental_view": "2026Q3 营收同比 +6.8%，归母净利润同比 +7.5%，毛利率 91.6%，预收款环比增长；直销渠道占比提升，i茅台平台贡献稳定。",
        "technical_view": "股价回踩 MA60 后收出止跌阳线，MA20 保持上行；MACD 绿柱缩短；成交量较 20 日均量放大 15%；支撑 1420，阻力 1550。",
        "catalysts": ["批价企稳回升", "春节备货", "分红率提升"],
        "risks": ["批价大幅波动", "消费需求走弱", "渠道库存上升"],
        "valuation": "PE(TTM) 21.8x，PB 7.2x，股息率 2.8%",
        "target_price": 1580.00,
        "financials": [
            {"period": "2026Q3", "revenue": "1,186 亿", "revenue_yoy": "+6.8%", "net_profit": "608 亿", "profit_yoy": "+7.5%", "roe": "34.2%", "debt_ratio": "18.6%"},
            {"period": "2026Q2", "revenue": "1,142 亿", "revenue_yoy": "+6.2%", "net_profit": "582 亿", "profit_yoy": "+7.0%", "roe": "33.5%", "debt_ratio": "18.9%"},
            {"period": "2026Q1", "revenue": "1,098 亿", "revenue_yoy": "+5.8%", "net_profit": "556 亿", "profit_yoy": "+6.6%", "roe": "32.8%", "debt_ratio": "19.2%"},
            {"period": "2025年报", "revenue": "4,280 亿", "revenue_yoy": "+5.2%", "net_profit": "2,120 亿", "profit_yoy": "+6.0%", "roe": "32.0%", "debt_ratio": "19.0%"},
        ],
        "announcements": [
            {"date": "2026-11-02", "type": "定期报告", "title": "2026 年第三季度报告"},
            {"date": "2026-10-25", "type": "分红", "title": "2026 年中期利润分配方案"},
            {"date": "2026-10-12", "type": "经营数据", "title": "2026 年第三季度主要经营数据公告"},
        ],
    },
    "002594": {
        "thesis": "销量维持增长但价格战压制毛利，股价处于箱体下沿。建议观察是否放量突破 MA20，未确认前不宜追高。",
        "fundamental_view": "2026Q3 营收同比 +12.4%，归母净利润同比 -8.6%，毛利率 18.2%（同比 -2.1pct）；海外销量占比提升至 24%，但国内价格战仍在持续。",
        "technical_view": "股价在 100~110 箱体内震荡，MA20 走平；成交量较 20 日均量放大 18%；MACD 零轴下方金叉；支撑 100，阻力 110。",
        "catalysts": ["海外销量超预期", "新车型放量", "原材料成本下降"],
        "risks": ["价格战持续", "毛利率继续下滑", "海外贸易摩擦"],
        "valuation": "PE(TTM) 24.5x，PB 4.1x",
        "target_price": 98.00,
        "financials": [
            {"period": "2026Q3", "revenue": "5,620 亿", "revenue_yoy": "+12.4%", "net_profit": "286 亿", "profit_yoy": "-8.6%", "roe": "14.2%", "debt_ratio": "72.8%"},
            {"period": "2026Q2", "revenue": "5,380 亿", "revenue_yoy": "+13.1%", "net_profit": "278 亿", "profit_yoy": "-6.2%", "roe": "13.8%", "debt_ratio": "73.1%"},
            {"period": "2026Q1", "revenue": "5,120 亿", "revenue_yoy": "+14.5%", "net_profit": "268 亿", "profit_yoy": "-3.8%", "roe": "13.4%", "debt_ratio": "73.5%"},
            {"period": "2025年报", "revenue": "19,800 亿", "revenue_yoy": "+18.2%", "net_profit": "1,120 亿", "profit_yoy": "+2.1%", "roe": "13.0%", "debt_ratio": "73.0%"},
        ],
        "announcements": [
            {"date": "2026-10-30", "type": "定期报告", "title": "2026 年第三季度报告"},
            {"date": "2026-10-20", "type": "产销数据", "title": "2026 年 9 月产销快报"},
        ],
    },
    "600000": {
        "thesis": "估值低、股息率高，但资产质量改善仍需验证。股价在 MA20 上方缩量整理，适合观察，不宜重仓。",
        "fundamental_view": "2026Q3 营收同比 +1.8%，归母净利润同比 +2.4%，不良率 1.48%，拨备覆盖率 168%；净息差 1.52%，仍面临下行压力。",
        "technical_view": "股价在 MA20 上方缩量整理，MA5 与 MA10 粘合；成交量萎缩至 20 日均量的 68%；MACD 零轴附近；支撑 9.60，阻力 10.50。",
        "catalysts": ["资产质量改善", "高股息资金流入", "地方债务化解"],
        "risks": ["不良率上升", "净息差收窄", "房地产风险暴露"],
        "valuation": "PE(TTM) 5.2x，PB 0.48x，股息率 6.2%",
        "target_price": 11.20,
        "financials": [
            {"period": "2026Q3", "revenue": "1,180 亿", "revenue_yoy": "+1.8%", "net_profit": "386 亿", "profit_yoy": "+2.4%", "roe": "8.6%", "debt_ratio": "92.1%"},
            {"period": "2026Q2", "revenue": "1,150 亿", "revenue_yoy": "+1.4%", "net_profit": "376 亿", "profit_yoy": "+2.0%", "roe": "8.4%", "debt_ratio": "92.4%"},
            {"period": "2026Q1", "revenue": "1,120 亿", "revenue_yoy": "+1.0%", "net_profit": "366 亿", "profit_yoy": "+1.6%", "roe": "8.2%", "debt_ratio": "92.6%"},
            {"period": "2025年报", "revenue": "4,520 亿", "revenue_yoy": "+0.8%", "net_profit": "1,460 亿", "profit_yoy": "+1.2%", "roe": "8.0%", "debt_ratio": "92.3%"},
        ],
        "announcements": [
            {"date": "2026-10-29", "type": "定期报告", "title": "2026 年第三季度报告"},
            {"date": "2026-10-16", "type": "股东变动", "title": "关于股东权益变动的提示性公告"},
        ],
    },
}


def get_stock_detail(code: str) -> dict | None:
    return STOCK_DETAILS.get(code)
