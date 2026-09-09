"""清理飞书 bitable 中全空的表。

判定规则（满足任一即视为空）：
  - 该表 0 条记录
  - 该表所有记录的 fields 都是空 dict（飞书自动生成的占位行）

保护机制：
  - PROTECTED_TABLES 白名单里的业务表永不被删
  - 默认 dry-run，只在 --confirm 时才真删

用法：
    # 1. 扫一遍看哪些表是空的（默认 dry-run）
    .venv/bin/python utils/cleanup_empty_tables.py

    # 2. 列出所有表 + 标记空表
    .venv/bin/python utils/cleanup_empty_tables.py --list

    # 3. 真删（必须显式 --confirm）
    .venv/bin/python utils/cleanup_empty_tables.py --confirm
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "utils"))

from push_to_feishu_bitable import (  # noqa: E402
    _load_creds_from_disk,
    _ok,
    _req,
    API,
    BITABLE_NAME as _BITABLE_DEFAULT,
    find_bitable,
    get_token,
)

_load_creds_from_disk()

BITABLE_NAME = __import__("os").getenv("FEISHU_BITABLE_NAME", _BITABLE_DEFAULT)

# 白名单：业务表永不删（避免误删已知重要表）
# 注：飞书自动生成的 "数据表" / "默认数据表" / "未命名表格" 不在白名单，会被识别为空表清理
PROTECTED_TABLES: set[str] = {
    "交易分析",
    "策略命中明细",
    "每日选股流水",
    "每日选股明细",
}


def is_empty_table(token: str, app_token: str, table_id: str) -> tuple[bool, int, int]:
    """返回 (是否为空, 总记录数, 有内容的记录数)。

    空的判定：total == 0  或  non_empty == 0
    """
    total = 0
    non_empty = 0
    page_token = None
    while True:
        url = f"{API}/bitable/v1/apps/{app_token}/tables/{table_id}/records?page_size=500"
        if page_token:
            url += f"&page_token={page_token}"
        d = _ok(_req("GET", url, token))
        for it in d.get("items", []):
            total += 1
            if any(v not in (None, "", [], {}) for v in it.get("fields", {}).values()):
                non_empty += 1
        if not d.get("has_more"):
            break
        page_token = d.get("page_token")
    return (non_empty == 0, total, non_empty)


def list_tables(token: str, app_token: str) -> list[dict]:
    d = _ok(_req("GET", f"{API}/bitable/v1/apps/{app_token}/tables?page_size=100", token))
    return d.get("items", [])


def delete_table(token: str, app_token: str, table_id: str) -> None:
    _ok(_req("DELETE", f"{API}/bitable/v1/apps/{app_token}/tables/{table_id}", token))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--list", action="store_true", help="列出所有表 + 标记空表（默认行为）")
    p.add_argument("--confirm", action="store_true", help="真删；不加此 flag 只 dry-run")
    p.add_argument("--bitable", default=BITABLE_NAME, help=f"bitable 名（默认 {BITABLE_NAME!r}）")
    args = p.parse_args()

    token = get_token()
    app_token = find_bitable(token, args.bitable)
    if not app_token:
        sys.stderr.write(f"[ERROR] 找不到 bitable: {args.bitable!r}\n")
        return 2
    print(f"📊 bitable: {args.bitable}  app_token={app_token}\n")

    tables = list_tables(token, app_token)
    if not tables:
        print("（无表）")
        return 0

    print(f"{'表名':<20s} {'table_id':<22s} {'总记录':>8s} {'有内容':>8s}  状态")
    print("─" * 80)
    to_delete: list[tuple[str, str]] = []
    for t in tables:
        name = t.get("name", "?")
        tid = t["table_id"]
        empty, total, non_empty = is_empty_table(token, app_token, tid)
        is_protected = name in PROTECTED_TABLES
        status = ("空表(受保护)" if empty and is_protected
                  else "空表-将删除" if empty
                  else "正常")
        print(f"{name:<20s} {tid:<22s} {total:>8d} {non_empty:>8d}  {status}")
        if empty and not is_protected:
            to_delete.append((name, tid))

    print()
    if not to_delete:
        print("✅ 无可清理的空表")
        return 0

    print(f"⚠  找到 {len(to_delete)} 个空表待清理:")
    for name, tid in to_delete:
        print(f"   - {name}  ({tid})")

    if not args.confirm:
        print("\n(dry-run，未删除。加 --confirm 真删)")
        return 0

    print("\n开始删除 ...")
    for name, tid in to_delete:
        try:
            delete_table(token, app_token, tid)
            print(f"  ✓ 删除 {name}")
        except Exception as exc:
            print(f"  ✗ 删除 {name} 失败：{exc}")
    print(f"\n✅ 完成，删了 {len(to_delete)} 个空表")
    return 0


if __name__ == "__main__":
    sys.exit(main())