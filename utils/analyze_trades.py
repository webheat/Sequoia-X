"""交易数据入库 + 分析。

数据源：/opt/Sequoia-X/data/赵新兵总-股票交易.xlsx (Sheet: 清洗结果)
落盘：  /opt/Sequoia-X/data/trades.db          (独立 DB，不污染 sequoia_v2.db)

用法：
    # 一次性入库（幂等，已存在则覆盖）
    .venv/bin/python utils/analyze_trades.py --ingest

    # 默认：打印完整分析报告（同上次对话内容）
    .venv/bin/python utils/analyze_trades.py

    # 程序化：输出结构化 JSON（供 push_to_feishu_bitable.py 等消费）
    .venv/bin/python utils/analyze_trades.py --json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

# ---------- 路径 ----------
PROJECT_DIR = Path(__file__).resolve().parent.parent
XLSX_PATH = PROJECT_DIR / "data" / "赵新兵总-股票交易.xlsx"
DB_PATH = PROJECT_DIR / "data" / "trades.db"
SHEET = "清洗结果"
TABLE = "trades"


# ---------- 入库 ----------
def ingest(force: bool = False) -> int:
    """从 xlsx 读清洗结果 sheet 写入 SQLite。返回写入行数。"""
    if not XLSX_PATH.exists():
        raise FileNotFoundError(f"源文件不存在：{XLSX_PATH}")

    df = pd.read_excel(XLSX_PATH, sheet_name=SHEET)
    df.columns = [c.strip() for c in df.columns]
    df["操作日期"] = pd.to_datetime(df["操作日期"], errors="coerce").dt.strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_PATH)
    try:
        if force:
            conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                操作日期          TEXT,
                股票名称          TEXT,
                股票代码          TEXT,
                前期买入价格      REAL,
                当天买入价格      REAL,
                当天卖出价格      REAL,
                当天买入数量      REAL,
                当天卖出数量      REAL,
                当天盈利毛利      REAL,
                实际净盈利        REAL,
                当天毛利比例      REAL
            )
        """)
        conn.execute(f"DELETE FROM {TABLE}")
        df.to_sql(TABLE, conn, if_exists="append", index=False)
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_date  ON {TABLE}(操作日期)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_sym   ON {TABLE}(股票代码)")
        conn.commit()
        return len(df)
    finally:
        conn.close()


def load(conn: sqlite3.Connection | None = None) -> pd.DataFrame:
    """从 SQLite 读所有交易，附加当天盈亏估算列。"""
    own = conn is None
    conn = conn or sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql_query(f"SELECT * FROM {TABLE}", conn, parse_dates=["操作日期"])
        df["当天买入金额"] = (df["当天买入价格"] * df["当天买入数量"]).where(df["当天买入价格"].notna())
        df["当天卖出金额"] = (df["当天卖出价格"] * df["当天卖出数量"]).where(df["当天卖出价格"].notna())
        return df
    finally:
        if own:
            conn.close()


# ---------- 数据质量自检 ----------
def quality_check(df: pd.DataFrame) -> dict:
    n_total = len(df)
    n_only_buy  = ((df["当天买入价格"].notna()) & (df["当天卖出价格"].isna())).sum()
    n_only_sell = ((df["当天买入价格"].isna()) & (df["当天卖出价格"].notna())).sum()
    n_both      = ((df["当天买入价格"].notna()) & (df["当天卖出价格"].notna())).sum()
    n_empty     = ((df["当天买入价格"].isna()) & (df["当天卖出价格"].isna())).sum()
    return {
        "总记录": int(n_total),
        "当日买卖齐全": int(n_both),
        "只买未卖": int(n_only_buy),
        "只卖未买": int(n_only_sell),
        "都为空": int(n_empty),
    }


# ---------- 分析结果 dataclass ----------
@dataclass
class AnalysisResult:
    overall: dict            # 总体 KPI（key 维度 → 数值）
    monthly: list[dict]      # 月度净利
    top_stocks: list[dict]   # 按股票累计净利 TOP
    worst_stocks: list[dict] # 亏损股票
    top_trades: list[dict]   # 单笔净利 TOP
    quality: dict            # 数据质量


