#!/usr/bin/env python3
"""
招股书自动下载器 — HK IPO Helper

从港交所披露易（官方源，最稳）按公司名/股票代码查找并下载招股书 PDF 到本地，
供 pdf2md.py 精读。关键设计：**下载后校验是不是真 PDF**（校验 %PDF 魔数），
拿到 HTML 错误页 / 反爬拦截页会明确报错并回传原始链接，绝不把错误页当招股书。

用法：
    # 列出当前处理中的 IPO 及招股书链接
    python3 fetch_prospectus.py --list

    # 按公司名关键词下载
    python3 fetch_prospectus.py --name 永康

    # 按港交所披露易 ID 下载（--list 里会给）
    python3 fetch_prospectus.py --id 108390

    # 指定输出目录（默认 ./prospectus/）
    python3 fetch_prospectus.py --name 永康 --out ./prospectus

设计说明：
- 招股书 PDF 由用户提供或本工具自动下载，两种方式都支持；下载失败时降级为“请手动下载”。
- 本工具只依赖 hkex.py（港交所官方 JSON API），不依赖任何已失效的第三方源。
"""

from __future__ import annotations

import argparse
import os
import re
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "hkipo"))

try:
    from hkex import fetch_hkex_active_ipos_sync, get_prospectus_url, HKEXIPO  # type: ignore
except Exception as e:  # noqa: BLE001
    print(f"❌ 无法导入 hkex 模块：{e}", file=sys.stderr)
    sys.exit(1)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.hkexnews.hk/",
}

# 港交所标题搜索 servlet（正式披露 listconews 的可靠来源）
TITLE_SEARCH_URL = "https://www1.hkexnews.hk/search/titleSearchServlet.do"
HKEX_BASE = "https://www1.hkexnews.hk"
# 招股书类公告标题关键词（正式招股后发布在 listconews 路径）
PROSPECTUS_TITLE_KEYWORDS = ["全球發售", "股份發售", "招股章程", "發售通函", "招股章程及配發結果"]


# titlesearch 单次请求的 rowRange：实测接口最多能返回约 10000 条（超过则被截断），
# 且按最新时间倒序返回——窗口一拉长，旧公告会被更新的公告"挤出"截断边界之外。
# 单次安全查询步长（天）：实测港股单日公告量峰值可达 2000+ 条，为留足安全余量，
# 步长收紧为 1 天（配合 rowRange=10000，单日窗口远不会触及上限）。
_TITLESEARCH_ROW_RANGE = 10000
_TITLESEARCH_STEP_DAYS = 1


def search_prospectus_via_titlesearch(stock_code: str, days_back: int = 30) -> list[dict]:
    """通过港交所 titlesearch servlet 按股票代码查正式招股书（listconews 路径）。

    这是自动探测正式招股书的可靠方式：appactive 接口只给「申请版本」链接（正式招股后会 404），
    而公司正式招股后招股书发布在 /listedco/listconews/ 路径，只能通过本搜索接口拿到。

    修复说明（重要）：该接口按时间倒序返回记录，且单次请求存在 rowRange 硬上限（约 3000~10000）。
    若一次性查询整个 days_back 窗口，当区间内总公告量超过上限时，旧公告会被截断丢失——
    这正是此前 06880（Momenta）等公司招股书"探测不到"的根因：接口实际只返回了最近 1~2 天的数据，
    06/29 发布的招股书公告落在被截断的窗口外。
    修复方式：把 days_back 拆成多个 _TITLESEARCH_STEP_DAYS 天的窄窗口，逐段查询（每段远低于上限），
    一旦命中目标股票代码的招股书类文档就提前返回，避免遍历整个窗口造成不必要的请求。

    Args:
        stock_code: 港股代码，如 "6880" / "06880"
        days_back: 往前搜索的天数窗口（默认 30 天，覆盖整个招股周期）

    Returns:
        匹配的招股书文档列表，每项含 {title, date, url, stock_name}，按日期倒序。
    """
    import json
    from datetime import datetime, timedelta

    code_norm = stock_code.replace(".HK", "").replace(".hk", "").strip().zfill(5)
    today = datetime.now()

    hits: list[dict] = []
    seen_links: set[str] = set()

    # 按窄窗口从最近往过去分段查询，命中即可提前返回（招股书通常在最近几天内发布）
    window_end = today
    while window_end > today - timedelta(days=days_back):
        window_start = max(window_end - timedelta(days=_TITLESEARCH_STEP_DAYS), today - timedelta(days=days_back))
        from_date = window_start.strftime("%Y%m%d")
        to_date = window_end.strftime("%Y%m%d")

        params = {
            "sortDir": "0", "sortByOptions": "DateTime", "category": "0",
            "market": "SEHK", "stockId": "-1", "documentType": "-1",
            "fromDate": from_date, "toDate": to_date, "title": "",
            "searchType": "0", "t": "1", "lang": "ZH", "rowRange": str(_TITLESEARCH_ROW_RANGE),
        }
        try:
            with httpx.Client(follow_redirects=True, timeout=30, headers=HEADERS) as client:
                resp = client.get(TITLE_SEARCH_URL, params=params)
                resp.raise_for_status()
                outer = resp.json()
                records = json.loads(outer.get("result", "[]"))
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  titlesearch 查询失败（{type(e).__name__}，窗口 {from_date}~{to_date}），跳过该段。", file=sys.stderr)
            records = []

        if len(records) >= _TITLESEARCH_ROW_RANGE:
            print(f"⚠️  窗口 {from_date}~{to_date} 返回记录数达到上限 {_TITLESEARCH_ROW_RANGE}，"
                  f"该段可能仍有截断，请留意结果。", file=sys.stderr)

        for r in records:
            sc = str(r.get("STOCK_CODE", "")).zfill(5)
            title = r.get("TITLE", "")
            link = r.get("FILE_LINK", "")
            if sc != code_norm:
                continue
            if not link.lower().endswith(".pdf"):
                continue
            if any(k in title for k in PROSPECTUS_TITLE_KEYWORDS):
                if link in seen_links:
                    continue
                seen_links.add(link)
                hits.append({
                    "title": title,
                    "date": r.get("DATE_TIME", ""),
                    "url": HKEX_BASE + link if link.startswith("/") else link,
                    "stock_name": r.get("STOCK_NAME", ""),
                    "size": r.get("FILE_INFO", ""),
                })

        if hits:
            # 命中即可提前返回，避免继续往更早的窗口发起不必要的请求
            break

        window_end = window_start

    return hits


