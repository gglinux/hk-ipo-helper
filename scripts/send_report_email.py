#!/usr/bin/env python3
"""
IPO 打新分析报告 — 邮件发送脚本

从 .env 文件读取 SMTP 配置和收件人列表，发送 HTML 报告邮件。

用法：
  python3 send_report_email.py <html报告路径> [收件邮箱1] [收件邮箱2] ...

示例：
  # 发送给 .env 中配置的所有默认收件人
  python3 send_report_email.py /path/to/ipo_analysis_report_202602.html

  # 发送给指定收件人（覆盖默认列表）
  python3 send_report_email.py /path/to/report.html alice@qq.com bob@qq.com

首次使用：
  1. cp .env.example .env
  2. 编辑 .env 填入你的 SMTP 配置和收件人
  3. 运行本脚本
"""

import smtplib
import sys
import os
import re
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path


def load_env(env_path: str = None) -> dict:
    """
    从 .env 文件加载配置。
    查找顺序：指定路径 → 脚本所在目录的上级（skill 根目录）→ 当前工作目录
    """
    search_paths = []
    if env_path:
        search_paths.append(env_path)

    # 脚本所在目录的上级（skill 根目录）
    script_dir = Path(__file__).resolve().parent
    search_paths.append(str(script_dir.parent / ".env"))

    # 当前工作目录
    search_paths.append(os.path.join(os.getcwd(), ".env"))

    env_vars = {}
    for path in search_paths:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        key, _, value = line.partition("=")
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        if key and value:
                            env_vars[key] = value
            break  # 找到第一个就停

    return env_vars


def get_config() -> dict:
    """获取配置（优先环境变量，其次 .env 文件）"""
    env = load_env()

    config = {
        "smtp_server": os.environ.get("SMTP_SERVER", env.get("SMTP_SERVER", "")),
        "smtp_port": int(os.environ.get("SMTP_PORT", env.get("SMTP_PORT", "465"))),
        "sender": os.environ.get("SENDER_EMAIL", env.get("SENDER_EMAIL", "")),
        "auth_code": os.environ.get("SENDER_AUTH_CODE", env.get("SENDER_AUTH_CODE", "")),
        "default_recipients": [],
    }

    # 解析收件人列表
    recipients_str = os.environ.get("DEFAULT_RECIPIENTS", env.get("DEFAULT_RECIPIENTS", ""))
    if recipients_str:
        config["default_recipients"] = [
            r.strip() for r in recipients_str.split(",") if r.strip()
        ]

    return config


def validate_config(config: dict) -> list[str]:
    """验证配置完整性，返回缺失项列表"""
    errors = []
    if not config["smtp_server"]:
        errors.append("SMTP_SERVER")
    if not config["sender"]:
        errors.append("SENDER_EMAIL")
    if not config["auth_code"]:
        errors.append("SENDER_AUTH_CODE")
    if not config["default_recipients"]:
        errors.append("DEFAULT_RECIPIENTS")
    return errors


def extract_title_from_html(html: str) -> str:
    """从 HTML <title> 标签提取报告标题作为邮件主题"""
    match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return "IPO 打新分析报告"


def send_report(html_path: str, recipients: list[str], config: dict = None) -> None:
    """发送 HTML 报告邮件"""
    if config is None:
        config = get_config()

    if not os.path.exists(html_path):
        print(f"❌ 文件不存在：{html_path}")
        sys.exit(1)

    # 验证配置
    missing = validate_config(config)
    if missing:
        print(f"❌ 配置不完整，缺少以下字段：{', '.join(missing)}")
        print()
        print("请按以下步骤配置：")
        print("  1. cp .env.example .env")
        print("  2. 编辑 .env 填入你的邮箱配置")
        print()
        print("或者通过环境变量设置：")
        for field in missing:
            print(f"  export {field}=...")
        sys.exit(1)

    with open(html_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    subject = extract_title_from_html(html_content)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config["sender"]
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    print(f"📨 正在发送：{subject}")
    print(f"   报告文件：{html_path}")
    print(f"   发件人：{config['sender']}")
    print(f"   收件人数：{len(recipients)}")
    print()

    with smtplib.SMTP_SSL(config["smtp_server"], config["smtp_port"]) as server:
        server.login(config["sender"], config["auth_code"])
        server.sendmail(config["sender"], recipients, msg.as_string())

    print("✅ 发送成功！收件人：")
    for r in recipients:
        print(f"   - {r}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    html_path = sys.argv[1]
    config = get_config()

    # 如果命令行传了收件人，用命令行的；否则用配置文件的默认列表
    recipients = sys.argv[2:] if len(sys.argv) > 2 else config["default_recipients"]

    if not recipients:
        print("❌ 没有收件人。请在 .env 中配置 DEFAULT_RECIPIENTS 或在命令行指定。")
        sys.exit(1)

    send_report(html_path, recipients, config)
