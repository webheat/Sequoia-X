"""Sequoia-X 每日调度器（最小版）。

设计目标
--------
- 每 ``RUN_INTERVAL_SECONDS``（默认 24h）跑一次 ``main.py``
- 单次运行超过 ``SINGLE_RUN_TIMEOUT_S``（默认 30 分钟）强制 kill，防 baostock 卡死拖垮整个调度
- 主进程收到 SIGTERM/SIGINT 干净退出，不留孤儿
- 自身心跳写 ``data/daily_runner.log``；``main.py`` 的输出走 stdout（可被重定向到文件）

用法
----
cron / systemd / nohup 任选其一::

    /opt/Sequoia-X/.venv/bin/python -u /opt/Sequoia-X/_run_daily.py \\
        > /var/log/sequoia-x.out 2>&1

环境变量（可选，全部 opt-in）
----------------------------
- ``RUN_ONCE=1``              单次模式：跑一次 main.py 就退出。crontab 用此模式避免常驻。
- ``RUN_INTERVAL_SECONDS``    默认 86400（24h）。交易日内不必这么密；想调试可设 600。
                              （RUN_ONCE=1 时此变量无效。）
- ``SINGLE_RUN_TIMEOUT_S``    默认 1800（30min）。main.py 自身在 sync_today_bulk 有 3min 硬超时，
                              这里 30min 给策略跑 + 飞书推送 + DB 写入留余量。
- ``USE_SOCKS5=1``            走 SOCKS5 代理（与 ``_run_with_socks.py`` 同语义）。
- ``SOCKS5_HOST/PORT``        代理地址，默认 127.0.0.1:7891。

历史背景
--------
此文件 2026-09-05 那版失踪——9/5 启动的进程卡在 baostock 单例 socket 上 6 天不退。
新版（2026-09-11 重写）依赖 ``engine._bs_fetch_batch`` 的 3 次 login 重试 + 显式 socket 超时，
单次 main.py 调用本身已经在 3 分钟内必定收敛；这里的 30 分钟是更外层的兜底。
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
RUN_LOG = PROJECT_DIR / "data" / "daily_runner.log"

RUN_ONCE = os.getenv("RUN_ONCE") == "1"
RUN_INTERVAL_SECONDS = int(os.getenv("RUN_INTERVAL_SECONDS", "86400"))
SINGLE_RUN_TIMEOUT_S = int(os.getenv("SINGLE_RUN_TIMEOUT_S", "1800"))


def _log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    # 1) 写到 stdout（cron / nohup 重定向到日志文件时也会落盘）
    print(line, flush=True)
    # 2) 镜像到 data/daily_runner.log，方便事后排查（即使 stdout 被丢也能恢复）
    try:
        RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
        with RUN_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _apply_socks5_if_enabled() -> None:
    """与 _run_with_socks.py 同语义的 opt-in 代理，避免重复造轮子。"""
    if os.getenv("USE_SOCKS5") != "1":
        _log("SOCKS5 proxy disabled (direct connection)")
        return
    import socks  # PySocks

    host = os.getenv("SOCKS5_HOST", "127.0.0.1")
    port = int(os.getenv("SOCKS5_PORT", "7891"))
    socks.set_default_proxy(socks.SOCKS5, host, port)
    socket.socket = socks.socksocket
    _log(f"SOCKS5 proxy enabled → {host}:{port}")


_apply_socks5_if_enabled()

_shutdown = False


def _on_signal(signum: int, _frame) -> None:
    global _shutdown
    _log(f"收到信号 {signum}，准备退出...")
    _shutdown = True


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def _run_once() -> int:
    """执行一次 main.py，带硬超时。返回 main 的退出码。"""
    cmd = [sys.executable, "-u", str(PROJECT_DIR / "main.py")]
    _log(f"启动 main.py: {' '.join(cmd)}")
    start = time.time()

    try:
        proc = subprocess.Popen(cmd, cwd=str(PROJECT_DIR))
    except OSError as exc:
        _log(f"Popen 失败: {exc}")
        return 1

    deadline = start + SINGLE_RUN_TIMEOUT_S
    while proc.poll() is None:
        if _shutdown:
            _log("主进程 shutdown，终止 main.py 子进程")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            return 130  # 128 + SIGINT(2)
        if time.time() > deadline:
            _log(f"main.py 超过 {SINGLE_RUN_TIMEOUT_S}s 未结束，强制 kill")
            proc.kill()
            proc.wait()
            return 124  # timeout convention (like `timeout` cmd)
        time.sleep(2)

    rc = proc.returncode
    elapsed = time.time() - start
    _log(f"main.py 退出码 {rc}，耗时 {elapsed:.1f}s")
    return rc


def main() -> None:
    mode = "RUN_ONCE" if RUN_ONCE else f"LOOP(interval={RUN_INTERVAL_SECONDS}s)"
    _log(f"_run_daily 启动 (mode={mode}, timeout={SINGLE_RUN_TIMEOUT_S}s)")

    if RUN_ONCE:
        # 单次模式：跑一次 main.py 就退出，给 cron 用
        _run_once()
        return

    while not _shutdown:
        rc = _run_once()
        if _shutdown:
            break
        if rc == 124:
            _log("⚠️  上一次运行超时，已强制终止")
        elif rc != 0:
            _log(f"⚠️  上一次 main.py 退出码 {rc}，继续按调度等待下一轮")

        # sleep 切小段，便于响应 shutdown 信号；同时在控制台显示心跳
        slept = 0
        while slept < RUN_INTERVAL_SECONDS and not _shutdown:
            step = min(5, RUN_INTERVAL_SECONDS - slept)
            time.sleep(step)
            slept += step
        if not _shutdown:
            _log("到点，准备下一轮 main.py")

    _log("_run_daily 退出")


if __name__ == "__main__":
    main()