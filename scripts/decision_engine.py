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
# 成本常量（与 6D 模型 D6 保持一致）
# ============================================================
COST_FACTOR = 0.010077          # 硬性损耗系数：佣金+证监会+联交所+会财局
SELL_COMMISSION_HKD = 50.0      # 卖出侧固定佣金估算
DEFAULT_MARGIN_ANNUAL_RATE = 0.045   # 融资年化利率默认 4.5%
DEFAULT_MARGIN_DAYS = 7              # 默认计息天数

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
def breakeven_pct(entry_fee: float, lots: int, lot_market_value: float,
                  use_margin: bool = False,
                  margin_rate: float = DEFAULT_MARGIN_ANNUAL_RATE,
                  margin_days: int = DEFAULT_MARGIN_DAYS) -> float:
    """盈亏平衡涨幅%：首日涨多少才回本。"""
    total_mv = lot_market_value * lots
    if total_mv <= 0:
        return 0.0
    buy_cost = entry_fee * lots * COST_FACTOR
    interest = (entry_fee * lots * margin_rate * margin_days / 365) if use_margin else 0.0
    total_cost = buy_cost + interest + SELL_COMMISSION_HKD
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
    use_margin: bool = False
    margin_rate: float = DEFAULT_MARGIN_ANNUAL_RATE
    margin_days: int = DEFAULT_MARGIN_DAYS


@dataclass
class EVResult:
    name: str
    code: str
    lots: int
    capital_occupied: float          # 占用资金（入场费×手数）
    win_rate: float                  # 中签率（至少中一手，或按手数）
    expected_first_day_pct: float
    breakeven_pct: float
    expected_net_hkd: float          # 期望净收益（港元）
    expected_return_on_capital: float  # 期望收益率（相对占用资金）%
    verdict: str = ""
    notes: list[str] = field(default_factory=list)


def expected_value(inp: EVInput) -> EVResult:
    """计算单只票、指定手数的期望净收益与期望收益率。

    期望净收益 = 中签率 × 中签手数 × 一手市值 × 首日涨幅% − 买入手续费 − 融资利息 − 卖出佣金
    （简化：以「至少中一手」的中签率 × 手数近似期望中签手数；散户小额场景下足够决策用）
    """
    lots = max(1, inp.lots)
    capital = inp.entry_fee * lots
    total_mv = inp.lot_market_value * lots

    # 期望中签市值（保守用一手中签率线性近似期望手数）
    expected_hit_mv = inp.win_rate_1lot * total_mv
    gross_gain = expected_hit_mv * (inp.expected_first_day_pct / 100.0)

    # 买入手续费：港股现金申购未中签则资金原路退回、零费用，
    # 0.77% 硬成本只在【中签成交】时产生，故用中签率加权（与 sell_cost 一致）。
    buy_cost = capital * COST_FACTOR * inp.win_rate_1lot
    # 融资利息：只要动用孖展申购，无论中签与否都要付息（占用了融资额度）。
    interest = (capital * inp.margin_rate * inp.margin_days / 365) if inp.use_margin else 0.0
    # 卖出佣金只在中签时产生，用中签率加权
    sell_cost = SELL_COMMISSION_HKD * inp.win_rate_1lot
    net = gross_gain - buy_cost - interest - sell_cost

    be = breakeven_pct(inp.entry_fee, lots, inp.lot_market_value,
                       inp.use_margin, inp.margin_rate, inp.margin_days)
    roc = round(net / capital * 100, 3) if capital > 0 else 0.0

    notes = []
    if inp.use_margin:
        notes.append(f"融资{inp.margin_days}天@{inp.margin_rate*100:.1f}%年化，利息{interest:.0f}港元")
    if inp.expected_first_day_pct < be:
        notes.append(f"⚠️ 首日涨幅期望{inp.expected_first_day_pct:.1f}% < 盈亏平衡{be:.1f}%，现金申购期望为负")

    return EVResult(
        name=inp.name, code=inp.code, lots=lots,
        capital_occupied=round(capital, 2),
        win_rate=round(inp.win_rate_1lot, 4),
        expected_first_day_pct=inp.expected_first_day_pct,
        breakeven_pct=be,
        expected_net_hkd=round(net, 2),
        expected_return_on_capital=roc,
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

    # 期望值（现金一手 + 可选融资/多手）
    ev_input_common = dict(
        name=payload.get("name", ""), code=payload.get("code", ""),
        entry_fee=payload["entry_fee"], lot_market_value=payload["lot_market_value"],
        win_rate_1lot=payload["win_rate_1lot"],
        expected_first_day_pct=payload["expected_first_day_pct"],
    )
    scenarios = {}
    scenarios["现金一手"] = asdict(expected_value(EVInput(**ev_input_common, lots=1)))
    if payload.get("evaluate_margin"):
        scenarios["融资一手"] = asdict(expected_value(EVInput(**ev_input_common, lots=1, use_margin=True)))

    result["scenarios"] = scenarios
    cash1 = scenarios["现金一手"]
    result["verdict"] = _verdict_from_ev(cash1["expected_return_on_capital"], cash1["expected_net_hkd"])
    return result


def _verdict_from_ev(roc_pct: float, net_hkd: float) -> str:
    """基于期望收益率给散户的操作档位（与 6D 四选一对齐）。"""
    if net_hkd <= 0:
        return "撤退 (Avoid/Skip)：现金一手期望收益为负"
    if roc_pct >= 3.0:
        return "全力出击 (All-in)：期望收益率高，可考虑多户/加码"
    if roc_pct >= 1.0:
        return "现金摸鱼 (Cash Only)：正期望，现金一手/多户稳吃"
    return "防守性申购 (Speculative)：期望微正，白嫖一手即可"


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
        one = evaluate_one({**c, "evaluate_margin": False})
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
            "verdict": one["verdict"],
        })

    # 资金效率优先排序（每块钱期望收益）
    passed.sort(key=lambda x: x["roc_pct"], reverse=True)

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
