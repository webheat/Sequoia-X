#!/usr/bin/env python3
"""Sequoia-X 数据源连通性 + 数据正确性自检。

覆盖：
  1. 网络层：DNS 解析 / TCP 连通 / HTTP 根路径
  2. baostock：login + query_stock_basic(全市场) + query_history_k_data_plus(单股)
  3. 数据正确性：行数、列完整性、OHLCV 合理性、最新交易日时延

推荐运行方式（已自带虚拟环境）：
    .venv/bin/python utils/test_data_source.py
    USE_SOCKS5=1 .venv/bin/python utils/test_data_source.py   # 验证代理路径

可执行权限开启后也能直接跑（前提是已激活 venv）：
    ./utils/test_data_source.py

退出码：0 全部通过；1 有失败项；2 依赖缺失。
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time
import urllib.request
from datetime import date, timedelta

# ---------- 依赖自检：缺失时给出修复提示，避免裸 python3 跑挂 ----------
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV_PY = os.path.join(_PROJECT_ROOT, ".venv", "bin", "python")


def _missing_deps() -> list[str]:
    return [m for m in ("baostock", "pandas") if _cannot_import(m)]


def _cannot_import(mod: str) -> bool:
    try:
        __import__(mod)
    except ImportError:
        return True
    return False


def _ensure_deps() -> None:
    missing = _missing_deps()
    if not missing:
        return
    lines = [
        "",
        "[ERROR] 缺少依赖：" + ", ".join(missing),
        f"当前解释器：{sys.executable}",
        "",
    ]
    if os.path.exists(_VENV_PY):
        lines += [
            "项目自带虚拟环境，请改用：",
            f"  .venv/bin/python utils/test_data_source.py",
            "",
            "或激活 venv 后再跑：",
            f"  source .venv/bin/activate && python utils/test_data_source.py",
            "",
        ]
    else:
        lines += [
            "请先安装依赖：",
            "  uv sync",
            "  # 或者",
            "  pip install baostock pandas",
            "",
        ]
    sys.stderr.write("\n".join(lines))
    sys.exit(2)


_ensure_deps()

import baostock as bs  # noqa: E402  (deps guaranteed by _ensure_deps)
import pandas as pd  # noqa: E402

# ---------- 可选 SOCKS5 注入（与 _run_with_socks.py 行为一致） ----------
if os.getenv("USE_SOCKS5") == "1":
    import socks

    host = os.getenv("SOCKS5_HOST", "127.0.0.1")
    port = int(os.getenv("SOCKS5_PORT", "7891"))
    socks.set_default_proxy(socks.SOCKS5, host, port)
    socket.socket = socks.socksocket

# ---------- 颜色输出（无 rich 依赖） ----------
GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def ok(msg: str) -> None:
    print(f"{GREEN}  ✓ {msg}{RESET}")


def fail(msg: str) -> None:
    print(f"{RED}  ✗ {msg}{RESET}")


def info(msg: str) -> None:
    print(f"{DIM}  · {msg}{RESET}")


def section(title: str) -> None:
    print(f"\n{YELLOW}── {title} ──{RESET}")


# ---------- 校验器 ----------
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> bool:
    t0 = time.time()
    try:
        passed, detail = fn()
    except Exception as exc:  # noqa: BLE001
        passed, detail = False, f"{type(exc).__name__}: {exc}"
    ms = int((time.time() - t0) * 1000)
    RESULTS.append((name, passed, detail))
    line = f"{name} — {detail}  ({ms} ms)"
    (ok if passed else fail)(line)
    return passed


# ---------- 各检查项 ----------
def check_dns() -> tuple[bool, str]:
    t0 = time.time()
    ip = socket.gethostbyname("baostock.com")
    return True, f"baostock.com → {ip}"


def check_tcp() -> tuple[bool, str]:
    t0 = time.time()
    s = socket.create_connection(("baostock.com", 80), timeout=5)
    s.close()
    return True, f"TCP :80 ok"


def check_http() -> tuple[bool, str]:
    req = urllib.request.urlopen("http://baostock.com", timeout=5)
    return req.status in (200, 301, 302), f"HTTP {req.status}"


def check_login() -> tuple[bool, str]:
    lg = bs.login()
    if lg.error_code != "0":
        return False, lg.error_msg or "unknown"
    return True, "error_code=0"


def check_all_stocks() -> tuple[bool, str]:
    rs = bs.query_stock_basic(code_name="", code="")
    df = rs.get_data()
    if df is None or df.empty:
        return False, "empty result"
    n_total = len(df)
    n_active = int((df["status"] == "1").sum())
    if n_total < 4000:
        return False, f"{n_total} 只（疑似不全）"
    expected = {"code", "code_name", "ipoDate", "status"}
    missing = expected - set(df.columns)
    if missing:
        return False, f"列缺失：{missing}"
    return True, f"{n_total} 只（在市 {n_active}）"


def check_history(symbol: str, days: int) -> tuple[bool, str]:
    end = date.today().strftime("%Y-%m-%d")
    start = (date.today() - timedelta(days=days + 15)).strftime("%Y-%m-%d")
    rs = bs.query_history_k_data_plus(
        symbol,
        "date,open,high,low,close,volume",
        start_date=start,
        end_date=end,
        frequency="d",
        adjustflag="2",  # 前复权
    )
    df = rs.get_data()
    if df is None or df.empty:
        return False, "empty"
    # 数据完整性：至少要有 ~ days 行
    if len(df) < min(5, days):
        return False, f"仅 {len(df)} 行（期望 ≥ {days}）"
    # OHLCV 合理性
    bad_close = (df["close"].astype(float) <= 0).sum()
    bad_vol = (df["volume"].astype(float) <= 0).sum()
    if bad_close or bad_vol:
        return False, f"close≤0: {bad_close} 行, volume≤0: {bad_vol} 行"
    latest = df["date"].max()
    last_close = float(df["close"].iloc[-1])
    # 最新日期时延（自然日）
    lag = (date.today() - pd.to_datetime(latest).date()).days
    lag_note = "" if lag <= 3 else f" ⚠已滞后 {lag} 天（节假日/停牌？）"
    return True, f"{len(df)} 根 K, 最新={latest}, 收盘={last_close}{lag_note}"


# ---------- 主流程 ----------
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--symbol", default="sh.600000", help="单股 K 线抽样代码")
    p.add_argument("--days", type=int, default=30, help="抽样回看天数")
    p.add_argument("--skip-network", action="store_true", help="跳过 DNS/TCP/HTTP 检查")
    args = p.parse_args()

    proxy = os.getenv("USE_SOCKS5") == "1"
    print(f"Sequoia-X 数据源自检   {date.today()}   host={socket.gethostname()}")
    print(f"  socket 模式：{'SOCKS5' if proxy else 'DIRECT'}"
          + (f" → {os.getenv('SOCKS5_HOST', '127.0.0.1')}:{os.getenv('SOCKS5_PORT', '7891')}" if proxy else ""))

    if not args.skip_network:
        section("1) 网络层")
        check("DNS 解析 baostock.com", check_dns)
        check("TCP 连通 :80", check_tcp)
        check("HTTP 根路径", check_http)

    section("2) baostock 登录")
    if not check("bs.login()", check_login):
        print(f"\n{RED}登录失败，跳过数据层校验{RESET}")
        return 1

    try:
        section("3) 数据正确性")
        check("query_stock_basic 全市场", check_all_stocks)
        check(f"query_history_k_data_plus {args.symbol}", lambda: check_history(args.symbol, args.days))
    finally:
        bs.logout()

    section("汇总")
    n_pass = sum(1 for _, p, _ in RESULTS if p)
    n_total = len(RESULTS)
    if n_pass == n_total:
        print(f"{GREEN}全部 {n_pass}/{n_total} 项通过 ✓{RESET}")
        return 0
    print(f"{RED}{n_total - n_pass}/{n_total} 项失败 ✗{RESET}")
    return 1


if __name__ == "__main__":
    sys.exit(main())