#!/usr/bin/env python3
"""
招股书 PDF 智能解析器 v2 — HK IPO Helper（方案 A：轻量稳妥）

在原有「智能去噪」基础上，新增两大能力，解决大而复杂招股书解析不好的痛点：
  1. 章节切分：港股招股书动辄 500+ 页，全塞给 AI 又贵又超上下文。本工具按标题切分，
     只抽取打新决策真正需要的关键章节（财务摘要 / 募资用途 / 基石 / 风险因素 / 发售机制），
     单独产出 <name>.key.md，AI 精读这份即可。
  2. 表格质量自检：pymupdf4llm 对跨页、多栏财务表可能解析崩坏。本工具检测明显崩坏的表格
     （列数不齐、大量空单元格），在产出里打 ⚠️ 标记并汇总告警，提示人工核对或改用 MinerU。

用法：
    python3 pdf2md.py <招股书.pdf>              # 产出 全文.md + .key.md + 自检报告
    python3 pdf2md.py <招股书.pdf> --full-only  # 只产出全文，不做章节切分

依赖：pymupdf4llm（方案 A）。若需更强的复杂表格解析，可自行改用 MinerU（见 README「可选增强」）。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import pymupdf4llm
except ImportError:
    print("❌ 缺少依赖 pymupdf4llm，请先安装：pip install pymupdf4llm", file=sys.stderr)
    sys.exit(1)

# --- 噪音章节黑名单（转 md 后剔除，缩减体积）---
# 注意：只列「整章都是噪音」且不易与正文标题混淆的词。
# 刻意不含「全球发售/包销/结构」等——它们在封面常以 H1 出现，一旦命中会连带吞掉后续所有子章节。
IGNORE_KEYWORDS = [
    "释义", "釋義", "DEFINITIONS", "GLOSSARY",
    "技术词汇", "技術詞彙", "TECHNICAL TERMS",
    "如何申请", "如何申請", "HOW TO APPLY",
    "附录", "附錄", "APPENDIX",
    "备查文件", "備查文件", "DOCUMENTS DELIVERED",
    "前瞻性陈述", "前瞻性陳述", "FORWARD-LOOKING",
    "公司资料", "公司資料", "CORPORATE INFORMATION",
    "送呈公司註冊處", "送呈公司注册处",
    "目录", "目錄", "TABLE OF CONTENTS",
]

# --- 打新决策关键章节白名单（用于章节切分抽取）---
KEY_SECTION_KEYWORDS = {
    "财务/业绩": ["财务资料", "財務資料", "财务摘要", "財務摘要", "FINANCIAL INFORMATION",
                "会计师报告", "會計師報告", "ACCOUNTANT", "综合损益", "綜合損益", "经营业绩", "經營業績"],
    "募资用途": ["未来计划", "未來計劃", "所得款项用途", "所得款項用途", "募集资金", "USE OF PROCEEDS", "FUTURE PLANS"],
    "基石投资者": ["基石投资者", "基石投資者", "CORNERSTONE"],
    "风险因素": ["风险因素", "風險因素", "RISK FACTORS"],
    "发售机制": ["发售股份", "發售股份", "股份发售", "股份發售", "结构", "結構", "STRUCTURE OF",
              "回拨", "回撥", "CLAWBACK"],
    "业务概览": ["业务", "業務", "BUSINESS", "公司概览", "公司概覽", "概要", "SUMMARY"],
}


class ProspectusCleaner:
    """智能去噪 + 结构保护（继承自 HK-IPO-Sniper 的清洗逻辑）。"""

    def __init__(self):
        self.page_num_pattern = re.compile(r'^\s*(-?\s*\d+\s*-?|Page\s*\d+)\s*$')
        self.cj_pattern = re.compile(r'[\u4e00-\u9fff]')
        self.header_pattern = re.compile(r"^(#+)\s*(.*)")
        self.ignore_set = {k.upper() for k in IGNORE_KEYWORDS}

    def _normalize_header(self, text: str) -> str:
        text = text.replace('*', '').replace('_', '')
        text = re.sub(r'^[\d\w]+\.\s*', '', text)
        return text.strip().upper()

    def _is_blacklist_header(self, clean_header_text: str) -> bool:
        if clean_header_text in self.ignore_set:
            return True
        return any(k in clean_header_text for k in self.ignore_set)

    def clean(self, text: str) -> str:
        if not text:
            return ""
        lines = text.split('\n')
        merged_lines: list[str] = []
        buffer = ""
        is_skipping = False
        skip_level = 0

        for line in lines:
            line = line.rstrip()
            if not line:
                if not is_skipping:
                    if buffer:
                        merged_lines.append(buffer); buffer = ""
                    merged_lines.append("")
                continue

            header_match = self.header_pattern.match(line)
            if header_match:
                if buffer:
                    merged_lines.append(buffer); buffer = ""
                current_level = len(header_match.group(1))
                clean_header = self._normalize_header(header_match.group(2))
                is_hit = self._is_blacklist_header(clean_header)

                if is_skipping:
                    if current_level > skip_level:
                        continue
                    if is_hit:
                        skip_level = current_level
                    else:
                        is_skipping = False
                        skip_level = 0
                        merged_lines.append(line)
                else:
                    if is_hit:
                        is_skipping = True
                        skip_level = current_level
                        continue
                    merged_lines.append(line)
            else:
                if is_skipping:
                    continue
                if self.page_num_pattern.match(line):
                    continue
                if line.strip().startswith(('|', '```', '-', '*', '>')):
                    if buffer:
                        merged_lines.append(buffer); buffer = ""
                    merged_lines.append(line)
                    continue
                if not buffer:
                    buffer = line
                    continue
                prev_char = buffer[-1]
                curr_char = line.strip()[0] if line.strip() else ""
                if self.cj_pattern.match(prev_char) and self.cj_pattern.match(curr_char):
                    buffer += line.strip()
                elif prev_char == '-' and not self.cj_pattern.match(curr_char):
                    buffer = buffer[:-1] + line.strip()
                else:
                    buffer += " " + line.strip()

        if buffer:
            merged_lines.append(buffer)
        return "\n".join(merged_lines)


def check_table_quality(md: str) -> list[str]:
    """表格质量自检：找出明显崩坏的 md 表格，返回告警列表。"""
    warnings: list[str] = []
    lines = md.split("\n")
    table_blocks: list[list[str]] = []
    cur: list[str] = []
    for line in lines:
        if line.strip().startswith("|"):
            cur.append(line)
        else:
            if len(cur) >= 2:
                table_blocks.append(cur)
            cur = []
    if len(cur) >= 2:
        table_blocks.append(cur)

    for i, block in enumerate(table_blocks, 1):
        col_counts = [row.count("|") for row in block]
        if not col_counts:
            continue
        # 列数不一致 → 结构崩坏
        if max(col_counts) - min(col_counts) >= 2:
            warnings.append(f"表格#{i}: 各行列数不一致（{min(col_counts)}~{max(col_counts)}），疑似跨页/多栏解析错乱")
        # 空单元格占比过高 → 内容丢失
        cells = "".join(block).split("|")
        empty = sum(1 for c in cells if not c.strip())
        if cells and empty / len(cells) > 0.55:
            warnings.append(f"表格#{i}: 空单元格占比 {empty * 100 // len(cells)}%，疑似数字未对齐或丢失")
    return warnings


def _extract_by_text_window(md: str) -> tuple[str, set]:
    """回退切分：当 md 里关键章节标题没被识别成 `#` 时，
    直接在正文里定位关键词行，从该行起截取一段窗口作为章节内容。"""
    lines = md.split("\n")
    picked: list[str] = []
    hit_labels: set = set()
    used_ranges: list[tuple[int, int]] = []
    # 每个关键章节大致截取的行数窗口（招股书正文段落较长）
    WINDOW = 260

    for label, kws in KEY_SECTION_KEYWORDS.items():
        for i, line in enumerate(lines):
            ls = line.strip()
            # 跳过目录点线行（形如「風險因素 . . . . 24」）与过短行
            if not ls or ls.count(".") > 8 or len(ls) > 40:
                continue
            if any(kw.upper() in ls.upper() for kw in kws):
                # 需像标题（该行基本等于关键词，而非长句里偶然包含）
                if not any(ls.replace(" ", "").upper().startswith(kw.replace(" ", "").upper()[:4]) for kw in kws):
                    continue
                start = i
                end = min(len(lines), i + WINDOW)
                # 避免与已截取窗口大量重叠
                if any(s <= start <= e for s, e in used_ranges):
                    continue
                used_ranges.append((start, end))
                picked.append(f"\n\n<!-- ===== 关键章节(文本回退)：{label} ===== -->\n" + "\n".join(lines[start:end]))
                hit_labels.add(label)
                break
    return "".join(picked), hit_labels


def extract_key_sections(md: str) -> str:
    """按标题切分，抽取打新决策关键章节。若 `#` 标题切分命中 0 章，回退到正文关键词窗口切分。"""
    lines = md.split("\n")
    header_re = re.compile(r"^(#+)\s*(.*)")
    sections: list[tuple[str, int, list[str]]] = []  # (标题, 级别, 内容行)
    cur_title, cur_level, cur_body = None, 0, []

    for line in lines:
        m = header_re.match(line)
        if m:
            if cur_title is not None:
                sections.append((cur_title, cur_level, cur_body))
            cur_title = m.group(2).strip()
            cur_level = len(m.group(1))
            cur_body = [line]
        else:
            if cur_title is not None:
                cur_body.append(line)
    if cur_title is not None:
        sections.append((cur_title, cur_level, cur_body))

    picked: list[str] = []
    hit_labels: set[str] = set()
    for title, _level, body in sections:
        title_u = title.upper()
        for label, kws in KEY_SECTION_KEYWORDS.items():
            if any(kw.upper() in title_u for kw in kws):
                picked.append(f"\n\n<!-- ===== 关键章节：{label} ===== -->\n" + "\n".join(body))
                hit_labels.add(label)
                break

    fallback_note = ""
    # 回退：#标题切分命中过少（招股书至少应命中财务/风险等），改用正文关键词窗口
    if len(hit_labels) == 0:
        text_body, text_labels = _extract_by_text_window(md)
        if text_labels:
            picked.append(text_body)
            hit_labels = text_labels
            fallback_note = "（⚠️ 该PDF标题未被识别为markdown标题，已用正文关键词窗口回退切分，边界可能不精确）"

    header = "# 招股书关键章节摘录（打新决策用）\n\n"
    header += f"> 命中章节：{', '.join(sorted(hit_labels)) if hit_labels else '无（标题与正文切分均未命中，请读全文.md）'}{fallback_note}\n"
    header += "> ⚠️ 本文件仅为关键章节抽取，完整内容以 全文.md 与招股书原文为准。\n"
    return header + "".join(picked)


def process(input_path: str, full_only: bool = False) -> int:
    pdf = Path(input_path)
    if not pdf.exists():
        print(f"❌ 文件不存在: {input_path}", file=sys.stderr)
        return 1

    print(f"🚀 正在解析招股书: {pdf.name}")
    try:
        raw_md = pymupdf4llm.to_markdown(str(pdf), write_images=False)
    except Exception as e:  # noqa: BLE001
        print(f"❌ PDF 解析失败: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    cleaner = ProspectusCleaner()
    full_md = cleaner.clean(raw_md)

    # 去噪安全阀：正常去噪应保留大部分正文。若缩水 >92%，判定黑名单误伤整章，回退原始全文。
    if raw_md and len(full_md) < len(raw_md) * 0.08:
        print(f"⚠️  去噪后内容缩水过度（{len(raw_md)} → {len(full_md)} 字符，疑似标题误判整章），"
              f"已回退为原始全文以防丢失正文。")
        full_md = raw_md

    full_path = pdf.with_suffix(".md")
    full_path.write_text(full_md, encoding="utf-8")
    print(f"✅ 全文（去噪后）-> {full_path.name}  （{len(raw_md)} → {len(full_md)} 字符）")

    # 表格质量自检
    tbl_warnings = check_table_quality(full_md)
    if tbl_warnings:
        print(f"\n⚠️  表格质量自检发现 {len(tbl_warnings)} 处疑似崩坏：")
        for w in tbl_warnings:
            print(f"   - {w}")
        print("   → 关键财务数字建议对照招股书原文核对；若大量崩坏，考虑改用 MinerU（见 README）。")
    else:
        print("✨ 表格质量自检：未发现明显崩坏。")

    if full_only:
        return 0

    # 章节切分
    key_md = extract_key_sections(full_md)
    key_path = pdf.with_suffix(".key.md")
    key_path.write_text(key_md, encoding="utf-8")
    print(f"✅ 关键章节摘录 -> {key_path.name}  （AI 精读这份即可，省 token）")

    # 噪音残留抽查
    residual = [k for k in ["如何申请", "如何申請", "HOW TO APPLY", "释义", "釋義"] if k in full_md]
    if residual:
        print(f"⚠️  全文中仍含疑似噪音关键词（可能是正文引用）: {residual}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="招股书 PDF 智能解析器 v2（去噪 + 章节切分 + 表格自检）")
    p.add_argument("pdf", help="招股书 PDF 路径")
    p.add_argument("--full-only", action="store_true", help="只产出全文，不做章节切分")
    args = p.parse_args()
    return process(args.pdf, full_only=args.full_only)


if __name__ == "__main__":
    sys.exit(main())
