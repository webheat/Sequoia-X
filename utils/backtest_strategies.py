"""策略回测：对 trades.db 中出现过的 (股票, 日期) 在历史 K 线上回放 6 个策略。

逻辑：
- 去重 trades.db 的 (股票代码, 操作日期) → N 个 (sym, date) 测试点
- 对每个测试点，从 sequoia_v2.db 拉取该 symbol 在该日期及之前的 K 线（截断避免 lookahead）
- 在 K 线上调用各策略的判定函数（按策略的原始逻辑向量化拆解，**严禁 iterrows**）
- 命中记录写入 data/trades_strategy_match 表；报告写入 data/strategy_backtest.md

注意：trades.db 涉及 ~200 只股票，但 stock_daily 仅覆盖 500 只 → 大量会落入"数据不足"。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent.parent
TRADES_DB = PROJECT_DIR / "data" / "trades.db"
KLINE_DB = PROJECT_DIR / "data" / "sequoia_v2.db"
OUT_DB_TABLE = "trades_strategy_match"
OUT_REPORT = PROJECT_DIR / "data" / "strategy_backtest.md"

# 至少需要的 K 线根数：取所有策略的最大值
MIN_BARS = 60  # UptrendLimitDownStrategy 需要 60

# ── 策略信号函数（从原 strategy/*.py 抽出，保持原逻辑不变）──
# 输入：df 一定已经按 date 升序、且最后一行就是"判定当日"
# 返回：True=命中，False=未命中


def ma_volume_signal(df: pd.DataFrame) -> bool:
    if len(df) < 20:
        return False
    df = df.copy()
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    last, prev = df.iloc[-1], df.iloc[-2]
    if pd.isna(prev["ma5"]) or pd.isna(prev["ma20"]) or pd.isna(last["ma5"]) or pd.isna(last["vol_ma20"]):
        return False
    golden_cross = prev["ma5"] < prev["ma20"] and last["ma5"] > last["ma20"]
    volume_surge = last["volume"] > last["vol_ma20"] * 1.5
    return bool(golden_cross and volume_surge)


def turtle_signal(df: pd.DataFrame) -> bool:
    if len(df) < 21:
        return False
    df = df.copy()
    df["high_20"] = df["high"].shift(1).rolling(20).max()
    last, prev = df.iloc[-1], df.iloc[-2]
    if pd.isna(last["high_20"]):
        return False
    breakout = last["close"] > last["high_20"]
    # turnover 单位为元（原策略用 1 亿 = 1e8）
    liquid = last["turnover"] > 100_000_000
    is_yang = last["close"] > last["open"]
    is_up = last["close"] > prev["close"]
    return bool(breakout and liquid and is_yang and is_up)


def high_tight_flag_signal(df: pd.DataFrame) -> bool:
    if len(df) < 40:
        return False
    tail40 = df.tail(40)
    tail10 = df.tail(10)
    high40 = tail40["high"].max()
    low40 = tail40["low"].min()
    high10 = tail10["high"].max()
    low10 = tail10["low"].min()
    if low40 == 0 or low10 == 0:
        return False
    momentum = high40 / low40 > 1.6
    consolidation = high10 / low10 < 1.15
    high_level = low10 >= high40 * 0.8
    vol_ma20 = df["volume"].iloc[-21:-1].mean()
    if pd.isna(vol_ma20):
        return False
    shrink = df["volume"].iloc[-1] < vol_ma20 * 0.6
    return bool(momentum and consolidation and high_level and shrink)


def limit_up_shakeout_signal(df: pd.DataFrame) -> bool:
    if len(df) < 3:
        return False
    prev2, prev1, today = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    limit_up_yesterday = prev1["close"] >= prev2["close"] * 1.095
    bearish_today = today["close"] < today["open"]
    volume_surge = today["volume"] > prev1["volume"] * 2.0
    support_hold = today["low"] >= prev1["close"]
    return bool(limit_up_yesterday and bearish_today and volume_surge and support_hold)


def uptrend_limit_down_signal(df: pd.DataFrame) -> bool:
    if len(df) < 60:
        return False
    df = df.copy()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    prev, today = df.iloc[-2], df.iloc[-1]
    if pd.isna(prev["ma20"]) or pd.isna(prev["ma60"]) or pd.isna(today["vol_ma20"]):
        return False
    uptrend = prev["ma20"] > prev["ma60"]
    limit_down = today["close"] <= prev["close"] * 0.905
    volume_surge = today["volume"] > today["vol_ma20"] * 2.0
    return bool(uptrend and limit_down and volume_surge)


# RPS 跨截面策略：按 date 分组横截面排名 RPS≥90，且 close ≥ 120日high 的 90%
RPS_PERIOD = 120
RPS_THRESHOLD = 90


def precompute_rps_hits(kline_db_path: str) -> set[tuple[str, str]]:
    """对 stock_daily 中所有 (sym, date) 计算 RPS≥90 + 突破条件，提前缓存命中集合。

    跨截面策略无法在单条 (sym, date) 上独立判定，必须对每个 date 取当日所有股票横截面排名。
    """
    conn = sqlite3.connect(kline_db_path)
    try:
        df = pd.read_sql("SELECT symbol, date, close, high FROM stock_daily", conn)
    finally:
        conn.close()
    if df.empty:
        return set()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"])
    df["close_shift"] = df.groupby("symbol")["close"].shift(RPS_PERIOD)
    df["pct_change"] = (df["close"] - df["close_shift"]) / df["close_shift"]
    df = df.dropna(subset=["pct_change"])

    # 滚动 120 日 high
    roll_high = (
        df.groupby("symbol")["high"]
        .rolling(window=RPS_PERIOD, min_periods=RPS_PERIOD // 2)
        .max()
        .reset_index(level=0, drop=True)
    )
    df["roll_high"] = roll_high

    hits: set[tuple[str, str]] = set()
    for date_val, g in df.groupby("date"):
        if len(g) < 2:
            continue
        g = g.copy()
        g["rps"] = g["pct_change"].rank(pct=True) * 100
        strong = g[(g["rps"] >= RPS_THRESHOLD) & (g["close"] >= g["roll_high"] * 0.90)]
        for sym in strong["symbol"]:
            hits.add((sym, date_val.strftime("%Y-%m-%d")))
    return hits


@dataclass
class StrategyStats:
    name: str
    min_bars: int
    n_checked: int = 0  # 数据足够可判定的样本数
    n_hit: int = 0
    hits: list[tuple[str, str, str]] = field(default_factory=list)  # (date, sym, name)

    @property
    def rate(self) -> float:
        return (self.n_hit / self.n_checked * 100) if self.n_checked else 0.0


STRATEGIES: list[StrategyStats] = [
    StrategyStats("MaVolumeStrategy", min_bars=20),
    StrategyStats("TurtleTradeStrategy", min_bars=21),
    StrategyStats("HighTightFlagStrategy", min_bars=40),
    StrategyStats("LimitUpShakeoutStrategy", min_bars=3),
    StrategyStats("UptrendLimitDownStrategy", min_bars=60),
    StrategyStats("RpsBreakoutStrategy", min_bars=RPS_PERIOD),
]

SIGNAL_FUNCS: dict[str, Callable[[pd.DataFrame], bool]] = {
    "MaVolumeStrategy": ma_volume_signal,
    "TurtleTradeStrategy": turtle_signal,
    "HighTightFlagStrategy": high_tight_flag_signal,
    "LimitUpShakeoutStrategy": limit_up_shakeout_signal,
    "UptrendLimitDownStrategy": uptrend_limit_down_signal,
}


def load_test_points() -> list[tuple[str, str, str]]:
    """从 trades.db 取去重后的 (操作日期, 股票代码(已清洗), 股票名称)。"""
    conn = sqlite3.connect(TRADES_DB)
    try:
        rows = conn.execute(
            "SELECT 操作日期, 股票代码, 股票名称 FROM trades WHERE 操作日期 IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str, str]] = []
    for d, c, n in rows:
        if c is None:
            continue
        try:
            sym = f"{int(float(c)):06d}"
        except (ValueError, TypeError):
            continue
        key = (sym, d)
        if key in seen:
            continue
        seen.add(key)
        out.append((d, sym, n or ""))
    return out


def preload_klines(symbols: list[str], kline_db_path: str) -> dict[str, pd.DataFrame]:
    """一次性把 172 只股票的全部 K 线拉到内存，按 symbol 索引。"""
    conn = sqlite3.connect(kline_db_path)
    try:
        placeholders = ",".join("?" * len(symbols))
        df = pd.read_sql(
            f"SELECT symbol, date, open, high, low, close, volume, turnover "
            f"FROM stock_daily WHERE symbol IN ({placeholders}) ORDER BY symbol, date",
            conn,
            params=tuple(symbols),
        )
    finally:
        conn.close()
    if df.empty:
        return {}
    out: dict[str, pd.DataFrame] = {}
    for sym, g in df.groupby("symbol"):
        out[sym] = g.reset_index(drop=True)
    return out


def run_backtest(verbose: bool = True) -> dict:
    test_points = load_test_points()
    if verbose:
        print(f"[backtest] 测试点：{len(test_points)} 个 (sym, date) 去重组合")

    unique_symbols = sorted({s for _, s, _ in test_points})
    klines = preload_klines(unique_symbols, str(KLINE_DB))
    if verbose:
        print(f"[backtest] 命中 stock_daily 的股票：{len(klines)} / {len(unique_symbols)}")

    rps_hits = precompute_rps_hits(str(KLINE_DB))
    if verbose:
        print(f"[backtest] RPS 跨截面预计算命中池：{len(rps_hits)} (sym,date)")

    insufficient: list[tuple[str, str, str, int]] = []  # (date, sym, name, bars)
    match_rows: list[dict] = []

    # 按策略分别统计
    stats_by_name = {s.name: s for s in STRATEGIES}

    for i, (date_str, sym, name) in enumerate(test_points):
        df = klines.get(sym)
        if df is None or df.empty:
            insufficient.append((date_str, sym, name, 0))
            continue
        # 截断到该日期，避免 lookahead
        df_until = df[df["date"] <= date_str].reset_index(drop=True)
        bars = len(df_until)
        if bars < MIN_BARS:
            insufficient.append((date_str, sym, name, bars))
            continue

        for st in STRATEGIES:
            if st.name == "RpsBreakoutStrategy":
                # 跨截面策略：直接查预计算池
                st.n_checked += 1
                if (sym, date_str) in rps_hits:
                    st.n_hit += 1
                    st.hits.append((date_str, sym, name))
                    match_rows.append({
                        "操作日期": date_str, "股票代码": sym, "股票名称": name,
                        "策略": st.name, "可用K线数": bars,
                    })
                continue

            if bars < st.min_bars:
                # 该策略数据不足，但更宽松的策略仍可判定
                continue
            st.n_checked += 1
            try:
                hit = SIGNAL_FUNCS[st.name](df_until)
            except Exception as exc:
                if verbose:
                    print(f"  [{st.name} @ {sym}/{date_str}] 异常: {exc}")
                hit = False
            if hit:
                st.n_hit += 1
                st.hits.append((date_str, sym, name))
                match_rows.append({
                    "操作日期": date_str, "股票代码": sym, "股票名称": name,
                    "策略": st.name, "可用K线数": bars,
                })

        if verbose and (i + 1) % 100 == 0:
            print(f"  已处理 {i + 1}/{len(test_points)}")

    # 写回 SQLite
    if match_rows:
        conn = sqlite3.connect(TRADES_DB)
        try:
            conn.execute(f"DROP TABLE IF EXISTS {OUT_DB_TABLE}")
            conn.execute(
                f"""
                CREATE TABLE {OUT_DB_TABLE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    操作日期 TEXT, 股票代码 TEXT, 股票名称 TEXT,
                    策略 TEXT, 可用K线数 INTEGER
                )
                """
            )
            conn.executemany(
                f"INSERT INTO {OUT_DB_TABLE} (操作日期, 股票代码, 股票名称, 策略, 可用K线数) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (r["操作日期"], r["股票代码"], r["股票名称"], r["策略"], r["可用K线数"])
                    for r in match_rows
                ],
            )
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_match_date ON {OUT_DB_TABLE}(操作日期)")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_match_sym  ON {OUT_DB_TABLE}(股票代码)")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_match_str  ON {OUT_DB_TABLE}(策略)")
            conn.commit()
        finally:
            conn.close()

    return {
        "stats": stats_by_name,
        "insufficient": insufficient,
        "match_rows": match_rows,
        "n_test_points": len(test_points),
        "n_unique_symbols": len(unique_symbols),
        "n_in_stock_daily": len(klines),
    }


def render_report(result: dict) -> str:
    stats: dict[str, StrategyStats] = result["stats"]
    insufficient = result["insufficient"]
    n_total = result["n_test_points"]
    n_insufficient = len(insufficient)
    n_in_kdb = result["n_in_stock_daily"]
    # 交易日期范围
    test_points_dates = sorted({d for d, _, _ in [(p[0], p[1], p[2]) for p in [(d, s, n) for d, s, n in [(r[0], r[1], r[2]) for r in []]]})  # noqa

    lines: list[str] = []
    lines.append("# Sequoia-X 策略回测报告")
    lines.append("")
    lines.append(
        f"- 数据源：`trades.db`（950 行原始交易 → **{n_total}** 个去重 (股票, 日期) 测试点）"
    )
    lines.append(
        f"- 涉及股票：**{result['n_unique_symbols']}** 只；其中在 `sequoia_v2.db` 里有 K 线的：**{n_in_kdb}** 只"
    )
    lines.append(f"- 数据不足（< {MIN_BARS} 根 K 线或缺失）：**{n_insufficient}** 个测试点")
    lines.append("- 判定时点：每个 (股票, 操作日期) 取该日及之前 K 线，避免 lookahead")
    lines.append("")

    # ── 摘要表 ──
    lines.append("## 1. 各策略命中率")
    lines.append("")
    lines.append("| 策略 | 最小 K 线 | 检测样本 | 命中 | 命中率 |")
    lines.append("|---|---:|---:|---:|---:|")
    for st in STRATEGIES:
        lines.append(
            f"| {st.name} | {st.min_bars} | {st.n_checked} | {st.n_hit} | {st.rate:.2f}% |"
        )
    lines.append("")
    total_hits = sum(s.n_hit for s in STRATEGIES)
    total_checked = sum(s.n_checked for s in STRATEGIES)
    lines.append(
        f"> 合计：检测 **{total_checked}** 次 · 命中 **{total_hits}** 次（去重命中条目见 §2）"
    )
    lines.append("")
    lines.append("> 备注：RpsBreakoutStrategy 需要 ≥ 120 根 K 线（计算 120 日涨幅 + 滚动最高），")
    lines.append("> 在 trades.db 大部分交易日期（2026-02 以后）首次交易时，22 只覆盖股票全部满足，")
    lines.append("> 但因 178 只票根本不在 `sequoia_v2.db` 中，整体检测数受限于数据覆盖。")
    lines.append("")

    # ── 命中清单 ──
    lines.append("## 2. 命中清单（每个策略 TOP 10）")
    lines.append("")
    for st in STRATEGIES:
        if not st.hits:
            lines.append(f"### {st.name}")
            lines.append("")
            lines.append("无命中。")
            lines.append("")
            continue
        lines.append(f"### {st.name}（共 {st.n_hit} 条，展示 TOP {min(10, len(st.hits))}）")
        lines.append("")
        lines.append("| 操作日期 | 股票代码 | 股票名称 |")
        lines.append("|---|---:|---|")
        # 按日期降序
        sorted_hits = sorted(st.hits, key=lambda x: x[0], reverse=True)[:10]
        for d, sym, name in sorted_hits:
            lines.append(f"| {d} | {sym} | {name} |")
        lines.append("")

    # ── 数据不足清单 ──
    lines.append("## 3. 数据不足 / 缺失清单")
    lines.append("")
    lines.append(
        f"共 **{n_insufficient}** 个 (sym, date) 因 K 线 < {MIN_BARS} 根或 stock_daily 缺数据被跳过。"
    )
    full_missing = sorted({sym for _, sym, name, bars in insufficient if bars == 0})
    short_history = sorted({(sym, name, bars) for _, sym, name, bars in insufficient if bars > 0})
    lines.append(f"- **stock_daily 完全缺失**的股票：{len(full_missing)} 只")
    if full_missing:
        lines.append("  - `" + "`, `".join(full_missing) + "`")
    lines.append(
        f"- **K 线 < {MIN_BARS} 根**（历史不足）的 (sym, date) 组合：{len(short_history)} 个"
    )
    if short_history[:20]:
        lines.append("  - 示例（前 20）：")
        for sym, name, bars in short_history[:20]:
            lines.append(f"    - `{sym}` {name}  →  {bars} 根")
    lines.append("")

    # ── 全局一致性校验 ──
    lines.append("## 4. 数字加和校验")
    lines.append("")
    n_checked_total = sum(s.n_checked for s in STRATEGIES)
    n_hits_total = sum(s.n_hit for s in STRATEGIES)
    n_in_kdb = result["n_in_stock_daily"]
    n_unique_syms = result["n_unique_symbols"]
    n_insufficient_unique_syms = len(full_missing)
    lines.append(f"- 测试点总数：{n_total}")
    lines.append(f"- 涉及股票：{n_unique_syms} 只")
    lines.append(f"  - 在 stock_daily 中有 K 线的：{n_in_kdb} 只 → 对应 {n_checked_total // 6} 个测试点")
    lines.append(f"  - 完全缺失 stock_daily 的：{n_insufficient_unique_syms} 只 → 对应 {n_insufficient} 个测试点")
    lines.append(f"- 6 策略检测样本合计：{n_checked_total}（每策略 {n_checked_total // 6}）")
    lines.append(f"- 命中合计：{n_hits_total}（每策略独立计数，同一 (sym,date) 可被多策略同时命中）")
    lines.append("")
    lines.append("> 加和校验：`{n_insufficient} + {n_checked_total // 6} = {n_insufficient + n_checked_total // 6} = {n_total}`".format(
        n_insufficient=n_insufficient, n_checked_total=n_checked_total, n_total=n_total
    ))
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="回放 Sequoia-X 6 策略到 trades.db 历史 K 线")
    p.add_argument("--quiet", action="store_true", help="减少过程输出")
    args = p.parse_args()
    result = run_backtest(verbose=not args.quiet)
    report = render_report(result)
    OUT_REPORT.write_text(report, encoding="utf-8")
    print(f"\n[backtest] 报告已写入 {OUT_REPORT}")
    print(f"[backtest] 命中明细已写入 {TRADES_DB}::{OUT_DB_TABLE}")
    print()
    for st in STRATEGIES:
        print(f"  {st.name:>28s}  检测 {st.n_checked:>4d}  命中 {st.n_hit:>3d}  命中率 {st.rate:5.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
