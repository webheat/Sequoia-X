"""给「交易分析」表新增 2 个图表专用字段 + 重跑推送。

字段：
  - 盈亏段   (single-select)  按单笔记录的「净利」分桶
  - 卖出笔数 (number)          从月度净利行的「备注」里正则提取

用法：
    .venv/bin/python utils/prepare_chart_data.py

环境变量同 push_to_feishu_bitable.py（FEISHU_APP_ID / FEISHU_APP_SECRET）。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "utils"))

# 复用 push_to_feishu_bitable 里的 HTTP / 鉴权 / 记录操作 / to_records
import push_to_feishu_bitable as pfb  # noqa: E402
from analyze_trades import analyze, as_dict  # noqa: E402

# ----- 业务常量 -----
BITABLE_NAME = pfb.BITABLE_NAME  # "Sequoia-X 交易分析"
TABLE_NAME = "交易分析"

# 单笔净利分桶（不含上界，含下界）
BUCKETS = [
    ("亏损",  -1e18, 0),
    ("微利",  0,      1000),
    ("小赚",  1000,   5000),
    ("中赚",  5000,   10000),
    ("大赚",  10000,  1e18),
]

NOTE_SELL_RE = re.compile(r"(\d+)\s*笔")


def bucket_of(net: float | None) -> str:
    if net is None:
        return ""
    for name, lo, hi in BUCKETS:
        if lo <= net < hi:
            return name
    return ""


def parse_sell_count(note: str | None) -> float | None:
    if not note:
        return None
    m = NOTE_SELL_RE.search(note)
    return float(m.group(1)) if m else None


# ----- A. 加字段（幂等） -----
def ensure_chart_fields(token: str, app_token: str, table_id: str) -> None:
    """在已有字段基础上，幂等补 2 个新字段。"""
    url = f"{pfb.API}/bitable/v1/apps/{app_token}/tables/{table_id}/fields?page_size=100"
    existing = pfb._ok(pfb._req("GET", url, token))
    have = {f["field_name"]: f for f in existing.get("items", [])}

    # 1) 盈亏段 - single-select (type=3)
    if "盈亏段" in have:
        print("    · 盈亏段 已存在 → 跳过")
    else:
        options = [{"name": name, "color": idx} for idx, (name, _, _) in enumerate(BUCKETS)]
        body = {
            "field_name": "盈亏段",
            "type": 3,
            "property": {"options": options},
        }
        pfb._ok(pfb._req(
            "POST",
            f"{pfb.API}/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            token, body,
        ))
        print("    + field: 盈亏段 (single-select)")

    # 2) 卖出笔数 - number (type=2)
    if "卖出笔数" in have:
        print("    · 卖出笔数 已存在 → 跳过")
    else:
        body = {
            "field_name": "卖出笔数",
            "type": 2,
            "property": {"formatter": "0", "min": 0, "max": 1e9},
        }
        pfb._ok(pfb._req(
            "POST",
            f"{pfb.API}/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            token, body,
        ))
        print("    + field: 卖出笔数 (number)")


# ----- B. 改写 records -----
def enrich(records: list[dict]) -> list[dict]:
    """按业务规则给 records 填两个新字段。"""
    for r in records:
        cat = r.get("类别", "")
        metric = r.get("指标", "")
        if cat == "单笔" and metric == "净利":
            r["盈亏段"] = bucket_of(r.get("数值"))
        elif cat == "月度" and metric == "净利":
            r["卖出笔数"] = parse_sell_count(r.get("备注"))
    return records


# ----- C. 重跑推送 -----
def repush_with_chart_data() -> dict:
    """跑完整分析 → 加新字段 → 清空 → 重写。返回摘要 dict。"""
    if not os.getenv("FEISHU_APP_ID") or not os.getenv("FEISHU_APP_SECRET"):
        sys.stderr.write("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET 环境变量\n")
        sys.exit(2)

    print("=== 1) 本地分析 ===")
    d = as_dict(analyze())
    base_records = pfb.to_records(d)
    records = enrich(base_records)
    print(f"  base={len(base_records)} enriched={len(records)}")

    # 抽样预览（不改原列表）
    preview_danbi = [r for r in records if r.get("类别") == "单笔"][:3]
    preview_yuedu = [r for r in records if r.get("类别") == "月度" and r.get("指标") == "净利"][:3]
    print("  抽样-单笔(盈亏段):")
    for r in preview_danbi:
        print(f"    {r['维度']:<28s}  净利={r['数值']:>10,.0f}  盈亏段={r.get('盈亏段')}")
    print("  抽样-月度净利(卖出笔数):")
    for r in preview_yuedu:
        print(f"    {r['维度']:<10s}  备注={r.get('备注')!r:<14s}  卖出笔数={r.get('卖出笔数')}")

    print("\n=== 2) 鉴权 ===")
    token = pfb.get_token()

    print(f"\n=== 3) 定位 bitable {BITABLE_NAME!r} ===")
    app_token = pfb.find_or_create_bitable(token, BITABLE_NAME)

    print(f"\n=== 4) 定位表 {TABLE_NAME!r} ===")
    table_id = pfb.ensure_table(token, app_token, TABLE_NAME)

    print("\n=== 5) 确保字段（原 6 + 新 2 = 8）===")
    pfb.ensure_fields(token, app_token, table_id)  # 原 6 字段
    ensure_chart_fields(token, app_token, table_id)  # 新 2 字段

    print("\n=== 6) 清空旧记录 ===")
    pfb.clear_records(token, app_token, table_id)

    print("\n=== 7) 插入新记录 ===")
    n = pfb.insert_records(token, app_token, table_id, records)
    print(f"  写入 {n} 条")

    return {
        "app_token": app_token,
        "table_id": table_id,
        "records": n,
        "danbi_count": sum(1 for r in records if r.get("盈亏段")),
        "yuedu_sell_filled": sum(1 for r in records if r.get("类别") == "月度"
                                  and r.get("指标") == "净利" and r.get("卖出笔数") is not None),
    }


def main() -> int:
    summary = repush_with_chart_data()
    print("\n=== 完成 ===")
    for k, v in summary.items():
        print(f"  {k} = {v}")
    print(f"  URL: https://feishu.cn/base/{summary['app_token']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
