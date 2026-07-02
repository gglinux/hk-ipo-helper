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


def download(keyword: str | None, ipo_id: int | None, out_dir: str,
             url: str | None = None, out_name: str | None = None) -> int:
    # --- 方式一：用户直接提供招股书 URL（最可靠，绕过港交所链接失效问题）---
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
        print("💡 若已知招股书链接，可用 --url <链接> --name <公司名> 直接下载。")
        return 1

    url = get_prospectus_url(target)
    if not url:
        print(f"❌ {target.name} 当前还没有招股书 PDF（可能尚未提交聆讯后资料集）。")
        return 1

    out_path = os.path.join(out_dir, f"{_safe_filename(target.name)}.pdf")
    print(f"⬇️  正在下载：{target.name}\n    {url}")
    ok, msg = _download_pdf(url, out_path)
    if ok:
        print(f"✅ {msg} -> {out_path}")
        print(f"下一步：python3 pdf2md.py {out_path}")
        return 0
    else:
        print(f"❌ 下载失败：{msg}")
        print("   注：港交所 appactive 接口给的常是「申请版本」链接，公司正式招股后招股书会迁到")
        print("   www1.hkexnews.hk/listedco/listconews/ 路径，旧链接会 404。")
        print(f"👉 解决办法（二选一）：")
        print(f"   1. 浏览器打开港交所披露易搜索该公司，复制正式招股书 PDF 链接，再用：")
        print(f"      python3 fetch_prospectus.py --url <链接> --name {_safe_filename(target.name)}")
        print(f"   2. 手动下载后直接：python3 pdf2md.py <本地PDF路径>")
        print(f"   （失效链接：{url}）")
        return 1


def main() -> int:
    p = argparse.ArgumentParser(description="港股招股书下载器（港交所官方源 + 用户直传链接）")
    p.add_argument("--list", action="store_true", help="列出当前处理中的 IPO 及招股书链接")
    p.add_argument("--name", help="按公司名关键词下载（大小写不敏感）；配合 --url 时作为文件名")
    p.add_argument("--id", type=int, help="按港交所披露易 ID 或股票代码下载")
    p.add_argument("--url", help="直接指定招股书 PDF 链接下载（最可靠，绕过港交所链接失效）")
    p.add_argument("--out", default="./prospectus", help="输出目录（默认 ./prospectus）")
    args = p.parse_args()

    if args.list or (not args.name and args.id is None and not args.url):
        list_active()
        return 0
    return download(args.name, args.id, args.out, url=args.url, out_name=args.name)


if __name__ == "__main__":
    sys.exit(main())
