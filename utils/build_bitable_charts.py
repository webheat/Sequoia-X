"""在飞书 bitable "交易分析" 表里建 3 个 view + 5 张图表，1:1 还原原始 Excel "可视化看板" sheet。

幂等：重新跑会复用同名 view、清掉旧 chart 重建。

依赖：
    FEISHU_APP_ID / FEISHU_APP_SECRET  （.env / settings.json 已配）

用法：
    .venv/bin/python utils/build_bitable_charts.py

视图：
    月度净利视图  → 类别=月度 AND 指标=净利
    持仓视图      → 类别=持仓 OR 类别=持仓亏损
    单笔盈亏视图  → 类别=单笔

图表：
    1. 月度净盈利趋势   bar   月度净利视图     X=维度      Y=数值      sum
    2. 个股盈利 TOP10   bar   持仓视图         X=维度      Y=数值      sum    (filter 类别=持仓)
    3. 单笔盈亏分布     pie   单笔盈亏视图     X=盈亏段    Y=数值      count
    4. 月度交易笔数趋势 line  月度净利视图     X=维度      Y=卖出笔数  sum
    5. 个股亏损 TOP5    bar   持仓视图         X=维度      Y=数值      sum    (filter 类别=持仓亏损)

已知限制（2026-09 验证）：
    Feishu open platform 没有暴露 bitable chart 的创建/列表/删除 API。
    官方 lark_oapi SDK (v2_main) 也只有 dashboard.list / dashboard.copy 两个动作，
    没有 chart 资源；view.property 也只支持 filter_info / hidden_fields / hierarchy_config，
    不带 chart 配置。
    所以本脚本对 chart 步骤只是 best-effort：调官方文档里的 endpoint，
    如果 404 就在 stderr 给出清晰的提示，并继续 verify；不会让 view 那部分白做。

退出码：
    0  = 全部 view 重建成功（chart 部分尽力）
    2  = 缺凭据
    非 0 = 其它致命错误
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "utils"))

from push_to_feishu_bitable import (  # noqa: E402
    _req, _ok, get_token, API,
)

APP_TOKEN = "EYvgb5dOdaaeAgszYIKcwFfwn1e"
TABLE_ID = "tblJoMBMpSeTR9BQ"

CHART_ENDPOINT = (
    f"{API}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/views/{{view_id}}/charts"
)


# =========================== HTTP helpers (view) ===========================
def _create_view(token: str, name: str) -> str:
    r = _req("POST", f"{API}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/views",
             token, {"view_name": name, "view_type": "grid"})
    return _ok(r)["view"]["view_id"]


def _list_views(token: str) -> list[dict]:
    d = _ok(_req("GET", f"{API}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/views", token))
    return d.get("items", [])


def _set_view_conditions(token: str, view_id: str, conditions: list[dict]) -> None:
    """Feishu 实际接口是 PATCH /views/{view_id} body 带 filter —— 覆盖式。"""
    body: dict = {"filter": None} if not conditions else {
        "filter": {"conjunction": "and", "conditions": conditions}
    }
    r = _req("PATCH",
             f"{API}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/views/{view_id}",
             token, body)
    _ok(r)


# =========================== HTTP helpers (chart) ===========================
class ChartAPIUnavailable(Exception):
    """Raised when the Feishu open platform returns 404 for chart endpoints,
    indicating the chart API is not exposed to tenant_access_token scope."""


def _try_chart_request(method: str, url: str, token: str, body: dict | None = None) -> dict | None:
    """Wrapped chart API call that distinguishes 'endpoint not found' (404) from
    real errors. Returns parsed response on success, None on 404."""
    import urllib.error
    import urllib.request
    import json as _json
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = _json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return _json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} {url}: {body}") from e


def _list_charts(token: str, view_id: str) -> list[dict] | None:
    r = _try_chart_request("GET", CHART_ENDPOINT.format(view_id=view_id), token)
    if r is None:
        return None
    return _ok(r).get("items", [])


def _create_chart(token: str, view_id: str, name: str, type_: str,
                  chart_spec: dict) -> str | None:
    body = {"name": name, "type": type_, "chart_spec": chart_spec}
    r = _try_chart_request("POST", CHART_ENDPOINT.format(view_id=view_id), token, body)
    if r is None:
        return None
    d = _ok(r)
    return d.get("chart_id") or d.get("chart", {}).get("chart_id")


def _delete_chart(token: str, view_id: str, chart_id: str) -> bool:
    r = _try_chart_request("DELETE",
                           f"{CHART_ENDPOINT.format(view_id=view_id)}/{chart_id}",
                           token)
    return r is not None


# =========================== Field helpers ===========================
def _find_field_id(token: str, field_name: str) -> tuple[str | None, int | None]:
    d = _ok(_req("GET",
                 f"{API}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/fields?page_size=100",
                 token))
    for f in d.get("items", []):
        if f.get("field_name") == field_name:
            return f.get("field_id"), f.get("type")
    return None, None


def _ensure_fields(token: str) -> None:
    """确保图表依赖的字段存在（"盈亏段"/"卖出笔数" 由另一个 agent 添加，这里兜底创建）。"""
    want = [
        ("盈亏段", 1),     # Text
        ("卖出笔数", 2),   # Number
    ]
    for name, typ in want:
        fid, _ = _find_field_id(token, name)
        if fid:
            print(f"  [reuse field] {name} field_id={fid}")
            continue
        body: dict = {"field_name": name, "type": typ}
        if typ == 2:
            body["property"] = {"formatter": "0", "min": -1e12, "max": 1e12}
        _ok(_req("POST",
                 f"{API}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/fields",
                 token, body))
        print(f"  [create field] {name}")


# =========================== Specs ===========================
def _cond(field_name: str, value: str) -> dict:
    return {"field_name": field_name, "operator": "is", "value": value}


VIEW_SPECS = [
    {
        "name": "月度净利视图",
        "conditions": [
            _cond("类别", "月度"),
            _cond("指标", "净利"),
        ],
    },
    {
        "name": "持仓视图",
        # Feishu 多条件同字段 "is" 视为 OR（连写两条同字段条件）
        "conditions": [
            _cond("类别", "持仓"),
            _cond("类别", "持仓亏损"),
        ],
    },
    {
        "name": "单笔盈亏视图",
        "conditions": [
            _cond("类别", "单笔"),
        ],
    },
]

CHART_SPECS = [
    {"view": "月度净利视图", "name": "月度净盈利趋势",  "type": "bar",
     "x_field": "维度",   "y_field": "数值",     "agg": "sum",   "extra_filter": None},
    {"view": "持仓视图",   "name": "个股盈利 TOP10",   "type": "bar",
     "x_field": "维度",   "y_field": "数值",     "agg": "sum",   "extra_filter": [_cond("类别", "持仓")]},
    {"view": "单笔盈亏视图","name": "单笔盈亏分布",    "type": "pie",
     "x_field": "盈亏段", "y_field": "数值",     "agg": "count", "extra_filter": None},
    {"view": "月度净利视图","name": "月度交易笔数趋势","type": "line",
     "x_field": "维度",   "y_field": "卖出笔数", "agg": "sum",   "extra_filter": None},
    {"view": "持仓视图",   "name": "个股亏损 TOP5",    "type": "bar",
     "x_field": "维度",   "y_field": "数值",     "agg": "sum",   "extra_filter": [_cond("类别", "持仓亏损")]},
]


# =========================== Orchestration ===========================
def _find_or_create_view(token: str, name: str) -> str:
    for v in _list_views(token):
        if v.get("view_name") == name:
            vid = v["view_id"]
            print(f"  [reuse view] {name!r} view_id={vid}")
            return vid
    vid = _create_view(token, name)
    print(f"  [create view] {name!r} view_id={vid}")
    return vid


def main() -> int:
    if not os.getenv("FEISHU_APP_ID") or not os.getenv("FEISHU_APP_SECRET"):
        sys.stderr.write("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET\n")
        return 2

    print("=== 0) auth ===")
    token = get_token()
    print(f"  token len={len(token)}")

    print("\n=== 1) ensure fields (盈亏段 / 卖出笔数) ===")
    _ensure_fields(token)

    print("\n=== 2) build 3 views (same-name reuse, conditions rewrite) ===")
    view_ids: dict[str, str] = {}
    for spec in VIEW_SPECS:
        vid = _find_or_create_view(token, spec["name"])
        _set_view_conditions(token, vid, spec["conditions"])
        print(f"  [set conditions] view_id={vid} n={len(spec['conditions'])}")
        view_ids[spec["name"]] = vid

    print("\n=== 3) cleanup existing charts under each view (best-effort) ===")
    charts_unavailable = False
    for name, vid in view_ids.items():
        existing = _list_charts(token, vid)
        if existing is None:
            charts_unavailable = True
            print(f"  ! chart API not exposed (404) — view {name!r} skipped")
            continue
        for c in existing:
            print(f"  - delete chart_id={c.get('chart_id')} name={c.get('name')!r}")
            _delete_chart(token, vid, c["chart_id"])

    print("\n=== 4) create 5 charts (best-effort) ===")
    chart_results: list[dict] = []
    if charts_unavailable:
        print("  ! chart API 未在 open platform 暴露，跳过（详见 SKILL 文档末尾的限制说明）")
    for spec in CHART_SPECS:
        vid = view_ids[spec["view"]]
        chart_spec: dict = {
            "x_axis": {"field_name": spec["x_field"]},
            "y_axis": [{"field_name": spec["y_field"], "aggregation": spec["agg"]}],
        }
        if spec["extra_filter"]:
            chart_spec["filter"] = {
                "conjunction": "and",
                "conditions": spec["extra_filter"],
            }
        cid = _create_chart(token, vid, spec["name"], spec["type"], chart_spec)
        if cid is None:
            print(f"  ! {spec['name']!r}  chart API 不可用")
            continue
        print(f"  + {spec['name']!r:18s} view={spec['view']:8s} type={spec['type']:4s} "
              f"x={spec['x_field']:6s} y={spec['y_field']:6s} agg={spec['agg']:5s} → chart_id={cid}")
        chart_results.append({"name": spec["name"], "view": spec["view"],
                              "view_id": vid, "chart_id": cid, "type": spec["type"]})

    print("\n=== 5) verify ===")
    for name, vid in view_ids.items():
        charts = _list_charts(token, vid)
        if charts is None:
            print(f"  view {name!r}: chart API 不可用，跳过 verification")
            continue
        print(f"  view {name!r}: {len(charts)} chart(s)")
        for c in charts:
            cs = c.get("chart_spec", {})
            x = cs.get("x_axis", {}).get("field_name")
            ya = cs.get("y_axis", [])
            y_info = ", ".join(f"{y.get('field_name')}/{y.get('aggregation')}" for y in ya)
            print(f"    - {c.get('name')!r} type={c.get('type')} x={x} y=[{y_info}] "
                  f"filter={'yes' if cs.get('filter') else 'no'}")

    print("\n=== 完成 ===")
    print(f"  Views: {view_ids}")
    if chart_results:
        print(f"  Charts:")
        for c in chart_results:
            print(f"    {c['name']!r}  view={c['view']}  chart_id={c['chart_id']}")
    else:
        print("  Charts: (none — chart API unavailable; 手动到飞书 UI 加图即可)")
    print(f"  URL: https://feishu.cn/base/{APP_TOKEN}?table={TABLE_ID}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
