"""HTTP 数据服务启动入口。

::

    python serve.py                       # 0.0.0.0:8000
    python serve.py --port 9000
    python serve.py --host 127.0.0.1

注意：
- ``--reload`` 在 Py 3.14 下与 baostock / sqlite3 一起用会触发 fd 复用警告，
  服务端场景没必要，强制不开。
- 多 worker 不开：SQLite 进程内并发已够用；多 worker 反而 fork 触发同样
  的 fd 继承问题。后续真要横向扩展，前面加 nginx / caddy 即可。
"""

from __future__ import annotations

import argparse
import os

import uvicorn

# dotenv 提前 load，让 SEQUOIA_API_TOKEN 之类能被读到
from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    parser = argparse.ArgumentParser(description="Sequoia-X HTTP 数据服务")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    parser.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    parser.add_argument(
        "--workers", type=int, default=1,
        help="worker 数，默认 1（SQLite 不需要多 worker）",
    )
    args = parser.parse_args()

    # DB_PATH 显式注入到 env（万一 .env 没配 settings.app 也能起）
    if "DB_PATH" not in os.environ:
        try:
            from sequoia_x.core.config import get_settings

            os.environ["DB_PATH"] = get_settings().db_path
        except Exception:
            # .env 缺 feishu_webhook_url 等必填项时，app.py 会 fallback 到默认路径
            pass

    uvicorn.run(
        "sequoia_x.api.app:app",
        host=args.host,
        port=args.port,
        workers=args.workers,
        log_level="info",
        # 关键：不带 reload，避免 watchdog 进程触发 fd 继承
        reload=False,
    )


if __name__ == "__main__":
    main()
