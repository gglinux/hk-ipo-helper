# 🇭🇰 hk-ipo-helper · 港股打新助手

专业、靠谱的**港股 IPO 打新决策** AI Skill。核心是**招股书深度精读 + 6D 实战评分模型**，辅以港交所官方数据、存活的第三方数据源与 web search 交叉验证，输出「投 / 不投 + 怎么投 + 怎么走」的可执行结论。

> ⚠️ 仅供研究参考，不构成投资建议。数据可能延迟或不准，请以官方招股书与交易所公告为准。

## 设计原则（为什么不是缝合怪）

- **数据求真**：关键数字来自招股书原文 / 实时抓取 / web search，**绝不用模型记忆脑补**。
- **决策求狠**：不算盈亏平衡点不给结论；输出明确的四选一决策（全力出击/现金摸鱼/防守性申购/撤退）。
- **失效透明**：任一数据源挂了都不装死——自动降级到 web search，并在输出里明确标注「哪一项需要补」。

这套 skill 综合了社区四个项目的长处，但**以最稳的能力为主干**：

| 来源 | 吸收的能力 | 在本 skill 的定位 |
|------|-----------|-----------------|
| **HK-IPO-Sniper** | 6D 评分模型 + 招股书 PDF 精读 + 盈亏平衡方法论 | **主干**（依赖港交所官方源，最稳） |
| **Marvae/hk-ipo-research-assistant** | 数据引擎（在招列表/入场费/A+H/中签率/保荐人历史） | **加速器**（存活源直连，失效降级） |
| **limpidray/ipo-radar** | 加权评分维度参考 | 融入 6D |
| **discountifu/hk-ipo-skill** | 招股期扫描理念 | 融入 overview |

> 注：v2 已砍掉邮件推送 / HTML 报告 / 定时自动化——这些是"资讯播报"需求，对"这只能不能打"的决策质量零贡献，只增加维护面和失败点。

## 架构与数据源现状

```
招股书（自动下载 or 你提供）→ 智能解析(去噪+章节切分+表格自检) → 6D 决策
                                                    ↑
                        港交所/集思录/腾讯行情(存活) + web search(兜底)
```

- ✅ **港交所披露易**：在招列表、招股书 PDF 链接（主干靠它，最稳）
- ✅ **集思录**（历史/入场费）、**腾讯行情**（A股价算 A+H 折价）
- ⚠️ **aipo.myiqdii.com**（孖展/基石/评级/暗盘）：**该源目前可能已失效**，`analyze`/`overview` 会自动降级并提示走 web search
- 🌐 **web search**：孖展/超购/暗盘/负面/市场水位的兜底，始终可用

## 快速开始

```bash
pip install -r scripts/requirements.txt   # 需 Python 3.10+
cd scripts

# 1. 找标的 + 拿招股书（港交所官方源，带 %PDF 校验）
python3 fetch_prospectus.py --list
python3 fetch_prospectus.py --name 永康        # 或 --id 108390

# 2. 招股书精读（产出 全文.md + 关键章节.key.md + 表格自检）
python3 pdf2md.py ./prospectus/永康控股.pdf

# 3. 抓量化数据（含降级兜底，看 _data_status 判断哪些要 web search 补）
python3 hkipo.py analyze 02523
python3 hkipo.py ah compare 02523 --price 30.5 --name 永康控股

# 4. 中签率（本地计算，无网络依赖）
python3 hkipo.py odds --oversub 300 --price 30.5
```

在支持 Skills 的 AI Agent 中，直接说「帮我分析 02523.HK 能不能打」即可触发完整 6D 分析。

## 目录结构

```
hk-ipo-helper/
├── SKILL.md                     # 核心指令（角色/铁律/工作流/6D模型/降级策略）
├── scripts/
│   ├── fetch_prospectus.py      # 招股书自动下载（港交所源 + %PDF 校验）
│   ├── pdf2md.py                # 招股书解析：去噪 + 章节切分 + 表格自检
│   ├── hkipo.py                 # 数据引擎 CLI 入口
│   ├── hkipo/                   # 数据源适配器（hkex/jisilu/ah 存活；aipo 可能失效）
│   └── config/                  # 用户画像配置
└── references/
    ├── scoring-6d.md            # ⭐ 6D 实战评分模型（决策核心）
    ├── analysis-guide.md / ipo-mechanism.md ...
```

## 招股书解析（方案 A：轻量稳妥）

当前用 `pymupdf4llm`——**秒级、零模型、开箱即用**，并做了三层增强：
1. **智能去噪**：剔除释义/附录/申请方法等噪音章节（带安全阀，去噪过度会自动回退全文，不会删空）。
2. **章节切分**：只抽取打新决策要看的关键章节（财务/募资用途/基石/风险/发售机制）→ `.key.md`，AI 精读这份省 token。
3. **表格质量自检**：检测跨页/多栏表格崩坏并告警，提示对照原文核对。

### 可选增强：MinerU（复杂表格解析质量上限）

若实测发现复杂财务表解析仍不理想，可切换到 [MinerU](https://github.com/opendatalab/MinerU)（复杂版式/跨页表格/中文财报识别准确率业界最高）：

```bash
pip install -U "mineru[core]"           # 依赖较重，CPU 可跑但慢；arm64 Mac 注意架构
mineru -p 招股书.pdf -o ./output        # 产出 markdown + 结构化 json
```
代价：依赖重、单份大招股书解析可能要几分钟。建议**默认用 pymupdf4llm，遇到解析烂的招股书再对该份启用 MinerU**。

## 6D 评分模型

| 维度 | 权重 | 看点 |
|------|------|------|
| D0 市场水位 | 一票否决 | 恒指趋势 + 近 3 只新股表现 |
| D1 基本面 | 20% | 赛道稀缺性、增速、发行折让 |
| D2 保荐/稳价 | 15% | 保荐人 Tier、绿鞋、护盘史 |
| D3 基石 | 20% | 锁仓比例、基石成色 |
| D4 情绪博弈 | 25% | 孖展超购、回拨档位、中签率 |
| D5 套利空间 | 10% | AH/美股折价 |
| D6 成本平衡 | 10% | 盈亏平衡点、融资利息 |

输出四选一决策：**全力出击 / 现金摸鱼 / 防守性申购 / 撤退**。

## License

MIT
