"""把 analyze_trades.py 的分析结果推送到飞书多维表格（long format）。

幂等：复用同名 bitable 与表 → 清空记录 → 重新写入。
bitable 名：Sequoia-X 交易分析
表名：交易分析
字段：类别(text) | 指标(text) | 维度(text) | 数值(number) | 文本值(text) | 备注(text)

用法：
    .venv/bin/python utils/push_to_feishu_bitable.py

环境变量（settings.json / .env 已配）：
    FEISHU_APP_ID       应用 ID
    FEISHU_APP_SECRET   应用 Secret
    FEISHU_BITABLE_NAME  （可选，默认 "Sequoia-X 交易分析"）
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "utils"))

from analyze_trades import as_dict, analyze  # noqa: E402

# ---------- 环境兜底：未设 FEISHU_APP_ID/SECRET 时从 .env / settings.json 读 ----------
def _load_creds_from_disk() -> None:
    import re
    candidates = [
        PROJECT_DIR / ".env",
        Path.home() / ".claude" / "settings.json",
        Path.home() / ".claude" / "settings.local.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            txt = path.read_text(encoding="utf-8")
        except OSError:
            continue
        m_id = re.search(r'FEISHU_APP_ID["\']?\s*[:=]\s*["\']?([^"\',;\s]+)["\']?', txt)
        m_sc = re.search(r'FEISHU_APP_SECRET["\']?\s*[:=]\s*["\']?([^"\',;\s]+)["\']?', txt)
        if m_id:
            os.environ.setdefault("FEISHU_APP_ID", m_id.group(1))
        if m_sc:
            os.environ.setdefault("FEISHU_APP_SECRET", m_sc.group(1))
        if os.getenv("FEISHU_APP_ID") and os.getenv("FEISHU_APP_SECRET"):
            return


_load_creds_from_disk()

BITABLE_NAME = os.getenv("FEISHU_BITABLE_NAME", "Sequoia-X 交易分析")
TABLE_NAME = "交易分析"

API = "https://open.feishu.cn/open-apis"
TOKEN_TTL = 7000  # token 2h 有效期，提前刷新


# ---------- HTTP ----------
def _req(method: str, url: str, token: str | None = None, body: dict | None = None) -> dict:
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} {url}: {body}") from e


def _ok(r: dict, allow_no_data: bool = False) -> dict:
    if r.get("code", 0) != 0:
        raise RuntimeError(f"API error: {r}")
    if "data" in r:
        return r["data"]
    if allow_no_data:
        return r
    raise RuntimeError(f"API 返回缺少 data 字段：{r}")


# ---------- 鉴权 ----------
_token_cache: dict = {"token": None, "exp": 0}


def get_token() -> str:
    if _token_cache["token"] and _token_cache["exp"] > time.time():
        return _token_cache["token"]
    app_id = os.environ["FEISHU_APP_ID"]
    app_secret = os.environ["FEISHU_APP_SECRET"]
    r = _req("POST", f"{API}/auth/v3/tenant_access_token/internal", body={"app_id": app_id, "app_secret": app_secret})
    d = _ok(r, allow_no_data=True)
    _token_cache["token"] = d["tenant_access_token"]
    _token_cache["exp"] = time.time() + TOKEN_TTL
    return _token_cache["token"]


# ---------- 定位/创建 bitable ----------
def find_bitable(token: str, name: str) -> str | None:
    """drive/v1/files 列表里找同名 bitable，返回 app_token。"""
    page_token = None
    while True:
        url = f"{API}/drive/v1/files?type=bitable&page_size=50"
        if page_token:
            url += f"&page_token={page_token}"
        d = _ok(_req("GET", url, token))
        for f in d.get("files", []):
            if f.get("name") == name and f.get("type") == "bitable":
                return f["token"]
        if not d.get("has_more"):
            return None
        page_token = d.get("next_page_token")


def create_bitable(token: str, name: str) -> str:
    r = _ok(_req("POST", f"{API}/bitable/v1/apps", token, {"name": name, "folder_token": ""}))
    print(f"  [create bitable] {name!r} → app_token={r['app']['app_token']}")
    return r["app"]["app_token"]


def find_or_create_bitable(token: str, name: str) -> str:
    app_token = find_bitable(token, name)
    if app_token:
        print(f"  [reuse bitable] {name!r} → app_token={app_token}")
        return app_token
    return create_bitable(token, name)


# ---------- 定位/创建表 + 字段 ----------
def find_table(token: str, app_token: str, table_name: str) -> str | None:
    d = _ok(_req("GET", f"{API}/bitable/v1/apps/{app_token}/tables?page_size=100", token))
    for t in d.get("items", []):
        if t.get("name") == table_name:
            return t["table_id"]
    return None


def create_table(token: str, app_token: str, table_name: str) -> str:
    r = _ok(_req(
        "POST", f"{API}/bitable/v1/apps/{app_token}/tables", token,
        {"table": {"name": table_name}},
    ))
    return r["table_id"]


def ensure_table(token: str, app_token: str, table_name: str) -> str:
    tid = find_table(token, app_token, table_name)
    if tid:
        print(f"  [reuse table] {table_name!r} → table_id={tid}")
        return tid
    tid = create_table(token, app_token, table_name)
    print(f"  [create table] {table_name!r} → table_id={tid}")
    return tid


def ensure_fields(token: str, app_token: str, table_id: str) -> None:
    """确保字段都存在。已存在的跳过。"""
    want = [
        ("类别",   1),   # Text
        ("指标",   1),
        ("维度",   1),
        ("数值",   2),   # Number
        ("文本值", 1),
        ("备注",   1),
    ]
    existing = _ok(_req("GET", f"{API}/bitable/v1/apps/{app_token}/tables/{table_id}/fields?page_size=100", token))
    have = {f["field_name"] for f in existing.get("items", [])}
    for name, typ in want:
        if name in have:
            continue
        body: dict = {"field_name": name, "type": typ}
        if typ == 2:
            body["property"] = {"formatter": "0", "min": -1e12, "max": 1e12}
        _ok(_req("POST", f"{API}/bitable/v1/apps/{app_token}/tables/{table_id}/fields", token, body))
        print(f"    + field: {name}")


# ---------- 记录操作 ----------
def clear_records(token: str, app_token: str, table_id: str) -> int:
    """删除表中所有记录。"""
    ids = []
    page_token = None
    while True:
        url = f"{API}/bitable/v1/apps/{app_token}/tables/{table_id}/records?page_size=500"
        if page_token:
            url += f"&page_token={page_token}"
        d = _ok(_req("GET", url, token))
        for it in d.get("items", []):
            ids.append(it["record_id"])
        if not d.get("has_more"):
            break
        page_token = d.get("page_token")
    if ids:
        # batch_delete 每次最多 100
        for i in range(0, len(ids), 100):
            batch = ids[i:i + 100]
            _ok(_req("POST", f"{API}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_delete", token, {"records": batch}))
        print(f"  [clear] deleted {len(ids)} records")
    return len(ids)


def insert_records(token: str, app_token: str, table_id: str, records: list[dict]) -> int:
    """批量插入。batch_create 每次最多 1000。"""
    n = 0
    for i in range(0, len(records), 500):
        batch = records[i:i + 500]
        _ok(_req(
            "POST", f"{API}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create", token,
            {"records": [{"fields": r} for r in batch]},
        ))
        n += len(batch)
    return n


# ---------- 把 analysis dict 摊平成 long-format records ----------
def to_records(d: dict) -> list[dict]:
    out: list[dict] = []
    o = d["overall"]
    cutoff = o["截止日期"]

    # 总体（一行一个指标）
    overall_rows = [
        ("起始日期", None, o["起始日期"], ""),
        ("截止日期", None, o["截止日期"], ""),
        ("交易日数", o["交易日数"], None, "天"),
        ("总买入笔数", o["总买入笔数"], None, "笔"),
        ("总卖出笔数", o["总卖出笔数"], None, "笔"),
        ("涉及股票数", o["涉及股票数"], None, "只"),
        ("总买入金额", o["总买入金额"], None, "¥"),
        ("总卖出金额", o["总卖出金额"], None, "¥"),
        ("净入金", o["净入金"], None, "¥"),
        ("总毛利", o["总毛利"], None, "¥"),
        ("总净利", o["总净利"], None, "¥"),
        ("ROI%", o["ROI%"], None, "%"),
        ("年化ROI%", o["年化ROI%"], None, "%"),
        ("盈利股票数", o["盈利股票数"], None, "只"),
        ("亏损股票数", o["亏损股票数"], None, "只"),
    ]
    for name, num, txt, note in overall_rows:
        out.append({
            "类别": "总体",
            "指标": name,
            "维度": "全部",
            "数值": num,
            "文本值": txt or "",
            "备注": note,
        })

    # 月度
    for m in d["monthly"]:
        out.append({"类别": "月度", "指标": "净利", "维度": m["月份"], "数值": m["净利"], "备注": f"{m['卖出笔数']} 笔卖出"})
        out.append({"类别": "月度", "指标": "占比%", "维度": m["月份"], "数值": m["占比%"], "文本值": f"{m['占比%']}%"})

    # 持仓 TOP
    for s in d["top_stocks"]:
        dim = f"{s['股票名称']} {s['股票代码']}".strip()
        out.append({"类别": "持仓", "指标": "累计净利", "维度": dim, "数值": s["累计净利"]})

    # 持仓 亏损
    for s in d["worst_stocks"]:
        dim = f"{s['股票名称']} {s['股票代码']}".strip()
        out.append({"类别": "持仓亏损", "指标": "累计净利", "维度": dim, "数值": s["累计净利"]})

    # 单笔 TOP
    for t in d["top_trades"]:
        dim = f"{t['日期']} {t['股票名称']} {t['股票代码']}".strip()
        out.append({
            "类别": "单笔",
            "指标": "净利",
            "维度": dim,
            "数值": t["净利"],
            "文本值": (f"{int(t['卖出数量'])} 股 @{t['卖出价']:.2f}" if t["卖出数量"] and t["卖出价"] else ""),
        })

    return out


# ---------- main ----------
def main() -> int:
    # 缺凭据就直接退出
    if not os.getenv("FEISHU_APP_ID") or not os.getenv("FEISHU_APP_SECRET"):
        sys.stderr.write("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET 环境变量\n")
        return 2

    print("=== 1) 读本地分析结果 ===")
    result = as_dict(analyze())
    records = to_records(result)
    print(f"  共 {len(records)} 条记录待推送")

    print("\n=== 2) 拿 tenant_access_token ===")
    token = get_token()
    print(f"  token len={len(token)}")

    print(f"\n=== 3) 定位 bitable {BITABLE_NAME!r} ===")
    app_token = find_or_create_bitable(token, BITABLE_NAME)

    print(f"\n=== 4) 定位表 {TABLE_NAME!r} ===")
    table_id = ensure_table(token, app_token, TABLE_NAME)

    print("\n=== 5) 确保字段 ===")
    ensure_fields(token, app_token, table_id)

    print("\n=== 6) 清空旧记录 ===")
    clear_records(token, app_token, table_id)

    print("\n=== 7) 插入新记录 ===")
    n = insert_records(token, app_token, table_id, records)
    print(f"  写入 {n} 条")

    # 输出 bitable 链接（tenant 域名待用户补全；这里是 base/{app_token}）
    print(f"\n=== 完成 ===")
    print(f"  bitable app_token = {app_token}")
    print(f"  table_id         = {table_id}")
    print(f"  URL: https://feishu.cn/base/{app_token}")
    return 0


if __name__ == "__main__":
    sys.exit(main())