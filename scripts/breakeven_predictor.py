#!/usr/bin/env python3
"""
破发概率引擎 v3 — HK IPO Helper (D_break 生死闸门)

打新收益是【非对称】的：不破发顶多赚多赚少（有限上行），一旦破发是现金亏损+资金占用+
情绪打击（折了夫人又折兵）。所以「会不会破发」是应当【前置】的第一道生死闸门——
过不了这关，后面算期望值、算年化都是空中楼阁。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
第一性原理（v3 骨架）：破发 = 发行价定得高于二级市场首日清算价。它只由四层决定，
所有细因子都挂到这四层下，而不是平铺成一长串薄因子：

  A 定价/价值  —— 发行价离公允值有多远         （根因 α，v1/v2 被严重低估）
  B 首日货源供给 —— 首日能砸出来多少货          （近因，两版都不够）
  C 承接力量   —— 有多少钱/意愿接货             （近因）
  D 市场环境   —— 外生水位与情绪               （β 乘数，两版过重且共线）

v1/v2 的通病：把 30%/25% 押在「同赛道情绪」这个滞后的 β 上，却给根因「定价」只留 5%，
本末倒置；且「同赛道」与「大盘水位」高度共线、重复计价，是个顺周期放大器。
v3 把权重从「市场情绪」搬回「定价根因 + 首日货源结构」，并把散落的市场类因子
【合并为单一 β 因子】根治共线。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

七因子与权重（合计 1.0；某因子无数据时其权重按比例摊到其余因子）：
  A1 定价与折溢价      12% ← 发行PE/行业PE折溢价 + 顶格/下限/超区间定价（招股书 / aipo）
  A2 Pre-IPO与老股套现  8% ← 招股价对上轮估值倍数(顺挂高倍/真倒挂) + 老股套现占比（招股书）
  B3 首日流通市值      15% ← 公开部分×价格，哑铃U型：两端危险中间安全（analyze / 招股书）
  B4 货源分散度        12% ← 一手中签率 + 公开发售比例 + 回拨机制A/B（odds / 招股书）
  C5 承接力量          18% ← 基石(比例×成色) + 保荐人历史破发率 + 绿鞋/稳价（aipo / sentiment）
  D6 市场β(合并)        22% ← 同赛道近期首日 + 大盘/板块水位 + VHSI + 连环破发潮（sentiment / web）
  E7 超购热度(U型)      13% ← 孖展超购倍数，过冷/过热都不利（aipo margin / web）

设计原则：
- 因子支持「未知」：数据缺失则该因子不参与、权重重新归一，并在输出标注数据缺口，绝不脑补。
- 输出【区间】而非单点；小流通市值(微盘)时区间加宽而非中心上移——微盘首日常被拉高诱多、
  真正杀伤在上市后 3-5 天阴跌，故只加大不确定性、不误判首日。
- 暗盘是首日破发相关性最高的领先指标：一旦提供 dark_pool_pct，直接 override 模型预测的
  首日期望，作为「首日走/留」的最终裁决锚。

用法：
    python3 breakeven_predictor.py --json '{"name":"示例","first_day_float_yi":6,...}'
可作库：from breakeven_predictor import predict_break
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Optional

# ============================================================
# 七因子权重（合计 1.0；某因子为 None 时其权重按比例摊到其余因子）
# ============================================================
WEIGHTS = {
    "pricing": 0.12,            # A1 定价与折溢价（根因）
    "preipo": 0.08,            # A2 Pre-IPO倍数 + 老股套现（根因）
    "float_value": 0.15,       # B3 首日流通市值（哑铃U型）
    "source_dispersion": 0.12, # B4 货源分散度
    "support": 0.18,           # C5 承接力量（基石×成色 + 保荐人破发率 + 绿鞋）
    "market_beta": 0.22,       # D6 市场环境（合并单一β，根治共线）
    "heat": 0.13,              # E7 超购热度（U型）
}

def _clamp(x: float) -> float:
    return max(0.0, min(100.0, x))

@dataclass
class BreakInput:
    """破发预测输入。每项都可为 None（未知则不参与打分、权重归一）。

    评分口径：每个因子给一个【破发压力分】0~100，越高越易破发。
    """
    name: str = ""
    code: str = ""

    # ---- A1 定价与折溢价 ----
    valuation_discount_pct: Optional[float] = None  # 发行相对行业/对标公允值的折让%（正=折让安全垫，负=溢价/倒挂）
    issue_pe: Optional[float] = None                # 发行PE（可选，与 industry_pe 配合算折溢价）
    industry_pe: Optional[float] = None             # 行业平均PE
    pricing_position: Optional[str] = None          # 定价位置：bottom/下限, mid/中, top/顶格上限, above/超区间
    priced_at_premium: Optional[bool] = None        # 兼容旧字段：是否高估值/高价（fallback）
    is_profitable: Optional[bool] = None            # 兼容旧字段：是否盈利（fallback，微调波动）

    # ---- A2 Pre-IPO 与老股套现 ----
    preipo_multiple: Optional[float] = None         # 招股价 / 最近一轮Pre-IPO每股成本。>1顺挂(解禁抛压), <1真倒挂(流血上市)
    old_share_pct: Optional[float] = None           # 老股套现占发行比例%（>30% 强利空）

    # ---- B3 首日流通市值（哑铃U型）----
    first_day_float_yi: Optional[float] = None      # 首日流通市值（亿港元）= 公开发售部分×价格

    # ---- B4 货源分散度 ----
    win_rate_1lot_pct: Optional[float] = None       # 一手中签率%（越高=货源越散到无锁定散户=首日抛压大）
    public_offer_pct: Optional[float] = None        # 公开发售占比%（<5% 强保护；越高散户货越多）
    clawback_mechanism: Optional[str] = None        # 回拨机制 "A"(上限35%) / "B"(无回拨，比例锁定更可控)

    # ---- C5 承接力量 ----
    cornerstone_pct: Optional[float] = None         # 基石占发行比例%（越高抛压越小）
    cornerstone_grade: Optional[str] = None         # 基石成色：diamond/钻石(国家队/主权基金), gold/黄金(顶级私募/产业巨头), bronze/青铜(凑数亲友团)
    sponsor_break_rate_pct: Optional[float] = None  # 保荐人近一年破发率%（>60% 高危）
    sponsor_is_tier1: Optional[bool] = None         # 兼容旧字段：是否顶级投行（fallback）
    has_greenshoe: Optional[bool] = None            # 是否有绿鞋（超额配售权，护盘正面信号）

    # ---- D6 市场环境（合并单一β）----
    peer_first_day_avg_pct: Optional[float] = None  # 同赛道最近几只平均首日涨幅%（负=破发潮，正=赚钱效应）
    peer_break_count: Optional[int] = None          # 同赛道近期破发只数
    peer_total_count: Optional[int] = None          # 同赛道近期总只数
    hsi_change_pct: Optional[float] = None          # 当日恒指涨跌%
    sector_change_pct: Optional[float] = None       # 所属板块当日涨跌%
    vhsi: Optional[float] = None                    # 恒指波动率指数VHSI（<18平静，>30恐慌）
    is_break_wave: Optional[bool] = None            # 是否处于连环破发潮

    # ---- E7 超购热度 ----
    oversubscription: Optional[float] = None        # 孖展超购倍数（U型：过冷<1易破，过热>150纯散户抛压大）

    # ---- 覆盖锚（非因子）----
    dark_pool_pct: Optional[float] = None           # 暗盘涨幅%，若提供则override模型预测的首日期望

# ============================================================
# 各因子 → 破发压力分（0~100，越高越易破发）
# ============================================================
def _score_pricing(inp: BreakInput) -> Optional[float]:
    """A1 定价与折溢价（根因）：溢价/顶格/超区间→高压力；折让/下限定价→低压力。"""
    if all(v is None for v in (inp.valuation_discount_pct, inp.issue_pe,
                               inp.pricing_position, inp.priced_at_premium)):
        return None
    score = 50.0
    # 主信号：折溢价%。优先用直接给的 discount，其次用 PE 反推
    disc = inp.valuation_discount_pct
    if disc is None and inp.issue_pe is not None and inp.industry_pe:
        disc = (inp.industry_pe - inp.issue_pe) / inp.industry_pe * 100.0  # 发行PE低于行业=折让
    if disc is not None:
        # +30%折让→压力约20；0→50；-40%溢价(接近倒挂)→约90
        score = 50 - disc * 1.0
    elif inp.priced_at_premium is not None:
        score = 60.0 if inp.priced_at_premium else 45.0  # 旧字段 fallback
    # 定价位置
    if inp.pricing_position is not None:
        pos = str(inp.pricing_position).lower()
        if pos in ("above", "超区间"):
            score += 20
        elif pos in ("top", "upper", "顶格", "上限"):
            score += 12
        elif pos in ("bottom", "lower", "下限"):
            score -= 12
    # 盈利与否（旧字段）微调波动
    if inp.is_profitable is False:
        score += 5
    elif inp.is_profitable is True:
        score -= 3
    return _clamp(score)

def _score_preipo(inp: BreakInput) -> Optional[float]:
    """A2 Pre-IPO与老股套现：顺挂高倍(解禁必抛) 与 真倒挂(流血上市) 两个相反机制都利空。"""
    if inp.preipo_multiple is None and inp.old_share_pct is None:
        return None
    score = 50.0
    if inp.preipo_multiple is not None:
        m = inp.preipo_multiple
        if m >= 1:
            # 顺挂：早期投资人账面浮盈越高，解禁抛压越大。1x→50, 2x→65, 3x→80
            score = min(85.0, 50 + (m - 1) * 15)
        else:
            # 真倒挂：招股价低于上一轮，被迫折价流血上市=基本面走弱强信号。0.8x→62, 0.5x→80
            score = min(85.0, 50 + (1 - m) * 60)
    if inp.old_share_pct is not None:
        # 老股套现>30% 强利空（原始股东用IPO当退出通道）
        score = max(score, 45 + inp.old_share_pct * 1.0)
    return _clamp(score)

def _score_float_value(inp: BreakInput) -> Optional[float]:
    """B3 首日流通市值（哑铃U型）：两端危险、中间安全。
    注意：微盘(<2亿)首日常被拉高诱多，故中心压力不设过高，其高波动通过 band 加宽体现。"""
    if inp.first_day_float_yi is None:
        return None
    v = inp.first_day_float_yi
    if v < 1:
        return 62.0    # 微盘：极高波动、易控盘，首日方向难测
    if v < 2:
        return 55.0
    if v <= 10:
        return 35.0    # 健康区间，历史首日破发率最稳
    if v <= 30:
        return 48.0
    if v <= 80:
        return 60.0
    return 68.0        # 超大盘：承接消化压力大（但通常有强护盘对冲，见 support 因子）

def _score_source_dispersion(inp: BreakInput) -> Optional[float]:
    """B4 货源分散度：货越散到无锁定散户手里，首日集中抛压越大。"""
    parts = []
    if inp.win_rate_1lot_pct is not None:
        # 一手中签率 5%→约33；30%→约47；80%→约74
        parts.append(_clamp(30 + inp.win_rate_1lot_pct * 0.55))
    if inp.public_offer_pct is not None:
        p = inp.public_offer_pct
        parts.append(30.0 if p < 5 else 45.0 if p <= 15 else 55.0 if p <= 30 else 65.0)
    if not parts and inp.clawback_mechanism is None:
        return None
    score = sum(parts) / len(parts) if parts else 50.0
    # 回拨机制：B(无回拨,公开比例锁定)更可控；A(上限35%)散户货可能被推高
    if inp.clawback_mechanism is not None:
        m = str(inp.clawback_mechanism).upper()
        if m == "B":
            score -= 8
        elif m == "A":
            score += 3
    return _clamp(score)

def _score_support(inp: BreakInput) -> Optional[float]:
    """C5 承接力量：基石(比例×成色) + 保荐人历史破发率 + 绿鞋。比例是量，成色是质。"""
    parts = []
    # 基石：比例 × 成色
    if inp.cornerstone_pct is not None:
        base = 80 - inp.cornerstone_pct * 1.0   # 0%→80, 30%→50, 50%→30
        if inp.cornerstone_grade is not None:
            g = str(inp.cornerstone_grade).lower()
            if g in ("diamond", "钻石"):
                base -= 15
            elif g in ("gold", "黄金"):
                base -= 7
            elif g in ("bronze", "青铜"):
                base += 12   # 亲友团凑数，锁定到期必砸
        parts.append(_clamp(base))
    elif inp.cornerstone_grade is not None:
        g = str(inp.cornerstone_grade).lower()
        parts.append(35.0 if g in ("diamond", "钻石") else 45.0 if g in ("gold", "黄金") else 65.0)
    # 保荐人历史破发率（优先），否则退回 Tier1 布尔
    if inp.sponsor_break_rate_pct is not None:
        parts.append(_clamp(20 + inp.sponsor_break_rate_pct * 0.8))  # 60%破发率→68
    elif inp.sponsor_is_tier1 is not None:
        parts.append(40.0 if inp.sponsor_is_tier1 else 60.0)
    if not parts and inp.has_greenshoe is None:
        return None
    score = sum(parts) / len(parts) if parts else 50.0
    # 绿鞋/稳价护盘
    if inp.has_greenshoe is True:
        score -= 8
    elif inp.has_greenshoe is False:
        score += 5
    return _clamp(score)

def _score_market_beta(inp: BreakInput) -> Optional[float]:
    """D6 市场环境（合并单一β）：同赛道近期首日 + 大盘/板块水位 + VHSI + 破发潮。
    合并解决 v1/v2「同赛道」与「大盘水位」高度共线、重复计价的顺周期放大问题。"""
    if inp.is_break_wave is True:
        return 85.0
    parts = []
    # 同赛道近期首日表现（最强子信号）
    if inp.peer_first_day_avg_pct is not None:
        parts.append(_clamp(50 - inp.peer_first_day_avg_pct * 2.0))  # +30%→10, 0→50, -15%→80
    if inp.peer_break_count is not None and inp.peer_total_count:
        parts.append(_clamp(40 + (inp.peer_break_count / inp.peer_total_count) * 55))
    # 大盘/板块水位
    mkt = [x for x in (inp.hsi_change_pct, inp.sector_change_pct) if x is not None]
    if mkt:
        avg = sum(mkt) / len(mkt)
        parts.append(_clamp(50 - avg * 5.0))  # +2%→40, 0→50, -3%→65
    # VHSI 波动率
    if inp.vhsi is not None:
        parts.append(_clamp(30 + inp.vhsi * 1.2))  # 15→48, 20→54, 30→66, 恐慌越高越易破
    if not parts:
        return None
    return sum(parts) / len(parts)

def _score_heat(inp: BreakInput) -> Optional[float]:
    """E7 超购热度（U型）：过冷不足额、过热纯散户，两端都易破发。（v1 设计最好的一项，保留）"""
    if inp.oversubscription is None:
        return None
    o = inp.oversubscription
    if o < 1:
        return 80.0          # 认购不足额，极易破发
    if o < 5:
        return 55.0          # 偏冷
    if o <= 50:
        return 30.0          # 健康区间，最不易破发
    if o <= 150:
        return 45.0          # 偏热
    return 60.0              # 过热(>150)，纯散户堆积，首日抛压大

FACTOR_FUNCS = {
    "pricing": _score_pricing,
    "preipo": _score_preipo,
    "float_value": _score_float_value,
    "source_dispersion": _score_source_dispersion,
    "support": _score_support,
    "market_beta": _score_market_beta,
    "heat": _score_heat,
}

FACTOR_LABELS = {
    "pricing": "定价与折溢价(根因)",
    "preipo": "Pre-IPO倍数+老股套现",
    "float_value": "首日流通市值(哑铃)",
    "source_dispersion": "货源分散度",
    "support": "承接力量(基石×成色+保荐人+绿鞋)",
    "market_beta": "市场环境β(同赛道+大盘+VHSI)",
    "heat": "超购热度(U型)",
}

def _prob_to_level(prob: float) -> str:
    if prob >= 60:
        return "高"
    if prob >= 40:
        return "中等偏高"
    if prob >= 25:
        return "中等偏低"
    if prob >= 12:
        return "低"
    return "很低"

def _pressure_to_first_day_range(pressure: float) -> tuple[float, float]:
    """把破发压力分（0~100）反推为首日涨幅期望区间%，供 D7 使用。
    压力0→约[+28,+52]；压力50→约[-3,+21]；压力100→约[-34,-10]。线性插值给区间。"""
    mid = 40 - pressure * 0.62      # 压力0→+40中值；压力100→约-22中值
    half = 12 + pressure * 0.10     # 压力越高，区间越宽（不确定性大）
    return round(mid - half, 1), round(mid + half, 1)

def predict_break(payload: dict) -> dict:
    """聚合七因子，输出破发概率区间 + 首日涨幅期望区间。"""
    inp = BreakInput(**{k: payload.get(k) for k in BreakInput.__annotations__})

    contributions = {}
    missing = []
    active_weight = 0.0
    weighted_sum = 0.0

    for key, func in FACTOR_FUNCS.items():
        score = func(inp)
        if score is None:
            missing.append(FACTOR_LABELS[key])
            continue
        w = WEIGHTS[key]
        active_weight += w
        weighted_sum += score * w
        contributions[FACTOR_LABELS[key]] = {"pressure": round(score, 1), "weight": w}

    if active_weight == 0:
        return {
            "name": inp.name, "code": inp.code,
            "error": "所有破发因子均无数据，无法预测。请至少提供定价折溢价、首日流通市值或市场β其一。",
            "missing_factors": missing,
        }

    # 权重归一（缺失因子的权重按比例摊到其余因子）
    pressure = weighted_sum / active_weight
    prob_center = pressure
    coverage = active_weight  # 0~1

    # 概率区间：数据越缺区间越宽；微盘(小流通市值)额外加宽（高波动 → 只加不确定性，不上移中心）
    band = 8 + (1 - coverage) * 20
    if inp.first_day_float_yi is not None and inp.first_day_float_yi < 2:
        band += 6

    prob_low = max(0.0, round(prob_center - band, 1))
    prob_high = min(100.0, round(prob_center + band, 1))

    fd_low, fd_high = _pressure_to_first_day_range(pressure)
    fd_mid = round((fd_low + fd_high) / 2, 1)

    # 闸门判定
    if prob_center >= 55:
        gate = "🔴 红灯：破发概率过高，建议放弃（此闸门优先于6D/D7）"
        gate_pass = False
    elif prob_center >= 40:
        gate = "🟡 黄灯：破发概率中等偏高，仅现金白嫖一手、首日见好就收"
        gate_pass = True
    else:
        gate = "🟢 绿灯：破发概率可控，可进入 6D/D7 正常评估"
        gate_pass = True

    result = {
        "name": inp.name, "code": inp.code,
        "break_probability_pct": round(prob_center, 1),
        "break_probability_range": f"{prob_low}%-{prob_high}%",
        "break_level": _prob_to_level(prob_center),
        "gate": gate,
        "gate_pass": gate_pass,
        "expected_first_day_range_pct": f"{fd_low}%~{fd_high}%",
        "expected_first_day_mid_pct": fd_mid,
        "factor_contributions": contributions,
        "data_coverage_pct": round(coverage * 100, 1),
        "missing_factors": missing,
        "note": "破发概率=七因子破发压力分的加权（缺失因子权重已按比例归一）。"
                "expected_first_day_mid_pct 可直接作为 decision_engine 的 expected_first_day_pct 输入，"
                "让 D7 期望值不再手填。missing_factors 里的项请用招股书/odds/web search 补齐后重算。",
    }

    # 暗盘 override：暗盘是首日破发相关性最高的领先指标，一旦可得直接覆盖模型预测的首日期望
    if inp.dark_pool_pct is not None:
        dp = inp.dark_pool_pct
        result["dark_pool_pct"] = dp
        result["expected_first_day_mid_pct_model"] = fd_mid
        result["expected_first_day_mid_pct"] = dp
        result["expected_first_day_range_pct"] = f"{round(dp - 3, 1)}%~{round(dp + 3, 1)}%（暗盘校准）"
        result["dark_pool_note"] = ("已用暗盘涨幅 override 模型预测的首日期望（暗盘是首日最强领先指标）。"
                                    "暗盘破发(<0)时应无视模型绿灯、按破发跑处理。")

    return result

def main() -> int:
    p = argparse.ArgumentParser(description="破发概率引擎 v3 D_break（打新第一道生死闸门）")
    p.add_argument("--json", required=True, help="破发因子 JSON 输入，见文件顶部因子说明")
    args = p.parse_args()
    print(json.dumps(predict_break(json.loads(args.json)), ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    sys.exit(main())
