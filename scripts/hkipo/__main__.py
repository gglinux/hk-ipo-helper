#!/usr/bin/env python3
"""
港股打新研究助手 CLI

用法:
    ./hkipo <module> <command> [args...]

模块:
    aipo        - AiPO 数据（孖展、评级、暗盘、基石等）
    jisilu      - 集思录历史数据
    tradesmart  - TradeSmart 入场费数据
    allotment   - 中签率预测
    odds        - 中签率表格（不同超购×不同手数）
    calendar    - 资金日历（踩踏/释放规划）
    ah          - A+H 折价计算
    hkex        - 港交所披露易（招股书）
    sentiment   - 市场情绪（VHSI、保荐人历史）

别名:
    overview    → 当前招股一览
    margin      → aipo margin-list
    rating      → aipo rating-list
    grey        → aipo grey-list
    history     → jisilu list
    vhsi        → sentiment vhsi
    sponsor     → sentiment sponsor

示例:
    ./hkipo overview
    ./hkipo aipo margin-detail 02692
    ./hkipo odds --oversub 300 --price 73.68
    ./hkipo calendar
    ./hkipo ah compare 02692 --price 73.68 --name 兆威机电
    ./hkipo jisilu list --sponsor 招银国际
"""

import sys
import os

# 添加模块路径
sys.path.insert(0, os.path.dirname(__file__))