def _safe_filename(name: str) -> str:
    """把公司名清洗成安全文件名。"""
    name = re.sub(r"[（）()\s股份有限公司控股集团]", "", name)
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    return name[:40] or "prospectus"


def list_active() -> list[HKEXIPO]:
    """列出当前处理中的 IPO。"""
    ipos = fetch_hkex_active_ipos_sync()
    print(f"当前处理中的 IPO 共 {len(ipos)} 个：\n")
    for ipo in ipos:
        url = get_prospectus_url(ipo)
        flag = "📄有招股书" if url else "⏳无招股书PDF"
        code = f" [{ipo.stock_code}]" if ipo.stock_code else ""
        print(f"  id={ipo.id}{code} | {ipo.name} | {ipo.submit_date} | {ipo.board} | {flag}")
    print("\n用 --name <关键词> 或 --id <披露易ID> 下载指定招股书。")
    return ipos


def _download_pdf(url: str, out_path: str) -> tuple[bool, str]:
    """下载并校验是不是真 PDF。返回 (成功?, 说明)。"""
    try:
        with httpx.Client(follow_redirects=True, timeout=120, headers=HEADERS) as client:
            resp = client.get(url)
    except Exception as e:  # noqa: BLE001
        return False, f"网络错误：{type(e).__name__}: {e}"

    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}（很可能链接已失效或被拦截）"

    content = resp.content
    # 关键校验：真 PDF 以 %PDF 开头。港交所 404 会返回 HTML 错误页。
    if not content[:5].startswith(b"%PDF"):
        head = content[:200].decode("utf-8", errors="ignore").replace("\n", " ")
        if "<html" in head.lower() or "hong kong exchanges" in head.lower():
            return False, "返回的是 HTML 错误页而非 PDF（可能是 404、反爬拦截，或当前网络/代理限制二进制下载）"
        return False, f"返回内容不是有效 PDF（前 200 字节：{head[:120]}）"

    if len(content) < 50 * 1024:
        return False, f"PDF 体积异常偏小（{len(content)} 字节），可能不完整"

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(content)
    return True, f"已保存 {len(content) // 1024} KB"


def _download_via_titlesearch(stock_code: str, out_dir: str, out_name: str | None) -> int:
    """按股票代码自动探测正式招股书（listconews）并下载。"""
    print(f"🔎 正在通过港交所 titlesearch 自动探测 {stock_code} 的正式招股书…")
    hits = search_prospectus_via_titlesearch(stock_code)
    if not hits:
        print(f"❌ 未探测到 {stock_code} 的正式招股书（可能尚未招股或代码有误）。")
        print("💡 可用 --url <链接> 直接下载，或用 --list 查看在审 IPO。")
        return 1
    # 优先「全球發售/股份發售」正式招股书，取最新一条
    best = hits[0]
    print(f"✅ 探测到 {len(hits)} 份招股书类文档，选用最新：")
    print(f"   [{best['stock_name']}] {best['title']} ({best['date']}, {best.get('size','')})")
    fname = _safe_filename(out_name or best["stock_name"] or stock_code)
    out_path = os.path.join(out_dir, f"{fname}.pdf")
    print(f"⬇️  下载：{best['url']}")
    ok, msg = _download_pdf(best["url"], out_path)
    if ok:
        print(f"✅ {msg} -> {out_path}")
        print(f"下一步：python3 pdf2md.py {out_path}")
        return 0
    print(f"❌ 下载失败：{msg}\n👉 链接：{best['url']}")
    return 1


