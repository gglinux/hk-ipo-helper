#!/usr/bin/env python3
"""
破发概率引擎 — HK IPO Helper (D_break 生死闸门)

打新收益是【非对称】的：不破发顶多赚多赚少（有限上行），一旦破发是现金亏损+资金占用+
情绪打击（折了夫人又折兵）。所以「会不会破发」是应当【前置】的第一道生死闸门——
过不了这关，后面算期望值、算年化都是空中楼阁。

本引擎把散落在 6D 各维度里的破发线索【聚合】成一个显式的破发概率，并联动 D7：
  1. 输出【破发概率区间】（如 25-35%，中等偏低），当决策流程第一道闸门。
  2. 反推【首日涨幅期望区间】喂给 decision_engine 的 D7，替掉原来手填的数字。

设计原则：
- 六因子加权，同赛道近期新股表现是最强预测因子。
- 因子支持「未知」：数据缺失则该因子不参与、权重重新归一，并在输出标注数据缺口，绝不脑补。
- 输出区间而非单点，金融预测给区间更诚实。

因子与数据来源：
  同赛道近期首日表现 30% ← futu 近期表现 / web search（最强，如小马文远破发预警同赛道）
  大盘/板块当日水位   20% ← sentiment HSI / web search
  基石锁定比例        20% ← analyze cornerstone
  超购热度(U型)       15% ← web search 孖展（过冷/过热都不利）
  保荐人+绿鞋护盘     10% ← analyze sponsors
  估值/基本面          5% ← 招股书

用法：
    python3 breakeven_predictor.py --json '{...}'    # 见文件底部示例
可作库：from breakeven_predictor import predict_break
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional


# ============================================================
# 因子权重（合计 1.0；某因子为 None 时其权重按比例摊到其余因子）
# ============================================================
WEIGHTS = {
    "peer_performance": 0.30,   # 同赛道近期新股首日表现（最强）
    "market_water": 0.20,       # 大盘/板块当日水位
    "cornerstone_lock": 0.20,   # 基石锁定比例
    "subscription_heat": 0.15,  # 超购热度（U型）
    "sponsor_greenshoe": 0.10,  # 保荐人+绿鞋护盘
    "valuation": 0.05,          # 估值/基本面
}


@dataclass
class BreakInput:
    """破发预测输入。每项都可为 None（未知则不参与打分）。

    评分口径：每个因子给一个【破发压力分】0~100，越高越易破发。
    """
    name: str = ""
    code: str = ""
    # 同赛道近期新股首日表现：同赛道最近几只的平均首日涨幅%（负=破发潮，正=赚钱效应）
    peer_first_day_avg_pct: Optional[float] = None
    peer_break_count: Optional[int] = None      # 同赛道近期破发只数（辅助）
    peer_total_count: Optional[int] = None       # 同赛道近期总只数
    # 大盘水位：当日恒指/相关板块涨跌幅%；is_break_wave 标记连环破发潮
    hsi_change_pct: Optional[float] = None
    sector_change_pct: Optional[float] = None    # 所属板块当日涨跌%（如半导体/科技）
    is_break_wave: Optional[bool] = None         # 是否处于连环破发潮
    # 基石锁定比例：基石占发行比例%（越高抛压越小）
    cornerstone_pct: Optional[float] = None
    # 超购热度：孖展超购倍数（U型——过冷<1不足额易破，过热>200纯散户首日抛压大）
    oversubscription: Optional[float] = None
    # 保荐人护盘：is_tier1 顶级投行；has_greenshoe 有绿鞋（超额配股权）
    sponsor_is_tier1: Optional[bool] = None
    has_greenshoe: Optional[bool] = None
    # 估值/基本面：is_profitable 是否盈利；priced_at_premium 是否高估值/高价
    is_profitable: Optional[bool] = None
    priced_at_premium: Optional[bool] = None


# ============================================================
# 各因子 → 破发压力分（0~100，越高越易破发）
# ============================================================
def _score_peer(inp: BreakInput) -> Optional[float]:
    """同赛道近期表现：破发潮→高压力；赚钱效应→低压力。"""
    if inp.peer_first_day_avg_pct is None and inp.peer_total_count is None:
        return None
    score = 50.0
    # 主信号：同赛道平均首日涨幅
    if inp.peer_first_day_avg_pct is not None:
        # +30%→压力约10；0%→50；-15%→约80
        score = 50 - inp.peer_first_day_avg_pct * 2.0
    # 辅助：破发比例
    if inp.peer_break_count is not None and inp.peer_total_count:
        break_ratio = inp.peer_break_count / inp.peer_total_count
        score = max(score, 40 + break_ratio * 55)  # 破发比例越高压力越大
    return max(0.0, min(100.0, score))


def _score_market(inp: BreakInput) -> Optional[float]:
    """大盘/板块水位：破发潮或大盘急跌→高压力。"""
    vals = [x for x in (inp.hsi_change_pct, inp.sector_change_pct) if x is not None]
    if not vals and inp.is_break_wave is None:
        return None
    if inp.is_break_wave is True:
        return 85.0
    if not vals:
        return 50.0
    avg = sum(vals) / len(vals)
    # 板块/大盘 +2%→约35；0→50；-3%→约65
    return max(0.0, min(100.0, 50 - avg * 5.0))


def _score_cornerstone(inp: BreakInput) -> Optional[float]:
    """基石锁定比例：锁得越多抛压越小→压力越低。"""
    if inp.cornerstone_pct is None:
        return None
    # 0%→80（无基石高压力）；30%→50；50%→约33；≥50%强护盘
    return max(0.0, min(100.0, 80 - inp.cornerstone_pct * 1.0))


def _score_heat(inp: BreakInput) -> Optional[float]:
    """超购热度（U型）：过冷不足额、过热纯散户，两端都易破发。"""
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


def _score_sponsor(inp: BreakInput) -> Optional[float]:
    """保荐人+绿鞋护盘：Tier1+绿鞋→压力低。"""
    if inp.sponsor_is_tier1 is None and inp.has_greenshoe is None:
        return None
    score = 55.0
    if inp.sponsor_is_tier1 is True:
        score -= 15
    elif inp.sponsor_is_tier1 is False:
        score += 10
    if inp.has_greenshoe is True:
        score -= 10
    return max(0.0, min(100.0, score))


def _score_valuation(inp: BreakInput) -> Optional[float]:
    """估值/基本面：未盈利+高估值→波动大→压力略高。"""
    if inp.is_profitable is None and inp.priced_at_premium is None:
        return None
    score = 50.0
    if inp.is_profitable is False:
        score += 15
    elif inp.is_profitable is True:
        score -= 10
    if inp.priced_at_premium is True:
        score += 10
    return max(0.0, min(100.0, score))


FACTOR_FUNCS = {
    "peer_performance": _score_peer,
    "market_water": _score_market,
    "cornerstone_lock": _score_cornerstone,
    "subscription_heat": _score_heat,
    "sponsor_greenshoe": _score_sponsor,
    "valuation": _score_valuation,
}

FACTOR_LABELS = {
    "peer_performance": "同赛道近期新股首日表现",
    "market_water": "大盘/板块当日水位",
    "cornerstone_lock": "基石锁定比例",
    "subscription_heat": "超购热度(U型)",
    "sponsor_greenshoe": "保荐人+绿鞋护盘",
    "valuation": "估值/基本面",
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
    压力0→约[+30,+60]；压力50→约[-2,+12]；压力100→约[-25,-8]。线性插值给区间。"""
    mid = 40 - pressure * 0.62      # 压力0→+40中值；压力100→约-22中值
    half = 12 + pressure * 0.10     # 压力越高，区间越宽（不确定性大）
    low = round(mid - half, 1)
    high = round(mid + half, 1)
    return low, high