def analyze(df: pd.DataFrame | None = None) -> AnalysisResult:
    df = df if df is not None else load()

    sell = df.dropna(subset=["当天卖出价格"]).copy()

    # 总体
    total_buy = df["当天买入金额"].sum()
    total_sell_amt = df["当天卖出金额"].sum()
    net_in = total_buy - total_sell_amt
    net_pnl = sell["实际净盈利"].sum()
    gross_pnl = sell["当天盈利毛利"].sum()
    roi = net_pnl / net_in * 100 if net_in else 0.0
    days = (df["操作日期"].max() - df["操作日期"].min()).days or 1
    annualized = roi * 365 / days

    overall = {
        "起始日期": df["操作日期"].min().strftime("%Y-%m-%d"),
        "截止日期": df["操作日期"].max().strftime("%Y-%m-%d"),
        "交易日数": int(df["操作日期"].dt.date.nunique()),
        "总买入笔数": int(((df["当天买入价格"]).notna()).sum()),
        "总卖出笔数": int(((df["当天卖出价格"]).notna()).sum()),
        "涉及股票数": int(df["股票名称"].nunique()),
        "总买入金额": round(float(total_buy), 2),
        "总卖出金额": round(float(total_sell_amt), 2),
        "净入金": round(float(net_in), 2),
        "总毛利": round(float(gross_pnl), 2),
        "总净利": round(float(net_pnl), 2),
        "ROI%": round(float(roi), 2),
        "年化ROI%": round(float(annualized), 2),
        "盈利股票数": int((sell.groupby("股票名称")["实际净盈利"].sum() > 0).sum()),
        "亏损股票数": int((sell.groupby("股票名称")["实际净盈利"].sum() < 0).sum()),
    }

    # 月度
    sell["月份"] = sell["操作日期"].dt.to_period("M").astype(str)
    monthly_total = sell.groupby("月份")["实际净盈利"].sum()
    monthly_count = sell.groupby("月份").size()
    grand = monthly_total.sum()
    monthly = [
        {
            "月份": m,
            "净利": round(float(monthly_total[m]), 2),
            "卖出笔数": int(monthly_count[m]),
            "占比%": round(float(monthly_total[m] / grand * 100), 1) if grand else 0.0,
        }
        for m in monthly_total.index
    ]

    # 按股票累计净利
    by_sym = sell.groupby(["股票代码", "股票名称"])["实际净盈利"].sum().sort_values(ascending=False)

    def _fmt_code(v) -> str:
        if pd.isna(v):
            return ""
        try:
            return f"{int(float(v))}"
        except (ValueError, TypeError):
            return str(v)

    top_stocks = [
        {"股票代码": _fmt_code(k[0]),
         "股票名称": k[1],
         "累计净利": round(float(v), 2)}
        for k, v in by_sym.head(10).items() if v > 0
    ]
    worst_stocks = [
        {"股票代码": _fmt_code(k[0]),
         "股票名称": k[1],
         "累计净利": round(float(v), 2)}
        for k, v in by_sym.tail(10).items() if v < 0
    ]

    # 单笔 TOP
    top_trades_df = sell.dropna(subset=["实际净盈利"]).nlargest(10, "实际净盈利")
    top_trades = [
        {
            "日期": r["操作日期"].strftime("%Y-%m-%d"),
            "股票代码": _fmt_code(r["股票代码"]),
            "股票名称": r["股票名称"],
            "卖出价": float(r["当天卖出价格"]) if pd.notna(r["当天卖出价格"]) else None,
            "卖出数量": float(r["当天卖出数量"]) if pd.notna(r["当天卖出数量"]) else None,
            "净利": float(r["实际净盈利"]),
        }
        for _, r in top_trades_df.iterrows()
    ]

    return AnalysisResult(
        overall=overall,
        monthly=monthly,
        top_stocks=top_stocks,
        worst_stocks=worst_stocks,
        top_trades=top_trades,
        quality=quality_check(df),
    )


# ---------- CLI 报告 ----------
def print_report(r: AnalysisResult) -> None:
    o = r.overall
    print("=" * 60)
    print(f"📊 交易分析报告   {o['起始日期']} → {o['截止日期']}")
    print("=" * 60)
    print(f"区间：{o['交易日数']} 个交易日   买入 {o['总买入笔数']} 笔 / 卖出 {o['总卖出笔数']} 笔 / 股票 {o['涉及股票数']} 只")
    print(f"资金：买入 ¥{o['总买入金额']:,.0f}  卖出 ¥{o['总卖出金额']:,.0f}  净入金 ¥{o['净入金']:,.0f}")
    print(f"损益：毛利 ¥{o['总毛利']:,.0f}  净利 ¥{o['总净利']:,.2f}  ROI {o['ROI%']:.2f}%  年化 {o['年化ROI%']:.2f}%")
    print(f"胜率（按股票）：{o['盈利股票数']} 赚 / {o['亏损股票数']} 亏")

    print("\n── 月度净利 ──")
    for m in r.monthly:
        bar = "█" * int(abs(m["占比%"]))
        sign = "+" if m["净利"] >= 0 else "-"
        print(f"  {m['月份']}  {sign}¥{abs(m['净利']):>9,.0f}  ({m['占比%']:>+5.1f}%)  {bar}")

    print("\n── TOP 持仓（按累计净利）──")
    for s in r.top_stocks[:5]:
        print(f"  {s['股票名称']:8s} {s['股票代码']:>6s}  +¥{s['累计净利']:,.0f}")

    print("\n── TOP 单笔 ──")
    for t in r.top_trades[:5]:
        print(f"  {t['日期']}  {t['股票名称']:8s} {t['股票代码']:>6s}  +¥{t['净利']:,.0f}")

    if r.worst_stocks:
        print("\n── 亏损持仓 ──")
        for s in r.worst_stocks[:5]:
            print(f"  {s['股票名称']:8s} {s['股票代码']:>6s}  -¥{abs(s['累计净利']):,.0f}")

    print("\n── 数据质量 ──")
    q = r.quality
    print(f"  总 {q['总记录']} 条 | 当日买卖齐全 {q['当日买卖齐全']} | 只买未卖 {q['只买未卖']} | 只卖未买 {q['只卖未买']} | 都为空 {q['都为空']}")


# ---------- JSON 输出（供下游 push 脚本） ----------
def as_dict(r: AnalysisResult) -> dict:
    return {
        "overall": r.overall,
        "monthly": r.monthly,
        "top_stocks": r.top_stocks,
        "worst_stocks": r.worst_stocks,
        "top_trades": r.top_trades,
        "quality": r.quality,
    }


# ---------- main ----------
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--ingest", action="store_true", help="从 xlsx 重新入库")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    args = p.parse_args()

    if args.ingest:
        n = ingest(force=True)
        print(f"[ingest] 写入 {n} 行 → {DB_PATH}")
        return 0

    r = analyze()
    if args.json:
        print(json.dumps(as_dict(r), ensure_ascii=False, indent=2))
    else:
        print_report(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())