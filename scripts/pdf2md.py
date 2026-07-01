import pymupdf4llm
import re
import argparse
from pathlib import Path
import sys

# --- 更加严谨的黑名单 ---
# 建议：关键词尽量短小精悍，覆盖核心词
IGNORE_KEYWORDS = [
    "释义", "DEFINITIONS", "GLOSSARY",
    "技术词汇", "TECHNICAL TERMS",
    "如何申请", "HOW TO APPLY",
    "包销", "UNDERWRITING",
    "全球发售", "GLOBAL OFFERING", # 缩短关键词以增加命中率
    "附录", "APPENDIX",
    "备查文件", "DOCUMENTS DELIVERED",
    "前瞻性陈述", "FORWARD-LOOKING",
    "公司资料", "CORPORATE INFORMATION",
    "豁免", "WAIVERS",
    "董事及参与", "DIRECTORS AND PARTIES",
    "目录", "CONTENTS", "TABLE OF CONTENTS"
]

class IPOProspectusCleanerV2:
    def __init__(self):
        self.page_num_pattern = re.compile(r'^\s*(-?\s*\d+\s*-?|Page\s*\d+)\s*$')
        self.cj_pattern = re.compile(r'[\u4e00-\u9fff]')
        
        # 优化1: 标题匹配正则，\s* 允许 # 后无空格
        self.header_pattern = re.compile(r"^(#+)\s*(.*)")
        
        # 优化2: 预编译关键词，忽略大小写
        self.ignore_set = {k.upper() for k in IGNORE_KEYWORDS}

    def _normalize_header(self, text: str) -> str:
        """
        清洗标题文本，移除 Markdown 符号、序号、多余空格，以便比对
        输入: "**1. 释义**" -> "释义"
        输入: "APPENDIX I" -> "APPENDIX"
        """
        # 1. 移除 Markdown 加粗/斜体符号
        text = text.replace('*', '').replace('_', '')
        # 2. 移除开头的序号 (例如 "1. ", "I. ", "A. ")
        text = re.sub(r'^[\d\w]+\.\s*', '', text)
        # 3. 移除多余空白
        text = text.strip().upper() 
        return text

    def _is_blacklist_header(self, clean_header_text: str) -> bool:
        """
        判断清洗后的标题是否在黑名单中 (支持部分匹配)
        """
        # 策略A: 精确匹配 (适合短标题)
        if clean_header_text in self.ignore_set:
            return True
        
        # 策略B: 包含匹配 (适合 "Structure of the Global Offering" 这种长标题)
        for keyword in self.ignore_set:
            if keyword in clean_header_text:
                return True
        return False

    def clean(self, text: str) -> str:
        if not text: return ""

        lines = text.split('\n')
        merged_lines = []
        buffer = ""
        
        # --- 状态机变量 ---
        is_skipping = False
        skip_level = 0 
        
        total_skipped_lines = 0

        for i, line in enumerate(lines):
            line = line.rstrip()
            if not line: # 处理空行，如果是 Skipping 状态直接丢弃
                if not is_skipping:
                    if buffer: merged_lines.append(buffer); buffer = ""
                    merged_lines.append("")
                continue

            # --- 1. 核心过滤逻辑 (层级感知) ---
            header_match = self.header_pattern.match(line)
            
            if header_match:
                # 结算缓冲区 (遇到新标题，先把之前的正文存了)
                if buffer: merged_lines.append(buffer); buffer = ""

                raw_level_str = header_match.group(1)
                current_level = len(raw_level_str)
                raw_header_text = header_match.group(2)
                
                # 关键步骤：清洗标题文本
                clean_header = self._normalize_header(raw_header_text)
                
                # 判断是否命中黑名单
                is_hit = self._is_blacklist_header(clean_header)

                # DEBUG LOG: 取消注释可查看标题识别详情
                # print(f"DEBUG: Level {current_level} | Raw: {raw_header_text} | Clean: {clean_header} | Hit: {is_hit}")

                if is_skipping:
                    # 【场景 A】正在跳过中...
                    if current_level > skip_level:
                        # 是子章节，继续跳过
                        total_skipped_lines += 1
                        continue 
                    else:
                        # 是同级或更高级标题 -> 重新评估
                        if is_hit:
                            print(f"🗑️  继续剔除新章节: {clean_header}")
                            skip_level = current_level
                            # is_skipping 保持 True
                        else:
                            print(f"✅ 恢复记录章节: {clean_header}")
                            is_skipping = False
                            skip_level = 0
                            merged_lines.append(line) # 记录这个有效的标题
                
                else:
                    # 【场景 B】正常记录中...
                    if is_hit:
                        print(f"✂️  开始剔除章节: {clean_header} (Level {current_level})")
                        is_skipping = True
                        skip_level = current_level
                        continue
                    else:
                        merged_lines.append(line) # 正常标题
            
            else:
                # --- 非标题行 ---
                if is_skipping:
                    total_skipped_lines += 1
                    continue

                # --- 2. 常规文本清洗 (页码、断句修复) ---
                if self.page_num_pattern.match(line): continue

                # 结构保护 (遇到列表、引用等强制换行)
                if line.strip().startswith(('|', '```', '-', '*', '>')):
                    if buffer: merged_lines.append(buffer); buffer = ""
                    merged_lines.append(line)
                    continue

                # 智能缝合 (处理 PDF 断行)
                if not buffer:
                    buffer = line
                    continue

                prev_char = buffer[-1]
                curr_char = line.strip()[0] if line.strip() else ""

                # 中文+中文 -> 直接拼接
                if self.cj_pattern.match(prev_char) and self.cj_pattern.match(curr_char):
                    buffer += line.strip()
                # 英文单词断字符 (-) -> 去掉连字符拼接
                elif prev_char == '-' and not self.cj_pattern.match(curr_char):
                    buffer = buffer[:-1] + line.strip()
                # 其他 -> 加空格拼接
                else:
                    buffer += " " + line.strip()

        if buffer: merged_lines.append(buffer)
        
        print(f"ℹ️  共跳过 {total_skipped_lines} 行正文内容")
        return "\n".join(merged_lines)

def process_ipo_pdf(input_path):
    file_path = Path(input_path)
    if not file_path.exists():
        print(f"❌ 文件不存在: {input_path}")
        return

    output_path = file_path.with_suffix('.md')
    
    print(f"🚀 正在分析招股书: {file_path.name}")
    
    cleaner = IPOProspectusCleanerV2()
    
    try:
        # 使用 pymupdf4llm 提取
        raw_md = pymupdf4llm.to_markdown(file_path, write_images=False)
        final_text = cleaner.clean(raw_md)
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(final_text)
            
        print(f"✅ 处理完成 -> {output_path.name}")
        print(f"📉 瘦身效果: {len(raw_md)} 字符 -> {len(final_text)} 字符")
        
        # 简单抽查
        check_list = ["如何申请", "HOW TO APPLY", "释义", "DEFINITIONS"]
        found = [k for k in check_list if k in final_text]
        if found:
            print(f"⚠️  警告: 输出中仍包含以下关键词(可能是正文引用): {found}")
        else:
            print("✨ 完美: 核心噪音章节已清除。")

    except Exception as e:
        print(f"❌ 错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    # 为了方便测试，如果没有传参，可以手动指定一个路径
    if len(sys.argv) > 1:
        process_ipo_pdf(sys.argv[1])
    else:
        print("用法: python script.py <path_to_pdf>")