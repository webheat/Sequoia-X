"""Sequoia-X 启动包装（最简版）：仅注入 SOCKS5 + chdir。"""
import os
import socket
from pathlib import Path

# 切到脚本所在目录
PROJECT_DIR = Path(__file__).resolve().parent
os.chdir(PROJECT_DIR)

import socks
socks.set_default_proxy(socks.SOCKS5, "127.0.0.1", 7891)
socket.socket = socks.socksocket

os.environ.setdefault(
    "FEISHU_WEBHOOK_URL",
    "https://open.feishu.cn/open-apis/bot/v2/hook/your-default-token",
)

import runpy
runpy.run_path(str(PROJECT_DIR / "main.py"), run_name="__main__")