def download(keyword: str | None, ipo_id: int | None, out_dir: str,
             url: str | None = None, out_name: str | None = None,
             code: str | None = None) -> int:
    # --- 方式一：用户直接提供招股书 URL（最可靠）---
    if url:
        fname = _safe_filename(out_name or keyword or "prospectus")
        out_path = os.path.join(out_dir, f"{fname}.pdf")
        print(f"⬇️  正在下载（用户提供链接）：\n    {url}")
        ok, msg = _download_pdf(url, out_path)
        if ok:
            print(f"✅ {msg} -> {out_path}")
            print(f"下一步：python3 pdf2md.py {out_path}")
            return 0
        print(f"❌ 下载失败：{msg}\n👉 请确认链接可访问，或在浏览器手动下载。")
        return 1

    # --- 方式二：按股票代码自动探测正式招股书（listconews）---
    if code:
        return _download_via_titlesearch(code, out_dir, out_name)

    ipos = fetch_hkex_active_ipos_sync()

    target = None
    if ipo_id is not None:
        # ipo_id 可能是披露易 ID（108670）或股票代码（6880/06880），两者都尝试
        target = next((x for x in ipos if x.id == ipo_id), None)
        if target is None:
            id_str = str(ipo_id).zfill(5)
            target = next((x for x in ipos if x.stock_code and str(x.stock_code).zfill(5) == id_str), None)
    elif keyword:
        # 大小写不敏感匹配（港交所列表里公司名可能全大写，如 MOMENTA）
        kw = keyword.lower()
        matches = [x for x in ipos if kw in x.name.lower()]
        if len(matches) > 1:
            print(f"⚠️ 「{keyword}」匹配到多个，请用 --id 指定披露易 ID：")
            for x in matches:
                print(f"  id={x.id} | {x.name}")
            return 2
        target = matches[0] if matches else None

    if target is None:
        print(f"❌ 未找到匹配的 IPO（keyword={keyword}, id={ipo_id}）。先用 --list 查看。")
        print("💡 也可用 --code <股票代码> 自动探测正式招股书，或 --url <链接> 直接下载。")
        return 1

    # 先试 appactive 给的链接
    appactive_url = get_prospectus_url(target)
    out_path = os.path.join(out_dir, f"{_safe_filename(target.name)}.pdf")
    if appactive_url:
        print(f"⬇️  正在下载（appactive）：{target.name}\n    {appactive_url}")
        ok, msg = _download_pdf(appactive_url, out_path)
        if ok:
            print(f"✅ {msg} -> {out_path}")
            print(f"下一步：python3 pdf2md.py {out_path}")
            return 0
        print(f"⚠️  appactive 链接不可用（{msg}），自动切换到 titlesearch 探测正式招股书…")

    # appactive 失效 → 自动用股票代码探测 listconews
    if target.stock_code:
        return _download_via_titlesearch(str(target.stock_code), out_dir, out_name or target.name)

    print(f"❌ {target.name} 无股票代码，无法自动探测正式招股书。")
    print(f"👉 请用 --url <链接> 手动指定，或浏览器下载后直接 pdf2md.py <PDF>。")
    return 1


def main() -> int:
    p = argparse.ArgumentParser(description="港股招股书下载器（港交所官方源 + 自动探测 + 用户直传）")
    p.add_argument("--list", action="store_true", help="列出当前处理中的 IPO 及招股书链接")
    p.add_argument("--name", help="按公司名关键词下载（大小写不敏感）；配合 --url 时作为文件名")
    p.add_argument("--id", type=int, help="按港交所披露易 ID 或股票代码下载")
    p.add_argument("--code", help="按股票代码自动探测正式招股书（listconews 路径，最推荐）")
    p.add_argument("--url", help="直接指定招股书 PDF 链接下载")
    p.add_argument("--out", default="./prospectus", help="输出目录（默认 ./prospectus）")
    args = p.parse_args()

    if args.list or (not args.name and args.id is None and not args.url and not args.code):
        list_active()
        return 0
    return download(args.name, args.id, args.out, url=args.url, out_name=args.name, code=args.code)


if __name__ == "__main__":
    sys.exit(main())
