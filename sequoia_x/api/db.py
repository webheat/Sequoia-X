"""只读 SQLite helper：HTTP/MCP 共用。

为什么自己造一个，不复用 ``sequoia_x.data.engine._open_db``：
- 后者要写、要做 WAL 配置，服务端只读用不上
- 后者在 Py 3.14 下 with-block 不 close fd（已修但仍要小心），服务端多请求
  并发更易踩坑
- 服务端要 OS 级只读（防误写），用 ``mode=ro`` URI 最稳

并发：WAL 模式下多读不互斥；每个请求开新连接比共享单连接更稳 —
SQLite 进程内连接几乎零开销。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# 只接受 6 位纯数字（含可能带交易所前缀）
_PREFIX_RE = re.compile(r"^(?:(?:sh|sz|bj)\.)?(\d{6})$", re.IGNORECASE)


def normalize_symbol(raw: str) -> str:
    """归一化股票代码：``000001`` / ``sh.000001`` / ``SZ000001`` → ``000001``。

    Raises:
        ValueError: 输入不是合法的 A 股代码格式
    """
    if not isinstance(raw, str):
        raise ValueError(f"symbol 必须是字符串，得到 {type(raw).__name__}")
    s = raw.strip()
    # 处理 ``SZ000001`` 这种无点紧凑写法
    m = _PREFIX_RE.match(s)
    if not m:
        m2 = re.match(r"^([sS][hH]|[sS][zZ]|[bB][jJ])(\d{6})$", s)
        if m2:
            return m2.group(2)
        raise ValueError(
            f"非法 symbol: {raw!r}（期望 6 位数字，可带 sh./sz./bj. 前缀）"
        )
    return m.group(1)


@contextmanager
def open_readonly(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """以 OS 级只读模式打开 SQLite。

    使用 ``file:...?mode=ro`` URI：哪怕代码里有意外 UPDATE/INSERT 也会被
    SQLite 直接报错 ``attempt to write a readonly database``，不会落盘。
    """
    p = Path(db_path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"数据库不存在: {p}")
    # mode=ro 是 OS 级只读；即便 main.py 正在写也不冲突（WAL 允许并发读）
    uri = f"file:{p}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    try:
        # 只读连接上加 PRAGMA 仍然合法（WAL 是 DB 级持久的，busy_timeout 无副作用）
        conn.execute("PRAGMA busy_timeout=10000")
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


# ── 业务查询 ──

def list_symbols(db_path: str | Path) -> list[str]:
    """返回本地有数据的全部股票代码（按代码字典序）。"""
    with open_readonly(db_path) as conn:
        rows = conn.execute(
            "SELECT symbol FROM (SELECT DISTINCT symbol FROM stock_daily) "
            "ORDER BY symbol"
        ).fetchall()
    return [r[0] for r in rows]


def get_symbol_meta(db_path: str | Path, symbol: str) -> dict | None:
    """返回单只股票的元信息：first_date / last_date / row_count。

    Returns:
        dict 或 None（symbol 不存在时）
    """
    sym = normalize_symbol(symbol)
    with open_readonly(db_path) as conn:
        row = conn.execute(
            "SELECT MIN(date) AS first_date, MAX(date) AS last_date, "
            "COUNT(*) AS row_count "
            "FROM stock_daily WHERE symbol = ?",
            (sym,),
        ).fetchone()
    if not row or row["row_count"] == 0:
        return None
    return {
        "symbol": sym,
        "first_date": row["first_date"],
        "last_date": row["last_date"],
        "row_count": row["row_count"],
    }


def query_ohlcv(
    db_path: str | Path,
    symbol: str,
    *,
    start: str | None = None,
    end: str | None = None,
    limit: int = 500,
    offset: int = 0,
    order: str = "asc",
) -> list[dict]:
    """单只股票 OHLCV，按日期排序。

    Args:
        symbol: 已归一化或未归一化的代码
        start: 起始日期 (含) ``YYYY-MM-DD``
        end: 结束日期 (含) ``YYYY-MM-DD``
        limit: 最多返回行数（1..5000）
        offset: 跳过前 N 行
        order: ``asc`` / ``desc``，默认升序

    Returns:
        list of dict（列：date/open/high/low/close/volume/turnover）

    Raises:
        ValueError: symbol 不存在时
    """
    sym = normalize_symbol(symbol)
    if limit < 1 or limit > 5000:
        raise ValueError(f"limit 必须在 1..5000，得到 {limit}")
    if offset < 0:
        raise ValueError(f"offset 不能为负，得到 {offset}")
    if order not in ("asc", "desc"):
        raise ValueError(f"order 必须是 asc/desc，得到 {order!r}")

    clauses = ["symbol = ?"]
    params: list = [sym]
    if start:
        clauses.append("date >= ?")
        params.append(start)
    if end:
        clauses.append("date <= ?")
        params.append(end)
    where = " AND ".join(clauses)
    sql = (
        f"SELECT date, open, high, low, close, volume, turnover "
        f"FROM stock_daily WHERE {where} "
        f"ORDER BY date {order.upper()} LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])

    with open_readonly(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    if not rows:
        # 区分"真的没数据"和"symbol 不存在"
        meta = get_symbol_meta(db_path, sym)
        if meta is None:
            raise ValueError(f"symbol 不存在或无数据: {sym}")
    return [dict(r) for r in rows]


def cross_section(
    db_path: str | Path,
    symbols: list[str],
    date: str,
) -> list[dict]:
    """横截面：多只股票 × 单一日期。

    Args:
        symbols: 1..200 只代码
        date: 目标日期 ``YYYY-MM-DD``

    Returns:
        命中的 (symbol, date) 行；未命中的 symbol 不会出现在结果里（调用方
        可结合 ``list_symbols`` 自行判断缺失）。
    """
    if not symbols:
        raise ValueError("symbols 不能为空")
    if len(symbols) > 200:
        raise ValueError(f"symbols 最多 200 只，得到 {len(symbols)}")
    normalized = [normalize_symbol(s) for s in symbols]
    placeholders = ",".join("?" for _ in normalized)
    sql = (
        f"SELECT symbol, date, open, high, low, close, volume, turnover "
        f"FROM stock_daily WHERE date = ? AND symbol IN ({placeholders})"
    )
    params: list = [date, *normalized]
    with open_readonly(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def latest_for_symbols(
    db_path: str | Path,
    symbols: list[str],
) -> list[dict]:
    """批量取每只股票的最新一行（按 max(date) 找）。"""
    if not symbols:
        raise ValueError("symbols 不能为空")
    if len(symbols) > 500:
        raise ValueError(f"symbols 最多 500 只，得到 {len(symbols)}")
    normalized = [normalize_symbol(s) for s in symbols]
    placeholders = ",".join("?" for _ in normalized)
    # SQLite 不直接支持 ``IN`` + 关联子查询聚合的"每组最新一行"，
    # 用 ``GROUP BY symbol`` + 自连接最稳。
    sql = f"""
        SELECT s.symbol, s.date, s.open, s.high, s.low, s.close, s.volume, s.turnover
        FROM stock_daily s
        JOIN (
            SELECT symbol, MAX(date) AS max_date
            FROM stock_daily
            WHERE symbol IN ({placeholders})
            GROUP BY symbol
        ) m ON s.symbol = m.symbol AND s.date = m.max_date
    """
    with open_readonly(db_path) as conn:
        rows = conn.execute(sql, normalized).fetchall()
    return [dict(r) for r in rows]


def db_stats(db_path: str | Path) -> dict:
    """数据库总览：symbol 数、row 总数、覆盖区间。"""
    with open_readonly(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS symbol_count FROM ("
            "SELECT DISTINCT symbol FROM stock_daily)"
        ).fetchone()
        total = conn.execute("SELECT COUNT(*) FROM stock_daily").fetchone()
        span = conn.execute(
            "SELECT MIN(date) AS first_date, MAX(date) AS last_date "
            "FROM stock_daily"
        ).fetchone()
    return {
        "symbol_count": row["symbol_count"],
        "row_count": total[0],
        "first_date": span["first_date"],
        "last_date": span["last_date"],
    }
