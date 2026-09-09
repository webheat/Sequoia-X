"""飞书多维表格写入器：把每日选股结果追加到 bitable。

两张表（都在 `Sequoia-X 交易分析` app 下）：
  - 每日选股流水：每 (日期, 策略) 一行，含候选数 + 代码汇总
  - 每日选股明细：每 (日期, 策略, 股票代码) 一行，含个股

幂等：表不存在则建，字段缺失则补；不清空既有数据（append-only）。

用法：
    from writer import get_writer
    writer = get_writer()
    writer.append_run("ZhaoStyleStrategy", ["600127","600354"], date.today().isoformat())

或 CLI（手工补跑某一日）：
    .venv/bin/python utils/feishu_bitable_writer.py --strategy ZhaoStyleStrategy \\
        --symbols 600127,600354 --date 2026-09-09
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Iterable

# 复用 push_to_feishu_bitable 的鉴权与基础辅助
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "utils"))

from push_to_feishu_bitable import (  # noqa: E402
    _load_creds_from_disk,
    _ok,
    _req,
    API,
    BITABLE_NAME as _BITABLE_DEFAULT,
    clear_records,
    create_bitable,
    ensure_fields as _ensure_fields,
    ensure_table as _ensure_table,
    find_bitable,
    find_table,
    get_token,
    insert_records,
)

_load_creds_from_disk()

BITABLE_NAME = os.getenv("FEISHU_BITABLE_NAME", _BITABLE_DEFAULT)
SUMMARY_TABLE = "每日选股流水"
DETAIL_TABLE = "每日选股明细"


# ---------- 字段定义 ----------
SUMMARY_FIELDS = [
    ("日期", 1),       # text
    ("策略", 1),
    ("候选数", 2),     # number
    ("候选代码", 1),
    ("候选名称", 1),   # 逗号分隔
]

DETAIL_FIELDS = [
    ("日期", 1),
    ("策略", 1),
    ("股票代码", 1),
    ("股票名称", 1),
]


def _to_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return str(v)


# ---------- Writer ----------
class FeishuBitableWriter:
    def __init__(self, bitable_name: str = BITABLE_NAME):
        self.token = get_token()
        self.app_token = find_or_create_app(self.token, bitable_name)
        self.summary_table_id = ensure_summary_table(self.token, self.app_token)
        self.detail_table_id = ensure_detail_table(self.token, self.app_token)
        self._name_cache: dict[str, str] = {}

    def append_run(
        self,
        strategy_name: str,
        symbols: list[str],
        run_date: str | None = None,
    ) -> tuple[int, int]:
        """追加一次策略运行：1 行到流水 + N 行到明细。返回 (summary_n, detail_n)。"""
        run_date = run_date or date.today().isoformat()
        symbols = [str(s) for s in symbols]

        names = self._resolve_names(symbols)

        # 1) 流水行
        summary_row = {
            "日期": run_date,
            "策略": strategy_name,
            "候选数": len(symbols),
            "候选代码": ", ".join(symbols),
            "候选名称": ", ".join(names[s] for s in symbols),
        }
        insert_records(self.token, self.app_token, self.summary_table_id, [summary_row])

        # 2) 明细行
        detail_rows = [
            {"日期": run_date, "策略": strategy_name, "股票代码": s, "股票名称": names[s]}
            for s in symbols
        ]
        insert_records(self.token, self.app_token, self.detail_table_id, detail_rows)

        return (1, len(detail_rows))

    def _resolve_names(self, symbols: list[str]) -> dict[str, str]:
        """优先 trades.db → 兜底 baostock → 都没有就用空串。"""
        out: dict[str, str] = {}
        miss: list[str] = []

        # 1) trades.db 本地缓存
        try:
            conn = sqlite3.connect(PROJECT_DIR / "data" / "trades.db")
            for code in symbols:
                rows = conn.execute(
                    "SELECT DISTINCT 股票名称 FROM trades WHERE 股票代码=? AND 股票名称 IS NOT NULL",
                    (code,),
                ).fetchall()
                if rows and rows[0][0]:
                    out[code] = rows[0][0]
                else:
                    miss.append(code)
            conn.close()
        except Exception:
            miss = list(symbols)

        # 2) 兜底用 baostock 全市场名（一次 query 全量）
        if miss:
            try:
                import baostock as bs
                lg = bs.login()
                if lg.error_code == "0":
                    rs = bs.query_stock_basic(code_name="", code="")
                    name_map: dict[str, str] = {}
                    for r in rs.get_data().itertuples(index=False):
                        # code 字段是 "sh.600000" / "sz.000001" 格式
                        full_code = getattr(r, "code", None) or r[0]
                        name = getattr(r, "code_name", None) or r[1]
                        if full_code and name:
                            # 同时支持 "600127" 和 "sh.600127" 两种 key
                            bare = str(full_code).split(".")[-1]
                            name_map[bare] = name
                            name_map[str(full_code)] = name
                    bs.logout()
                    for code in miss:
                        # code 可能是 "600127" → 补 6 位查
                        key = code.zfill(6) if code.isdigit() and len(code) <= 6 else code
                        out[code] = name_map.get(key, "")
            except Exception:
                for code in miss:
                    out[code] = ""

        # 任何仍未填充的用空串
        for code in symbols:
            out.setdefault(code, "")
        return out


# ---------- 表/字段管理 ----------
def find_or_create_app(token: str, name: str) -> str:
    app_token = find_bitable(token, name)
    if app_token:
        return app_token
    return create_bitable(token, name)


def _build_field_specs(fields) -> list:
    """转成 ensure_fields 需要的 [(name, type)]。"""
    return list(fields)


def ensure_summary_table(token: str, app_token: str) -> str:
    tid = find_table(token, app_token, SUMMARY_TABLE)
    if tid:
        return tid
    r = _ok(_req("POST", f"{API}/bitable/v1/apps/{app_token}/tables", token, {"table": {"name": SUMMARY_TABLE}}))
    tid = r["table_id"]
    # 加字段
    for name, typ in SUMMARY_FIELDS:
        body: dict = {"field_name": name, "type": typ}
        if typ == 2:
            body["property"] = {"formatter": "0", "min": -1e12, "max": 1e12}
        _ok(_req("POST", f"{API}/bitable/v1/apps/{app_token}/tables/{tid}/fields", token, body))
    return tid


def ensure_detail_table(token: str, app_token: str) -> str:
    tid = find_table(token, app_token, DETAIL_TABLE)
    if tid:
        return tid
    r = _ok(_req("POST", f"{API}/bitable/v1/apps/{app_token}/tables", token, {"table": {"name": DETAIL_TABLE}}))
    tid = r["table_id"]
    for name, typ in DETAIL_FIELDS:
        body: dict = {"field_name": name, "type": typ}
        _ok(_req("POST", f"{API}/bitable/v1/apps/{app_token}/tables/{tid}/fields", token, body))
    return tid


# ---------- 单例 ----------
_singleton: FeishuBitableWriter | None = None


def get_writer() -> FeishuBitableWriter:
    global _singleton
    if _singleton is None:
        _singleton = FeishuBitableWriter()
    return _singleton


# ---------- CLI ----------
def main() -> int:
    p = argparse.ArgumentParser(description="手工补跑一次策略结果到 bitable")
    p.add_argument("--strategy", required=True)
    p.add_argument("--symbols", required=True, help="逗号分隔代码列表")
    p.add_argument("--date", default=None, help="YYYY-MM-DD，默认今天")
    args = p.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    writer = get_writer()
    s_n, d_n = writer.append_run(args.strategy, symbols, args.date)
    print(f"OK  流水 +{s_n} 行，明细 +{d_n} 行")
    print(f"  bitable app_token  = {writer.app_token}")
    print(f"  summary_table_id   = {writer.summary_table_id}")
    print(f"  detail_table_id    = {writer.detail_table_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())