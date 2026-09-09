"""把 trades.db::trades_strategy_match 的命中明细追加推送到飞书多维表格（long format）。

幂等：复用 bitable + 表 → 清空记录 → 重新写入。
bitable：复用已有的 "Sequoia-X 交易分析"（app_token 见常量 BITABLE_APP_TOKEN）。
新表名：策略命中明细
字段：
    命中日期   (text)
    股票代码   (text)
    股票名称   (text)
    策略       (text)
    可用K线数  (number)
    命中类型   (text)  -- 从策略名派生

策略名 → 命中类型 映射：
    TurtleTradeStrategy       → 动量型
    RpsBreakoutStrategy       → 突破型
    MaVolumeStrategy          → 均线型
    UptrendLimitDownStrategy  → 反转型

用法：
    .venv/bin/python utils/push_strategy_hits_to_feishu.py

环境变量：
    FEISHU_APP_ID / FEISHU_APP_SECRET （.env 已配）
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "utils"))

# 复用现有推送脚本里的鉴权 / HTTP / 建表 / 字段辅助
from push_to_feishu_bitable import (  # noqa: E402
    _req, _ok, get_token,
    find_bitable, create_bitable,
    find_table, create_table,
    ensure_fields as _ensure_fields_generic,
    clear_records, insert_records,
    BITABLE_NAME, API,
)

DB_PATH = PROJECT_DIR / "data" / "trades.db"

# 已存在的 bitable：Sequoia-X 交易分析（app_token 已知，避免每次重查）
BITABLE_APP_TOKEN = os.getenv("FEISHU_BITABLE_APP_TOKEN", "EYvgb5dOdaaeAgszYIKcwFfwn1e")
TABLE_NAME = "策略命中明细"

# 策略名 → 命中类型
STRATEGY_TYPE_MAP = {
    "TurtleTradeStrategy": "动量型",
    "RpsBreakoutStrategy": "突破型",
    "MaVolumeStrategy": "均线型",
    "UptrendLimitDownStrategy": "反转型",
}


def ensure_app_token(token: str) -> str:
    """优先用 env 里给的 BITABLE_APP_TOKEN；找不到对应 bitable 时回退到名称搜索/创建。"""
    if BITABLE_APP_TOKEN:
        return BITABLE_APP_TOKEN
    app = find_bitable(token, BITABLE_NAME)
    if app:
        return app
    return create_bitable(token, BITABLE_NAME)


def ensure_strategy_hit_fields(token: str, app_token: str, table_id: str) -> None:
    """确保 6 个字段都存在；已存在的跳过。"""
    want = [
        ("命中日期",  1),   # Text
        ("股票代码",  1),
        ("股票名称",  1),
        ("策略",      1),
        ("可用K线数", 2),   # Number
        ("命中类型",  1),
    ]
    existing = _ok(_req(
        "GET",
        f"{API}/bitable/v1/apps/{app_token}/tables/{table_id}/fields?page_size=100",
        token,
    ))
    have = {f["field_name"] for f in existing.get("items", [])}
    for name, typ in want:
        if name in have:
            continue
        body: dict = {"field_name": name, "type": typ}
        if typ == 2:
            body["property"] = {"formatter": "0", "min": -1e12, "max": 1e12}
        _ok(_req(
            "POST",
            f"{API}/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            token,
            body,
        ))
        print(f"    + field: {name} (type={typ})")


def load_hits() -> list[dict]:
    """从 trades.db 读 trades_strategy_match 表的全部命中。"""
    if not DB_PATH.exists():
        raise FileNotFoundError(f"DB not found: {DB_PATH}")
    con = sqlite3.connect(str(DB_PATH))
    try:
        cur = con.execute(
            "SELECT 操作日期, 股票代码, 股票名称, 策略, 可用K线数 "
            "FROM trades_strategy_match ORDER BY 操作日期, 股票代码"
        )
        rows = cur.fetchall()
    finally:
        con.close()

    records: list[dict] = []
    for date, code, name, strategy, k_count in rows:
        if not strategy:
            continue
        hit_type = STRATEGY_TYPE_MAP.get(strategy, "其他")
        records.append({
            "命中日期":  date or "",
            "股票代码":  code or "",
            "股票名称":  name or "",
            "策略":      strategy,
            "可用K线数": int(k_count) if k_count is not None else 0,
            "命中类型":  hit_type,
        })
    return records


def main() -> int:
    if not os.getenv("FEISHU_APP_ID") or not os.getenv("FEISHU_APP_SECRET"):
        sys.stderr.write("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET\n")
        return 2

    print("=== 1) 读本地命中明细 ===")
    records = load_hits()
    print(f"  共 {len(records)} 条命中待推送")
    if not records:
        sys.stderr.write("trades_strategy_match 表为空，退出。\n")
        return 3

    print("\n=== 2) 拿 tenant_access_token ===")
    token = get_token()
    print(f"  token len={len(token)}")

    print(f"\n=== 3) 定位 bitable {BITABLE_NAME!r} ===")
    app_token = ensure_app_token(token)
    print(f"  app_token = {app_token}")

    print(f"\n=== 4) 定位表 {TABLE_NAME!r} ===")
    tid = find_table(token, app_token, TABLE_NAME)
    if tid:
        print(f"  [reuse table] {TABLE_NAME!r} → table_id={tid}")
    else:
        tid = create_table(token, app_token, TABLE_NAME)
        print(f"  [create table] {TABLE_NAME!r} → table_id={tid}")

    print("\n=== 5) 确保字段 ===")
    ensure_strategy_hit_fields(token, app_token, tid)

    print("\n=== 6) 清空旧记录（幂等）===")
    clear_records(token, app_token, tid)

    print("\n=== 7) 批量写入 ===")
    n = insert_records(token, app_token, tid, records)
    print(f"  写入 {n} 条")

    print("\n=== 完成 ===")
    print(f"  bitable  : {BITABLE_NAME}")
    print(f"  app_token= {app_token}")
    print(f"  table_name= {TABLE_NAME}")
    print(f"  table_id = {tid}")
    print(f"  URL      = https://feishu.cn/base/{app_token}?table={tid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())