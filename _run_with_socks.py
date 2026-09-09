"""Sequoia-X 启动包装（最简版）：chdir + 可选 SOCKS5 代理 + 飞书 fallback。

代理默认关闭（节省流量、减少依赖点）。需要时只需在调用前设：

    USE_SOCKS5=1                                # 开关
    SOCKS5_HOST=127.0.0.1  SOCKS5_PORT=7891     # 可选覆盖（默认 127.0.0.1:7891）

示例（crontab）：
    # 关闭代理（默认，国内/海外直连 baostock 均可达）
    /opt/Sequoia-X/.venv/bin/python /opt/Sequoia-X/_run_with_socks.py

    # 走代理（仅当海外 IP 触发飞书/东财风控时再启用）
    USE_SOCKS5=1 /opt/Sequoia-X/.venv/bin/python /opt/Sequoia-X/_run_with_socks.py
"""
import os
import socket
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
os.chdir(PROJECT_DIR)

if os.getenv("USE_SOCKS5") == "1":
    import socks  # PySocks

    host = os.getenv("SOCKS5_HOST", "127.0.0.1")
    port = int(os.getenv("SOCKS5_PORT", "7891"))
    socks.set_default_proxy(socks.SOCKS5, host, port)
    socket.socket = socks.socksocket
    print(f"[sequoia-x] SOCKS5 proxy enabled → {host}:{port}")
else:
    print("[sequoia-x] SOCKS5 proxy disabled (direct connection)")

os.environ.setdefault(
    "FEISHU_WEBHOOK_URL",
    "https://open.feishu.cn/open-apis/bot/v2/hook/your-default-token",
)

import runpy

runpy.run_path(str(PROJECT_DIR / "main.py"), run_name="__main__")