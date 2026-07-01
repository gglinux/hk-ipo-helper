# 🇭🇰 hk-ipo-helper · 港股打新助手

专业、靠谱的**港股 IPO 打新决策** AI Skill。以真实数据引擎为主干，叠加招股书深度精读与 6D 实战评分模型，输出「投 / 不投 + 怎么投 + 怎么走」的可执行结论。

> ⚠️ 仅供研究参考，不构成投资建议。数据可能延迟或不准，请以官方招股书与交易所公告为准。

## 它凭什么靠谱

这个 skill 综合了社区里四个优秀港股/IPO 项目的长处：

| 来源 | 吸收的能力 |
|------|-----------|
| **Marvae/hk-ipo-research-assistant** | 真实数据引擎（孖展/基石/评级/暗盘/中签率/A+H/保荐人战绩，8 源直连 + 自动 fallback）——**数据主干** |
| **HK-IPO-Sniper**（gemini 实战版） | 6D 评分模型 + 招股书 PDF 智能去噪精读 + 成本敏感的盈亏平衡方法论 |
| **limpidray/ipo-radar** | HTML 研报模板 + SMTP 邮件推送 + 定时值守思路 |
| **discountifu/hk-ipo-skill** | 招股期实时提醒理念 |

核心差异：**数据求真（不靠模型记忆脑补）+ 决策求狠（不算盈亏平衡不给结论）**。

## 快速开始

```bash
pip install -r scripts/requirements.txt

# 看当前在招哪些港股新股
python3 scripts/hkipo.py overview

# ⭐ 一键聚合分析单只（基本面+孖展+基石+评级+保荐人战绩+A+H）
python3 scripts/hkipo.py analyze 02692

# 中签率表格
python3 scripts/hkipo.py odds --oversub 300 --price 73.68

# 招股书 PDF 精读（去噪转 markdown）
python3 scripts/pdf2md.py your_prospectus.pdf
```

在支持 Skills 的 AI Agent 中，直接说「帮我分析 02692.HK 能不能打」即可触发完整 6D 分析流程。

## 目录结构

```
hk-ipo-helper/
├── SKILL.md                     # 核心指令（角色/铁律/工作流/6D模型）
├── scripts/
│   ├── hkipo.py                 # 真实数据引擎 CLI 入口
│   ├── hkipo/                   # 8 个数据源适配器
│   ├── pdf2md.py                # 招股书 PDF 智能去噪
│   ├── send_report_email.py     # HTML 研报邮件推送
│   └── config/                  # 用户画像配置
├── references/
│   ├── scoring-6d.md            # ⭐ 6D 实战评分模型（决策核心）
│   ├── scoring-framework-weighted.md
│   ├── analysis-guide.md / ipo-mechanism.md / aipo-api.md ...
└── templates/report-template.html
```

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
