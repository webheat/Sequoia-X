"""把 trades.db 里出现过的代码回填到 sequoia_v2.db 的 K 线表。

容错：
- 前导零被 Excel 截掉的代码（`2040` → `002040`）自动补零
- 长度仍异常（>6 位、含非数字）的代码跳过并报告
- 已入库的代码自动 skip

用法：
    .venv/bin/python utils/backfill_trades_stocks.py
"""
from __future__ import annotations

import re
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from sequoia_x.core.config import get_settings  # noqa: E402
from sequoia_x.data.engine import DataEngine  # noqa: E402

TRADES_DB = PROJECT_DIR / "data" / "trades.db"
KLINE_DB = PROJECT_DIR / "data" / "sequoia_v2.db"


def normalize_code(s: str) -> str | None:
    """修正代码：
    - 纯数字 → 去掉 .0 → 补零到 6 位
    - 补零后非 6 位 → 返回 None（异常）
    """
    s = str(s).strip()
    if s.endswith(".0"):
        s = s[:-2]
    if not s.isdigit():
        return None
    if len(s) > 6:
        return None  # 异常长度，跳过
    return s.zfill(6)


def collect_symbols() -> tuple[list[str], list[tuple[str, int]]]:
    """从 trades.db 取唯一代码并规范化。

    Returns: (valid_symbols, skipped_invalid[(raw, count), ...])
    """
    conn = sqlite3.connect(TRADES_DB)
    raw_codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT 股票代码 FROM trades WHERE 股票代码 IS NOT NULL"
    ).fetchall()]
    conn.close()

    valid: set[str] = set()
    invalid: dict[str, int] = {}
    for raw in raw_codes:
        norm = normalize_code(raw)
        if norm:
            valid.add(norm)
        else:
            invalid[str(raw)] = invalid.get(str(raw), 0) + 1
    return sorted(valid), sorted(invalid.items())


def main() -> int:
    t0 = time.time()

    valid, invalid = collect_symbols()
    print(f"[1/3] trades.db 共 {len(valid) + len(invalid)} 个唯一代码")
    print(f"  - 合法 6 位: {len(valid)}")
    print(f"  - 异常跳过: {len(invalid)} 个")
    if invalid:
        print("    异常清单：")
        for code, n in invalid:
            print(f"      {code!r:>15s} × {n}")

    # 当前覆盖
    conn = sqlite3.connect(KLINE_DB)
    already = {r[0] for r in conn.execute("SELECT DISTINCT symbol FROM stock_daily").fetchall()}
    conn.close()
    covered = sum(1 for s in valid if s in already)
    todo = [s for s in valid if s not in already]
    print(f"\n[2/3] sequoia_v2.db 已覆盖 {covered}/{len(valid)}；待补 {len(todo)}")

    if not todo:
        print("\n✅ 全部已覆盖，无需回填")
        return 0

    print(f"\n[3/3] 开始回填 {len(todo)} 只票 ...")
    settings = get_settings()
    engine = DataEngine(settings)
    engine.backfill(todo)

    # 验证
    conn = sqlite3.connect(KLINE_DB)
    now = len({r[0] for r in conn.execute("SELECT DISTINCT symbol FROM stock_daily").fetchall()})
    conn.close()
    print(f"\n✅ 完成 耗时 {time.time()-t0:.1f}s")
    print(f"  sequoia_v2.db 现覆盖 {now} 只票")
    return 0


if __name__ == "__main__":
    sys.exit(main())