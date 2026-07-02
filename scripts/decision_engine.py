#!/usr/bin/env python3
"""
散户打新决策器 — HK IPO Helper (D7 决策引擎)

在 6D 质量评分之上，补齐散户打新第一性原理真正需要的三件事：

  1. 闸门制（Gate / 一票否决）：打新风险是「非线性、单项致命」的。任何一道闸门不过，
     无论 6D 总分多高，直接判 SKIP。消除线性加权掩盖致命项的问题。

  2. D7 期望值引擎（Expected Value）：散户赚的是「招股价→首日价」的价差期望，
     核心是「给定本金，每一块钱的期望回报」，而非公司质量分。
        一手期望净收益 = 一手中签率 × E(首日涨幅) × 一手市值 − 手续费 − 融资利息
     把「中签率 / 入场费 / 首日涨幅期望」乘起来，输出「每只票、每种打法」的期望收益率。

  3. 组合层（Portfolio）：多只新股认购期重叠时，散户本金有限、资金有档期。
     在本金约束下按「每股本金期望收益率」排序，给出「这些钱该打哪几只、各打几手」。

设计原则：本引擎只做计算与排序，不编造数据。所有输入（中签率/入场费/超购/首日涨幅预期）
必须来自 analyze / odds / 招股书 / web search，缺失则要求补齐或按保守区间给出并显式标注。

用法（命令行，便于 AI 或用户直接调用）：
    # 单只票的期望值 + 闸门判定（JSON 输入）
    python3 decision_engine.py eval --json '{...}'

    # 组合优化：给定本金和多只候选，输出打法建议
    python3 decision_engine.py portfolio --capital 30000 --json '[{...},{...}]'

也可作为库被 import：from decision_engine import evaluate_one, optimize_portfolio
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional

# ============================================================
# 成本常量（依据富途牛牛官方费率，2024/10/24 起）
# 来源：futuhkapp.com/cn/support/topic2_418
# ============================================================
# 中签成交时一次性收取：证监会征费+财汇局征费+联交所交易费+经纪佣金 ≈ 1.0085% × 中签金额
# （港股新股卖出时同样按此类交易费率，这里成本口径统一按买入侧一次计，卖出费在首日涨幅里体现净值）
DEAL_FEE_RATE = 0.010085        # 中签成交费率（仅中签时产生）
# 申购手续费（按笔，与是否中签无关）
FEE_ORDINARY_HANDLING = 0.0     # 普通申购（现金 / 富途融资）：富途免手续费
FEE_BANK_HANDLING = 100.0       # 银行融资申购：100 港元/笔
# 融资年利率（按日计息，无论中签与否都收）
FUTU_MARGIN_ANNUAL_RATE = 0.068     # 富途融资年利率 6.8%
DEFAULT_BANK_MARGIN_ANNUAL_RATE = 0.040  # 银行融资年利率（各行不同，以申购页为准，默认按 4% 估）
DEFAULT_MARGIN_DAYS = 7             # 默认计息天数（招股到公布结果约 6-8 天）

GROUP_A_MAX = 5_000_000         # 甲组上限（公开发售认购额 ≤500万）


# ============================================================
# 闸门制（一票否决）
# ============================================================
@dataclass
class GateInput:
    """闸门判定所需的定性/定量信号（来自招股书精读 + web search + analyze）。"""
    market_crash: Optional[bool] = None      # D0：是否处于连环破发潮 / 大盘急跌
    sponsor_is_notorious: Optional[bool] = None  # 保荐人是否劣迹（近一年破发率>60%/妖庄惯犯）
    no_real_cornerstone: Optional[bool] = None   # 是否无实质基石（清一色亲友团/无名机构）
    shell_structure: Optional[bool] = None       # 是否疑似老千股结构（突击分红/股权高度集中/关联交易黑洞）
    breakeven_pct: Optional[float] = None        # 盈亏平衡涨幅%（D6），过高说明成本吞噬空间


# 闸门阈值
BREAKEVEN_HARD_LIMIT = 8.0   # 盈亏平衡线 >8% 视为成本致命（现金打新几乎不可能覆盖）


def check_gates(g: GateInput) -> list[str]:
    """返回所有被触发的一票否决理由；空列表代表全部通过。"""
    failed = []
    if g.market_crash is True:
        failed.append("D0市场水位：处于连环破发潮/大盘急跌，新股集体承压 → 一票否决")
    if g.sponsor_is_notorious is True:
        failed.append("保荐人劣迹：近一年破发率过高/妖庄惯犯 → 一票否决")
    if g.no_real_cornerstone is True:
        failed.append("无实质基石：清一色亲友团/无名机构，无机构背书 → 一票否决")
    if g.shell_structure is True:
        failed.append("疑似老千结构：突击分红/股权高度集中/关联交易黑洞 → 一票否决")
    if g.breakeven_pct is not None and g.breakeven_pct > BREAKEVEN_HARD_LIMIT:
        failed.append(f"成本致命：盈亏平衡线 {g.breakeven_pct:.1f}% > {BREAKEVEN_HARD_LIMIT}%，成本吞噬首日空间 → 一票否决")
    return failed


# ============================================================
# 成本与盈亏平衡
# ============================================================
def _resolve_funding(funding: str) -> tuple[float, float, float]:
    """按申购方式返回 (手续费, 融资年利率, 是否融资)。
    funding: 'cash' 现金 | 'futu_margin' 富途融资 | 'bank_margin' 银行融资
    """
    if funding == "futu_margin":
        return FEE_ORDINARY_HANDLING, FUTU_MARGIN_ANNUAL_RATE, True
    if funding == "bank_margin":
        return FEE_BANK_HANDLING, DEFAULT_BANK_MARGIN_ANNUAL_RATE, True
    # 默认现金：普通申购免手续费、无利息
    return FEE_ORDINARY_HANDLING, 0.0, False


def breakeven_pct(entry_fee: float, lots: int, lot_market_value: float,
                  funding: str = "cash", margin_days: int = DEFAULT_MARGIN_DAYS,
                  bank_rate: float | None = None) -> float:
    """盈亏平衡涨幅%：中签后首日涨多少才回本（含成交费、手续费、融资利息）。"""
    total_mv = lot_market_value * lots
    if total_mv <= 0:
        return 0.0
    handling, rate, use_margin = _resolve_funding(funding)
    if funding == "bank_margin" and bank_rate is not None:
        rate = bank_rate
    capital = entry_fee * lots
    deal_fee = total_mv * DEAL_FEE_RATE          # 中签成交费
    interest = (capital * rate * margin_days / 365) if use_margin else 0.0
    total_cost = deal_fee + handling + interest
    return round(total_cost / total_mv * 100, 3)


# ============================================================
# D7 期望值引擎
# ============================================================
@dataclass
class EVInput:
    """单只票、单种打法的期望值输入。"""
    name: str
    code: str
    entry_fee: float                 # 一手入场费（港元）
    lot_market_value: float          # 一手市值 = 每手股数 × 发行价（不含手续费）
    win_rate_1lot: float             # 一手中签率（0-1），来自 odds/allotment
    expected_first_day_pct: float    # 首日涨幅期望%（来自超购/情绪/回测，可给区间中值）
    lots: int = 1                    # 申购手数
    funding: str = "cash"            # cash 现金 / futu_margin 富途融资 / bank_margin 银行融资
    margin_days: int = DEFAULT_MARGIN_DAYS   # 资金占用天数（招股到公布结果约 6-8 天）
    bank_rate: float | None = None   # 银行融资年利率（银行申购时可指定）


@dataclass
class EVResult:
    name: str
    code: str
    lots: int
    funding: str                     # 申购方式
    capital_occupied: float          # 占用资金（入场费×手数）
    holding_days: int                # 资金占用天数
    win_rate: float                  # 一手中签率
    expected_first_day_pct: float
    breakeven_pct: float
    expected_net_hkd: float          # 期望净收益（港元）
    expected_return_on_capital: float  # 期望收益率（相对占用资金）%
    annualized_return_pct: float     # 年化收益率%（把占用天数折算成一年）
    verdict: str = ""
    notes: list[str] = field(default_factory=list)


def expected_value(inp: EVInput) -> EVResult:
    """计算单只票、指定手数的期望净收益、期望收益率与年化收益率。

    期望净收益 = 中签率×一手市值×首日涨幅%  −  成交费(中签才收)  −  申购手续费  −  融资利息(占款就收)
    - 成交费(1.0085%)：只在中签成交时产生，故乘中签率。
    - 申购手续费：普通申购=0；银行融资=100港元/笔，按笔收（与中签无关）。
    - 融资利息：动用孖展即产生，无论中签与否都付（不乘中签率）。
    """
    lots = max(1, inp.lots)
    capital = inp.entry_fee * lots
    total_mv = inp.lot_market_value * lots
    handling, rate, use_margin = _resolve_funding(inp.funding)
    if inp.funding == "bank_margin" and inp.bank_rate is not None:
        rate = inp.bank_rate

    # 中签期望毛收益
    expected_hit_mv = inp.win_rate_1lot * total_mv
    gross_gain = expected_hit_mv * (inp.expected_first_day_pct / 100.0)

    # 成交费：中签成交才收 1.0085%，故乘中签率
    deal_fee = total_mv * DEAL_FEE_RATE * inp.win_rate_1lot
    # 申购手续费：按笔收，与中签无关（普通申购=0，银行融资=100）
    handling_fee = handling
    # 融资利息：占用融资额度就收，无论中签与否
    interest = (capital * rate * inp.margin_days / 365) if use_margin else 0.0

    net = gross_gain - deal_fee - handling_fee - interest

    be = breakeven_pct(inp.entry_fee, lots, inp.lot_market_value,
                       funding=inp.funding, margin_days=inp.margin_days, bank_rate=inp.bank_rate)
    roc = round(net / capital * 100, 3) if capital > 0 else 0.0
    # 年化：把占用 margin_days 天的收益率折算成 365 天（单利折算，直观可比）
    annualized = round(roc * 365 / inp.margin_days, 2) if inp.margin_days > 0 else 0.0

    funding_cn = {"cash": "现金", "futu_margin": "富途融资", "bank_margin": "银行融资"}.get(inp.funding, inp.funding)
    notes = []
    if use_margin:
        notes.append(f"{funding_cn}：{rate*100:.1f}%年化×{inp.margin_days}天，利息{interest:.1f}港元（无论中签与否都收）")
    if inp.funding == "bank_margin":
        notes.append("银行融资额外手续费 100 港元/笔")
    if net <= 0:
        notes.append(f"⚠️ 期望净收益为负（{net:.1f}港元），不建议以此方式申购")

    return EVResult(
        name=inp.name, code=inp.code, lots=lots, funding=funding_cn,
        capital_occupied=round(capital, 2),
        holding_days=inp.margin_days,
        win_rate=round(inp.win_rate_1lot, 4),
        expected_first_day_pct=inp.expected_first_day_pct,
        breakeven_pct=be,
        expected_net_hkd=round(net, 2),
        expected_return_on_capital=roc,
        annualized_return_pct=annualized,
        notes=notes,
    )


def evaluate_one(payload: dict) -> dict:
    """单只票：闸门判定 + 一手/多手期望值。payload 见文件顶部示例。"""
    gate = GateInput(**{k: payload.get(k) for k in GateInput.__annotations__})

    # 闸门先行
    be_for_gate = None
    if all(payload.get(k) is not None for k in ("entry_fee", "lot_market_value")):
        be_for_gate = breakeven_pct(payload["entry_fee"], 1, payload["lot_market_value"])
    gate.breakeven_pct = be_for_gate
    gate_failures = check_gates(gate)

    result = {
        "name": payload.get("name"),
        "code": payload.get("code"),
        "gate_passed": len(gate_failures) == 0,
        "gate_failures": gate_failures,
    }
    if gate_failures:
        result["verdict"] = "撤退 (Avoid/Skip)"
        result["reason"] = "触发一票否决闸门，无论 6D 质量分多高都不参与。"
        return result

def evaluate_one(payload: dict) -> dict:
    """单只票：闸门判定 + 三种申购方式（现金/富途融资/银行融资）期望值对比。"""
    gate = GateInput(**{k: payload.get(k) for k in GateInput.__annotations__})

    # 闸门先行（用现金一手的盈亏平衡线判成本闸门）
    be_for_gate = None
    if all(payload.get(k) is not None for k in ("entry_fee", "lot_market_value")):
        be_for_gate = breakeven_pct(payload["entry_fee"], 1, payload["lot_market_value"], funding="cash")
    gate.breakeven_pct = be_for_gate
    gate_failures = check_gates(gate)

    result = {
        "name": payload.get("name"),
        "code": payload.get("code"),
        "gate_passed": len(gate_failures) == 0,
        "gate_failures": gate_failures,
    }
    if gate_failures:
        result["verdict"] = "撤退 (Avoid/Skip)"
        result["reason"] = "触发一票否决闸门，无论 6D 质量分多高都不参与。"
        return result

    days = payload.get("margin_days", DEFAULT_MARGIN_DAYS)
    common = dict(
        name=payload.get("name", ""), code=payload.get("code", ""),
        entry_fee=payload["entry_fee"], lot_market_value=payload["lot_market_value"],
        win_rate_1lot=payload["win_rate_1lot"],
        expected_first_day_pct=payload["expected_first_day_pct"],
        margin_days=days,
    )
    # 三种申购方式各算一手，便于横向对比
    scenarios = {
        "现金一手": asdict(expected_value(EVInput(**common, lots=1, funding="cash"))),
        "富途融资一手": asdict(expected_value(EVInput(**common, lots=1, funding="futu_margin"))),
    }
    if payload.get("bank_rate") is not None or payload.get("evaluate_bank"):
        scenarios["银行融资一手"] = asdict(expected_value(
            EVInput(**common, lots=1, funding="bank_margin", bank_rate=payload.get("bank_rate"))))

    result["scenarios"] = scenarios
    cash1 = scenarios["现金一手"]
    result["verdict"] = _verdict_from_ev(cash1["annualized_return_pct"], cash1["expected_net_hkd"])
    # 提示最优申购方式
    best = max(scenarios.items(), key=lambda kv: kv[1]["expected_net_hkd"])
    result["best_funding"] = f"{best[0]}（期望净收益 {best[1]['expected_net_hkd']} 港元，年化 {best[1]['annualized_return_pct']}%）"
    return result


def _verdict_from_ev(annualized_pct: float, net_hkd: float) -> str:
    """基于【年化收益率】给散户的操作档位。
    打新占款仅约一周，年化更能反映资金效率：现金申购只要期望为正、年化跑赢无风险利率即值得。
    """
    if net_hkd <= 0:
        return "撤退 (Avoid/Skip)：现金一手期望收益为负"
    if annualized_pct >= 50.0:
        return "全力出击 (All-in)：年化极高，可考虑多户/加码"
    if annualized_pct >= 15.0:
        return "现金摸鱼 (Cash Only)：年化可观，现金一手/多户稳吃"
    return "防守性申购 (Speculative)：年化偏低，白嫖一手即可"


# ============================================================
# 组合层
# ============================================================
def optimize_portfolio(capital: float, candidates: list[dict]) -> dict:
    """本金约束下的组合优化。

    策略（贴合散户与红鞋机制）：
    1. 先过闸门，淘汰一票否决标的。
    2. 每只按「现金一手」计算期望净收益与占用资金。
    3. 按「期望收益率(每块钱期望)」降序 = 资金效率优先。
    4. 在本金约束下贪心选入「一手」；红鞋机制下小散户最优通常是「多只各一手」而非重仓单只。
    5. 剩余资金提示可否加码次优标的。
    """
    passed, rejected = [], []
    for c in candidates:
        one = evaluate_one({**c})
        if not one.get("gate_passed"):
            rejected.append({"name": c.get("name"), "code": c.get("code"),
                             "reason": one.get("gate_failures")})
            continue
        cash1 = one["scenarios"]["现金一手"]
        passed.append({
            "name": c.get("name"), "code": c.get("code"),
            "entry_fee": c["entry_fee"],
            "win_rate": cash1["win_rate"],
            "expected_first_day_pct": cash1["expected_first_day_pct"],
            "breakeven_pct": cash1["breakeven_pct"],
            "expected_net_hkd": cash1["expected_net_hkd"],
            "roc_pct": cash1["expected_return_on_capital"],
            "annualized_return_pct": cash1["annualized_return_pct"],
            "verdict": one["verdict"],
        })

    # 资金效率优先排序（年化收益率降序）
    passed.sort(key=lambda x: x["annualized_return_pct"], reverse=True)

    # 贪心：本金约束下「多只各一手」
    plan = []
    remaining = capital
    total_expected = 0.0
    for p in passed:
        if p["expected_net_hkd"] <= 0:
            continue  # 期望为负不打
        if p["entry_fee"] <= remaining:
            plan.append({**p, "action": "现金申购 1 手"})
            remaining -= p["entry_fee"]
            total_expected += p["expected_net_hkd"]

    return {
        "capital": capital,
        "plan": plan,
        "capital_used": round(capital - remaining, 2),
        "capital_remaining": round(remaining, 2),
        "portfolio_expected_net_hkd": round(total_expected, 2),
        "portfolio_expected_roc_pct": round(total_expected / capital * 100, 3) if capital > 0 else 0.0,
        "ranked_candidates": passed,
        "rejected_by_gate": rejected,
        "note": "红鞋机制下小散户最优通常是『多只各一手』分散中签，而非重仓单只。"
                "期望为负的标的已自动剔除。融资打新需另行评估利息成本。",
    }


# ============================================================
# CLI
# ============================================================
def main() -> int:
    p = argparse.ArgumentParser(description="散户打新决策器（闸门制 + D7期望值 + 组合层）")
    sub = p.add_subparsers(dest="cmd")

    pe = sub.add_parser("eval", help="单只票：闸门判定 + 期望值")
    pe.add_argument("--json", required=True, help="单只票的 JSON 输入")

    pp = sub.add_parser("portfolio", help="组合优化：本金约束下打哪几只")
    pp.add_argument("--capital", type=float, required=True, help="可用本金（港元）")
    pp.add_argument("--json", required=True, help="候选票列表的 JSON")

    args = p.parse_args()
    if args.cmd == "eval":
        print(json.dumps(evaluate_one(json.loads(args.json)), ensure_ascii=False, indent=2))
    elif args.cmd == "portfolio":
        print(json.dumps(optimize_portfolio(args.capital, json.loads(args.json)), ensure_ascii=False, indent=2))
    else:
        p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