def predict_break(payload: dict) -> dict:
    """聚合六因子，输出破发概率区间 + 首日涨幅期望区间。"""
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
            "error": "所有破发因子均无数据，无法预测。请至少提供同赛道近期表现或大盘水位。",
            "missing_factors": missing,
        }

    # 权重归一（缺失因子的权重按比例摊到其余因子）
    pressure = weighted_sum / active_weight
    # 破发压力分 → 破发概率（压力分本身近似即为破发概率的中心，做轻微校准）
    prob_center = pressure
    # 概率区间：数据越全区间越窄
    coverage = active_weight  # 0~1
    band = 8 + (1 - coverage) * 20   # 数据越缺，区间越宽
    prob_low = max(0.0, round(prob_center - band, 1))
    prob_high = min(100.0, round(prob_center + band, 1))

    fd_low, fd_high = _pressure_to_first_day_range(pressure)

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

    return {
        "name": inp.name, "code": inp.code,
        "break_probability_pct": round(prob_center, 1),
        "break_probability_range": f"{prob_low}%-{prob_high}%",
        "break_level": _prob_to_level(prob_center),
        "gate": gate,
        "gate_pass": gate_pass,
        "expected_first_day_range_pct": f"{fd_low}%~{fd_high}%",
        "expected_first_day_mid_pct": round((fd_low + fd_high) / 2, 1),
        "factor_contributions": contributions,
        "data_coverage_pct": round(coverage * 100, 1),
        "missing_factors": missing,
        "note": "破发概率=各因子破发压力分的加权（缺失因子权重已按比例归一）。"
                "expected_first_day_mid_pct 可直接作为 decision_engine 的 expected_first_day_pct 输入，"
                "让 D7 期望值不再手填。missing_factors 里的项请用 web search 补齐后重算。",
    }


def main() -> int:
    p = argparse.ArgumentParser(description="破发概率引擎 D_break（打新第一道生死闸门）")
    p.add_argument("--json", required=True, help="破发因子 JSON 输入，见文件顶部")
    args = p.parse_args()
    print(json.dumps(predict_break(json.loads(args.json)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