def show_overview():
    """显示当前招股 IPO 概览（纯数据，不做筛选判断）。
    数据源优先级：AAStocks（主力，含入场费/暗盘日）→ aipo（备用，含孖展热度）→ 港交所（官方兜底）。"""

    # --- 首选：AAStocks 招股中/即将上市列表（主力源，实测稳定）---
    try:
        from aastocks import get_upcoming_ipos
        ipos = get_upcoming_ipos()
        if ipos:
            print("=" * 60)
            print("当前招股 / 即将上市 IPO 一览（数据源：AAStocks 主力）")
            print("=" * 60)
            for ipo in ipos:
                name = ipo.get("name_tc", "")
                code = ipo.get("code") or ipo.get("symbol", "")
                print(f"\n📈 {name} ({code})")
                if ipo.get("industry"):
                    print(f"   行业: {ipo['industry']}")
                if ipo.get("offer_price_range"):
                    print(f"   招股价: {ipo['offer_price_range']}")
                if ipo.get("entry_fee"):
                    print(f"   入场费: {ipo['entry_fee']:.0f} 港元 (每手 {ipo.get('lot_size', '?')} 股)")
                if ipo.get("subscription_deadline"):
                    print(f"   认购截止: {ipo['subscription_deadline']}")
                if ipo.get("grey_market_date"):
                    print(f"   暗盘: {ipo['grey_market_date']}")
                if ipo.get("listing_date"):
                    print(f"   上市日期: {ipo['listing_date']}")
            print("\n" + "=" * 60)
            print("💡 单只深度数据（保荐人/基石/孖展）用 analyze <代码>；孖展实时热度招股期内 AAStocks 会更新。")
            return
    except Exception as e:
        print(f"⚠️  AAStocks 数据源不可用（{type(e).__name__}），尝试降级到 aipo。\n")

    # --- 备用：aipo 孖展列表（含热度，但该源可能被 DNS 屏蔽）---
    try:
        from aipo import fetch_margin_list, fetch_ipo_brief
        ipos = fetch_margin_list()
        if ipos:
            print("=" * 60)
            print("当前招股 IPO 一览（数据源：aipo 备用，含孖展热度）")
            print("=" * 60)
            ipos.sort(key=lambda x: x.total_margin if hasattr(x, 'total_margin') else 0, reverse=True)
            for ipo in ipos:
                code = ipo.code if hasattr(ipo, 'code') else ipo.get('code', '')
                name = ipo.name if hasattr(ipo, 'name') else ipo.get('name', '')
                margin = ipo.total_margin if hasattr(ipo, 'total_margin') else ipo.get('total_margin', 0)
                listing = ipo.listing_date if hasattr(ipo, 'listing_date') else ipo.get('listing_date', '')
                min_cap = 0
                try:
                    brief = fetch_ipo_brief(code)
                    if brief:
                        min_cap = brief.get('minimum_capital', 0) if isinstance(brief, dict) else getattr(brief, 'minimum_capital', 0)
                except (KeyError, TypeError, AttributeError):
                    pass
                print(f"\n📈 {name} ({code})")
                print(f"   孖展: {margin:.2f} 亿港元")
                print(f"   上市日期: {listing}")
                if min_cap:
                    print(f"   入场费: {min_cap:.0f} 港元")
            print("\n" + "=" * 60)
            return
    except Exception as e:
        print(f"⚠️  aipo 数据源也不可用（{type(e).__name__}），降级到港交所官方源。\n")

    # --- 兜底：港交所在审 IPO（官方源，最稳）+ 集思录入场费 ---
    try:
        from hkex import fetch_hkex_active_ipos_sync, get_prospectus_url
        actives = fetch_hkex_active_ipos_sync()
    except Exception as e:
        print(f"❌ 港交所数据源也不可用（{type(e).__name__}）。请改用 web search 查询在招港股新股。")
        return

    # 集思录入场费（可选增强，挂了不影响）
    fee_map = {}
    try:
        from jisilu import fetch_jisilu_history
        for h in fetch_jisilu_history(limit=60):
            c = str(h.get("code") or "").zfill(5)
            if c and h.get("entry_fee"):
                fee_map[c] = h.get("entry_fee")
    except Exception:
        pass

    print("=" * 60)
    print("当前处理中的 IPO（数据源：港交所披露易，官方兜底）")
    print("=" * 60)
    if not actives:
        print("当前无处理中的 IPO。")
        return
    for ipo in actives[:25]:
        phip = "✓有聆讯后资料" if ipo.has_phip else "✗仅申请版本"
        print(f"\n📄 {ipo.name}")
        print(f"   提交日期: {ipo.submit_date} | 板块: {ipo.board} | {phip}")
        if ipo.stock_code:
            code5 = str(ipo.stock_code).zfill(5)
            print(f"   股票代码: {ipo.stock_code}")
            if code5 in fee_map:
                print(f"   入场费(集思录): {fee_map[code5]} 港元")
        url = get_prospectus_url(ipo)
        if url:
            print(f"   招股书: {url}")
    print("\n" + "=" * 60)
    print("💡 孖展/超购/暗盘等实时热度数据需 web search 补充（AAStocks 与 aipo 源当前均不可用）。")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    
    module = sys.argv[1]
    remaining_args = sys.argv[2:]
    
    # 特殊命令
    if module == 'overview':
        show_overview()
        return
    
    # 别名映射
    aliases = {
        'margin': ('aipo', ['margin-list']),
        'rating': ('aipo', ['rating-list']),
        'grey': ('aipo', ['grey-list']),
        'history': ('jisilu', ['list']),
        'vhsi': ('sentiment', ['vhsi']),  # legacy alias
        'hsi': ('sentiment', ['vhsi']),
        'sponsor': ('sentiment', ['sponsor']),
    }
    
    if module in aliases:
        module, prepend_args = aliases[module]
        remaining_args = prepend_args + remaining_args
    
    # 模块分发
    if module == 'aipo':
        from aipo import main as aipo_main
        aipo_main(remaining_args)
    
    elif module == 'jisilu':
        from jisilu import main as jisilu_main
        jisilu_main(remaining_args)
    
    elif module == 'futu':
        from futu import main as futu_main
        futu_main(remaining_args)
    
    elif module == 'tradesmart':
        from tradesmart import main as tradesmart_main
        tradesmart_main(remaining_args)
    
    elif module == 'allotment':
        from allotment import main as allotment_main
        allotment_main(remaining_args)
    
    elif module == 'ah':
        # A+H 折价计算
        import json
        from ah import fetch_ah_comparison
        if len(remaining_args) >= 1 and remaining_args[0] == 'compare':
            # 解析参数
            code = None
            price = None
            name = None
            for i, arg in enumerate(remaining_args[1:]):
                if arg == '--price' and i + 2 < len(remaining_args):
                    price = float(remaining_args[i + 2])
                elif arg == '--name' and i + 2 < len(remaining_args):
                    name = remaining_args[i + 2]
                elif not arg.startswith('--') and code is None:
                    code = arg
            if code and price and name:
                result = fetch_ah_comparison(code, price, name)
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                print("用法: cli.py ah compare <代码> --price <发行价> --name <公司名>")
        else:
            print("用法: cli.py ah compare <代码> --price <发行价> --name <公司名>")
    
    elif module == 'hkex':
        # 港交所披露易
        import json
        from hkex import fetch_hkex_active_ipos_sync, get_prospectus_url
        if len(remaining_args) >= 1 and remaining_args[0] == 'active':
            ipos = fetch_hkex_active_ipos_sync()
            for ipo in ipos[:10]:
                url = get_prospectus_url(ipo)
                print(f"{ipo.name}")
                print(f"  提交日期: {ipo.submit_date}")
                print(f"  状态: {ipo.status_cn}")
                if ipo.stock_code:
                    print(f"  股票代码: {ipo.stock_code}")
                if url:
                    print(f"  招股书: {url}")
                print()
        else:
            print("用法: cli.py hkex active")
    
    elif module == 'sentiment':
        from sentiment import main as sentiment_main
        sentiment_main(remaining_args)
    
    elif module == 'etnet':
        # 保荐人统计（etnet 经济通数据源）
        import json
        from etnet import fetch_sponsor_rankings, get_sponsor_stats
        
        subcommand = remaining_args[0] if remaining_args else 'list'
        
        if subcommand == 'list':
            # 获取保荐人排名列表
            output_json = '--json' in remaining_args
            limit = 20
            for i, arg in enumerate(remaining_args):
                if arg == '--limit' and i + 1 < len(remaining_args):
                    limit = int(remaining_args[i + 1])
            
            try:
                sponsors = fetch_sponsor_rankings()
            except Exception as e:
                print(f"获取 etnet 数据失败: {e}", file=sys.stderr)
                sys.exit(1)
            
            if not sponsors:
                print("获取 etnet 数据失败: 无数据返回", file=sys.stderr)
                sys.exit(1)
            
            sponsors = sponsors[:limit]
            
            if output_json:
                print(json.dumps([s.to_dict() for s in sponsors], ensure_ascii=False, indent=2))
            else:
                print(f"{'保荐人':<25} {'IPO数':>6} {'首日胜率':>10} {'平均首日':>10}")
                print("-" * 60)
                for s in sponsors:
                    print(f"{s.sponsor_name:<25} {s.ipo_count:>6} {s.first_day_up_rate:>9.1f}% {s.avg_first_day_change:>+9.2f}%")
        
        elif subcommand == 'search':
            # 搜索特定保荐人
            name = None
            for i, arg in enumerate(remaining_args):
                if arg == '--name' and i + 1 < len(remaining_args):
                    name = remaining_args[i + 1]
            
            if not name:
                print("用法: ./hkipo etnet search --name <保荐人名称>", file=sys.stderr)
                sys.exit(1)
            
            result = get_sponsor_stats(name)
            if result:
                print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            else:
                print(f"未找到保荐人: {name}", file=sys.stderr)
                sys.exit(1)
        
        else:
            print(f"未知子命令: {subcommand}")
            print("可用: list, search")
            sys.exit(1)
    
    elif module == 'odds':
        # 中签率表格（调用 allotment table）
        import json
        from allotment import predict_allotment_table, IPOData
        # 解析参数
        oversub = 100
        price = 10.0
        lot_size = 500
        mechanism = 'A'
        output_json = False
        for i, arg in enumerate(remaining_args):
            if arg == '--oversub' and i + 1 < len(remaining_args):
                oversub = float(remaining_args[i + 1])
            elif arg == '--price' and i + 1 < len(remaining_args):
                price = float(remaining_args[i + 1])
            elif arg == '--lot-size' and i + 1 < len(remaining_args):
                lot_size = int(remaining_args[i + 1])
            elif arg == '--mechanism' and i + 1 < len(remaining_args):
                mechanism = remaining_args[i + 1].upper()
            elif arg == '--json':
                output_json = True
        
        entry_fee = lot_size * price * 1.01
        ipo_data: IPOData = {
            'offer_price': price,
            'lot_size': lot_size,
            'entry_fee': entry_fee,
            'mechanism': mechanism,
        }
        results = predict_allotment_table(ipo_data, oversub)
        
        if output_json:
            print(json.dumps(results, indent=2, ensure_ascii=False))
        else:
            print(f"📊 中签率表格（超购 {oversub}x，机制{mechanism}）\n")
            print(f"{'手数':>6} │ {'金额':>12} │ {'中签率':>8} │ 分组")
            print("───────┼──────────────┼──────────┼─────")
            for r in results:
                amt = int(entry_fee * r['lots'])
                print(f"{r['lots']:>6} │ {amt:>12,} │ {r['probability_pct']:>8} │ {r['group']}")
            print(f"\n⚠️ 基于 TradeSmart 算法预测，实际以官方公告为准")
    
    elif module == 'calendar':
        # 资金日历（截止日期分组）
        import json
        from ipo_calendar import fetch_calendar
        result = fetch_calendar()
        if '--json' in remaining_args:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print("📅 新股资金日历\n")
            print("按截止日期分组，帮你规划资金：\n")
            for round_data in result.get("rounds", []):
                deadline = round_data.get("deadline", "未知")
                ipos = round_data.get("ipos", [])
                total_fee = sum(ipo.get("entry_fee", 0) for ipo in ipos)
                print(f"🗓️  截止: {deadline}")
                print(f"   本轮共 {len(ipos)} 只，入场费合计 ~{total_fee:,} HKD")
                for ipo in ipos:
                    print(f"   - {ipo['name']} ({ipo['code']}) | 入场费 {ipo.get('entry_fee', '?'):,} | 上市 {ipo.get('listing_date', '?')}")
                print()
    
    elif module == 'analyze':
        # 一键分析单只 IPO
        # 数据源优先级：AAStocks（主力）→ aipo（备用补缺）→ web search 兜底（_fallback 标注）
        import json
        if not remaining_args:
            print("用法: ./hkipo analyze <代码>")
            sys.exit(1)
        code = remaining_args[0]
        # AAStocks 用纯数字 symbol（去掉 .HK 和前导零无关，站点用带零代码如 00668）
        symbol = code.replace(".HK", "").replace(".hk", "").strip()

        from jisilu import fetch_jisilu_history
        from ah import fetch_ah_comparison

        result = {"code": code}
        fallback = {}
        source_used = {}

        def _try(source_name, fn):
            """执行取数，失败不抛异常，只在 fallback 台账里标记。"""
            try:
                return fn()
            except Exception as e:  # noqa: BLE001
                fallback[source_name] = f"数据源异常（{type(e).__name__}）"
                return None

        # ============ 主力源：AAStocks 单只详情 ============
        def _aastocks_detail():
            from aastocks import get_ipo_detail
            return get_ipo_detail(symbol)
        aa = _try("aastocks", _aastocks_detail)

        sponsor = None
        if aa:
            source_used["basic/sponsor/cornerstone"] = "AAStocks"
            # 基本信息
            result["brief"] = {
                "name": aa.get("name_tc") or aa.get("name_en"),
                "industry": aa.get("industry"),
                "offer_price": aa.get("offer_price_range"),
                "market_cap": aa.get("market_cap_range"),
                "lot_size": aa.get("lot_size"),
                "public_offer_shares": aa.get("public_offer_shares"),
                "sponsors": aa.get("sponsors"),
                "subscription_period": aa.get("subscription_period"),
                "listing_date": aa.get("listing_date"),
            }
            if aa.get("sponsors"):
                sponsor = aa["sponsors"][0].split("(")[0].strip()
            # 基石 / 机构投资者
            inv = aa.get("institutional_investors") or []
            if inv:
                result["cornerstone"] = {
                    "count": len(inv),
                    "investors": [{"name": i.get("name"), "type": i.get("type"), "amount": i.get("amount")} for i in inv[:8]],
                }
            else:
                # 无基石是重大信号（影响 D3 权重20% + 闸门「无实质基石」判定），必须显式提示，不能静默省略
                fallback["cornerstone"] = "AAStocks 未登记基石投资者。请用 web search 确认是『确实无基石认购』（D3应显著扣分/触发闸门）还是『数据未更新』"
            # 孖展
            md = aa.get("margin_data") or []
            if md:
                result["margin"] = {
                    "broker_count": len(md),
                    "brokers": [{"broker": m.get("broker"), "amount": m.get("financing_amount"), "rate": m.get("interest_rate")} for m in md[:8]],
                }
            else:
                fallback["margin"] = "AAStocks 暂无孖展数据（通常招股中后期才有），请用 web search 查最新孖展/超购倍数"

        # ============ 备用源：aipo 补缺（AAStocks 拿不到时才尝试）============
        if not aa:
            def _aipo_brief():
                from aipo import fetch_ipo_brief
                return fetch_ipo_brief(code)
            brief = _try("aipo_brief", _aipo_brief)
            if brief:
                source_used["basic"] = "aipo(备用)"
                result["brief"] = {
                    "name": brief.get("principal_activities", "")[:50],
                    "industry": brief.get("industry"),
                    "pe": brief.get("pe"),
                    "market_cap": brief.get("market_cap"),
                    "offer_price": brief.get("ipo_price_ceiling") or brief.get("ipo_pricing"),
                    "entry_fee": brief.get("minimum_capital"),
                    "sponsors": brief.get("sponsors"),
                    "listing_date": brief.get("listing_date"),
                }
                if brief.get("sponsors"):
                    sp = brief["sponsors"]
                    sponsor = (sp[0] if isinstance(sp, list) else str(sp)).split(",")[0].strip()

            def _aipo_margin():
                from aipo import fetch_margin_detail
                return fetch_margin_detail(code)
            margin = _try("aipo_margin", _aipo_margin)
            if margin:
                result["margin"] = {
                    "total_billion": margin.total_margin,
                    "top_broker": margin.broker_margins[0].broker_name if margin.broker_margins else None,
                    "broker_count": len(margin.broker_margins),
                }

            def _aipo_cornerstone():
                from aipo import fetch_cornerstone_investors
                return fetch_cornerstone_investors(code)
            cs = _try("aipo_cornerstone", _aipo_cornerstone)
            if cs:
                result["cornerstone"] = {
                    "count": len(cs),
                    "total_pct": round(sum(c.shareholding_pct for c in cs), 2),
                    "top_investors": [{"name": c.name, "pct": c.shareholding_pct} for c in cs[:3]],
                }

        # ============ 保荐人历史战绩：集思录（存活源）============
        history = _try("jisilu_history", lambda: fetch_jisilu_history(limit=50))
        if sponsor and history:
            sh = [h for h in history if sponsor in (h.get("underwriter") or "")]
            result["sponsor_history"] = {"sponsor": sponsor}
            if sh:
                returns = [h.get("first_day_return") for h in sh if h.get("first_day_return")]
                avg_return = sum(returns) / len(returns) if returns else None
                result["sponsor_history"]["jisilu"] = {
                    "ipo_count": len(sh),
                    "avg_first_day_return": round(avg_return, 2) if avg_return else None,
                }
                if len(sh) < 3:
                    result["sponsor_history"]["jisilu"]["note"] = "样本不足，仅供参考"
        elif sponsor is None:
            fallback["sponsor_history"] = "未取到保荐人名称，请用 web search 查保荐人及其历史战绩"

        # ============ A+H 折价：腾讯/新浪行情（存活源）============
        name_for_ah = (result.get("brief") or {}).get("name") or symbol
        price_for_ah = (result.get("brief") or {}).get("offer_price")
        if price_for_ah and str(price_for_ah) not in ("--", "N/A", ""):
            # 招股价可能是区间 "5.48-6.21"，取上限
            try:
                price_val = float(str(price_for_ah).replace(" ", "").split("-")[-1])
            except (ValueError, AttributeError):
                price_val = None
            if price_val:
                ah = _try("ah_premium", lambda: fetch_ah_comparison(code, price_val, name_for_ah))
                if ah and ah.get("a_share", {}).get("price_cny"):
                    result["ah_premium"] = {
                        "a_code": ah["a_share"]["code"],
                        "a_price_cny": ah["a_share"]["price_cny"],
                        "h_price_hkd": ah["h_share"]["price_hkd"],
                        "h_price_cny": ah["h_share"]["price_cny"],
                        "discount_pct": ah["discount_pct"],
                    }

        # ============ 汇总 ============
        if source_used:
            result["_source"] = source_used
        if fallback:
            result["_fallback"] = fallback
        got = [k for k in ("brief", "margin", "cornerstone", "sponsor_history", "ah_premium") if k in result]
        if not got:
            result["_data_status"] = "❌ 所有 CLI 数据源均未取到数据。请完全依赖招股书精读 + web search 进行 6D 分析。"
        elif fallback:
            result["_data_status"] = f"⚠️ 已取到：{got}；失效/缺失：{list(fallback.keys())}。失效项请用 web search 补齐后再打分。"
        else:
            result["_data_status"] = f"✅ CLI 数据完整：{got}"

        print(json.dumps(result, ensure_ascii=False, indent=2))
    
    elif module == 'profile':
        # 用户画像 + 当前 IPO 数据
        import json
        import os
        import yaml
        
        config_path = os.path.join(os.path.dirname(__file__), "..", "config", "user-profile.yaml")
        
        # 读取用户画像
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                profile = yaml.safe_load(f)
        else:
            # 没有配置文件，AI 需要问用户并创建
            print(json.dumps({
                "status": "need_profile",
                "config_path": config_path,
                "action": "请询问用户以下信息，然后写入配置文件",
                "questions": [
                    "本金多少港币？（如：20000）",
                    "风险偏好？（conservative 保守 / balanced 稳健 / aggressive 激进）",
                    "愿意用孖展融资吗？（never 不用 / cautious 谨慎用 / active 积极用）",
                    "用哪个券商？（如：longbridge、futu、tiger）"
                ],
                "yaml_template": "capital: <数字>\nrisk: <conservative|balanced|aggressive>\nmargin: <never|cautious|active>\nbroker: <券商名>"
            }, ensure_ascii=False, indent=2))
            sys.exit(0)
        
        # 获取当前招股列表
        from aipo import fetch_margin_list, fetch_ipo_brief, fetch_cornerstone_investors
        
        ipos = fetch_margin_list()
        capital = profile.get("capital", 20000)
        risk = profile.get("risk", "conservative")
        
        ipo_list = []
        for ipo in ipos:
            code = ipo["code"]
            brief = fetch_ipo_brief(code)
            cornerstone = fetch_cornerstone_investors(code)
            
            entry_fee = brief.get("minimum_capital", 0) if brief else 0
            has_cornerstone = len(cornerstone) > 0 if cornerstone else False
            margin_heat = ipo.get("total_margin", 0)
            
            # 输出数据让 AI 判断
            rec = {
                "code": code,
                "name": ipo.get("name"),
                "entry_fee": entry_fee,
                "affordable": entry_fee <= capital if entry_fee else None,
                "has_cornerstone": has_cornerstone,
                "cornerstone_count": len(cornerstone) if cornerstone else 0,
                "margin_billion": margin_heat,
                "pe": brief.get("pe") if brief else None,
                "listing_date": brief.get("listing_date") if brief else None,
            }
            ipo_list.append(rec)
        
        # 只输出数据，AI 自行分析
        affordable_count = len([r for r in ipo_list if r.get("affordable")])
        total_entry_fee = sum(r.get("entry_fee", 0) for r in ipo_list if r.get("entry_fee"))
        
        output = {
            "user_profile": profile,
            "current_ipos": ipo_list,
            "summary": {
                "total_capital": capital,
                "total_ipos": len(ipo_list),
                "affordable_count": affordable_count,
                "total_entry_fee_if_all": round(total_entry_fee, 2),
            },
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    
    elif module in ['-h', '--help', 'help']:
        print(__doc__)
        sys.exit(0)
    
    else:
        print(f"未知模块: {module}")
        print(f"可用模块: aipo, jisilu, futu, tradesmart, allotment, ah, hkex, analyze, profile")
        sys.exit(1)


if __name__ == "__main__":
    main()
