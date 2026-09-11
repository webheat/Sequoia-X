"""数据引擎模块：负责 SQLite 行情数据存储与 baostock 增量同步。"""

import sqlite3
import time
from pathlib import Path

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)


# ── SQLite 健壮性配置 ──
# journal_mode=WAL：读写不互斥，sync_today_bulk 写盘时策略仍可读
# synchronous=NORMAL：WAL 下安全但比 FULL 快很多
# busy_timeout：单次锁竞争最长等待，避免瞬时报"database is locked"
_DB_OPEN_RETRIES = 3
_DB_OPEN_BACKOFF_BASE = 0.1  # 秒，指数退避：0.1 / 0.3 / 0.9
_RETRYABLE_OPEN_ERRORS = ("unable to open", "database is locked", "disk i/o error")


def _open_db(path: str) -> sqlite3.Connection:
    """打开 SQLite 连接，附带 WAL 兼容 PRAGMA，并对瞬时错误重试。

    WAL 模式是 DB 级的持久设置（写入 header），只需在某次连接上设置一次。
    busy_timeout / synchronous 是每连接 PRAGMA，每次新连接都设置。
    """
    last_exc: sqlite3.OperationalError | None = None
    for attempt in range(_DB_OPEN_RETRIES):
        try:
            conn = sqlite3.connect(path, timeout=30.0)
            # journal_mode 在 WAL 与 DELETE 之间切换是 DB 级持久操作，
            # 重复执行无副作用（已 WAL 时返回 "wal"）
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            return conn
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if not any(token in msg for token in _RETRYABLE_OPEN_ERRORS):
                raise
            last_exc = exc
            if attempt < _DB_OPEN_RETRIES - 1:
                backoff = _DB_OPEN_BACKOFF_BASE * (3 ** attempt)
                logger.warning(
                    f"sqlite3 打开失败（{exc!s}），{backoff:.1f}s 后重试 "
                    f"[{attempt + 1}/{_DB_OPEN_RETRIES}]"
                )
                time.sleep(backoff)
    assert last_exc is not None
    raise last_exc


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stock_daily (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol   TEXT    NOT NULL,
    date     TEXT    NOT NULL,
    open     REAL,
    high     REAL,
    low      REAL,
    close    REAL,
    volume   REAL,
    turnover REAL,
    UNIQUE (symbol, date)
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_symbol_date ON stock_daily (symbol, date);
"""


def _bs_fetch_batch(tasks: list) -> list:
    """多进程 worker：独立 login，批量拉取 baostock 数据。

    防御性：
    - 每个 worker 设置 socket 超时
    - login 失败重试 2 次（指数退避），单次 baostock 抽风不至于整批返空
    - login 成功后对 baostock 单例 socket 显式 settimeout，覆盖历史无超时 socket
    - 单个 worker 卡死或失败时返回空列表，不影响其他 worker
    """
    import socket as _socket
    _socket.setdefaulttimeout(15.0)

    import baostock as bs
    import baostock.common.context as _bsctx

    def _close_residual_socket() -> None:
        """关掉 baostock 单例 socket，不关下次 login 会复用可能已损坏的连接。"""
        sock = getattr(_bsctx, "default_socket", None)
        if sock is None:
            return
        try:
            sock.close()
        except OSError:
            pass
        try:
            delattr(_bsctx, "default_socket")
        except AttributeError:
            pass

    # login 重试：最多 3 次，每次失败前彻底关闭旧 socket 让下次 login 重建
    lg = None
    last_err: str | None = None
    for attempt in range(3):
        _close_residual_socket()
        try:
            lg = bs.login()
            last_err = None if lg.error_code == "0" else lg.error_msg
        except Exception as exc:
            last_err = f"exception: {exc}"
            lg = None
        if last_err is None:
            break
        if attempt < 2:
            time.sleep(2 ** attempt)
    if lg is None or lg.error_code != "0":
        print(f"[worker] bs.login() 三次均失败: {last_err}")
        return []

    # login 成功后：显式给单例 socket 设超时，覆盖"之前已存在无超时 socket"的情况
    sock = getattr(_bsctx, "default_socket", None)
    if sock is not None:
        try:
            sock.settimeout(15.0)
        except OSError:
            pass

    results = []
    try:
        for symbol, bs_code, start, end in tasks:
            try:
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "date,open,high,low,close,volume,amount",
                    start_date=start,
                    end_date=end,
                    frequency="d",
                    adjustflag="1",  # 后复权
                )
                if rs.error_code != "0":
                    continue
                while rs.next():
                    results.append([symbol] + rs.get_row_data())
            except Exception as exc:
                print(f"[worker] {symbol} 查询失败: {exc}")
                continue
    finally:
        try:
            bs.logout()
        except Exception:
            pass
    return results


class DataEngine:
    """行情数据引擎，负责 SQLite 存储和 baostock 数据同步。"""

    def __init__(self, settings: Settings) -> None:
        self.db_path: str = settings.db_path
        self.start_date: str = settings.start_date
        self._init_db()

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with _open_db(self.db_path) as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            conn.commit()
        logger.info(f"数据库初始化完成：{self.db_path}")

    def _get_last_date(self, symbol: str) -> str | None:
        with _open_db(self.db_path) as conn:
            row = conn.execute(
                "SELECT MAX(date) FROM stock_daily WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        return row[0] if row and row[0] else None

    def get_ohlcv(self, symbol: str) -> pd.DataFrame:
        with _open_db(self.db_path) as conn:
            df = pd.read_sql(
                "SELECT * FROM stock_daily WHERE symbol = ? ORDER BY date",
                conn,
                params=(symbol,),
            )
        return df

    @staticmethod
    def _to_baostock_code(symbol: str) -> str:
        """将纯数字代码转为 baostock 格式：6/9开头 -> sh，其余 -> sz。"""
        prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
        return f"{prefix}.{symbol}"

    # ── 数据同步 ──

    def sync_today_bulk(self) -> int:
        """多进程并行通过 baostock 拉取增量数据（后复权），写入 SQLite。"""
        from datetime import date, timedelta
        import multiprocessing

        today_str = date.today().strftime("%Y-%m-%d")

        tasks = []
        with _open_db(self.db_path) as conn:
            rows = conn.execute(
                "SELECT symbol, MAX(date) FROM stock_daily GROUP BY symbol"
            ).fetchall()

        if not rows:
            logger.warning("本地无股票数据，请先执行 --backfill")
            return 0

        for symbol, last_date in rows:
            if last_date and last_date >= today_str:
                continue
            start = today_str
            if last_date:
                start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")
            tasks.append((symbol, self._to_baostock_code(symbol), start, today_str))

        if not tasks:
            logger.info("所有股票已是最新，无需更新")
            return 0

        logger.info(f"需要更新 {len(tasks)} 只股票，启动多进程并行拉取...")

        n_workers = min(4, len(tasks))
        chunks = [tasks[i::n_workers] for i in range(n_workers)]

        # 用 fork 上下文并设置硬超时，防止某个 worker 卡死拖垮主流程
        ctx = multiprocessing.get_context("fork")
        batch_results: list = []
        with ctx.Pool(n_workers) as pool:
            try:
                async_result = pool.map_async(_bs_fetch_batch, chunks)
                batch_results = async_result.get(timeout=180)  # 3 分钟硬超时
            except multiprocessing.TimeoutError:
                logger.error("sync_today_bulk 超过 3 分钟未返回，强制终止 Pool")
                pool.terminate()
                pool.join()
                batch_results = []
            except Exception as exc:
                logger.error(f"sync_today_bulk 异常: {exc}")
                pool.terminate()
                pool.join()
                batch_results = []

        all_rows = []
        for batch in batch_results:
            all_rows.extend(batch)

        if not all_rows:
            logger.info("无新数据（可能非交易日）")
            return 0

        df = pd.DataFrame(all_rows, columns=["symbol", "date", "open", "high", "low", "close", "volume", "turnover"])
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        df = df[df["volume"] > 0]

        count = len(df)
        with _open_db(self.db_path) as conn:
            for d in df["date"].unique().tolist():
                conn.execute("DELETE FROM stock_daily WHERE date = ?", (d,))
            df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi", chunksize=500)
            conn.commit()

        logger.info(f"sync_today_bulk: 写入 {count} 条数据")
        return count

    def backfill(self, symbols: list[str]) -> None:
        """通过 baostock 批量回填历史日 K 线数据（后复权）。

        容错机制：
        - 单只股票失败自动重试 3 次，间隔递增（2s/4s/8s）
        - 每 200 只股票自动重连 baostock（防止长连接超时）
        - 已入库的自动 skip，中断后可重跑续传
        """
        import time
        from datetime import date, timedelta

        import baostock as bs

        today_str = date.today().strftime("%Y-%m-%d")
        max_retries = 3
        reconnect_interval = 200  # 每处理 N 只股票重连一次

        def _login():
            lg = bs.login()
            if lg.error_code != "0":
                logger.error(f"baostock 登录失败: {lg.error_msg}")
                return False
            return True

        if not _login():
            return

        success = 0
        skipped = 0
        failed = 0
        since_reconnect = 0

        try:
            for i, symbol in enumerate(symbols):
                last_date = self._get_last_date(symbol)
                if last_date and last_date >= today_str:
                    skipped += 1
                    if (i + 1) % 500 == 0:
                        logger.info(
                            f"已处理 {i + 1}/{len(symbols)}，"
                            f"成功 {success} 跳过 {skipped} 失败 {failed}"
                        )
                    continue

                # 定期重连，防止长连接超时
                since_reconnect += 1
                if since_reconnect >= reconnect_interval:
                    bs.logout()
                    time.sleep(1)
                    if not _login():
                        logger.error("重连失败，终止回填")
                        return
                    since_reconnect = 0

                start = last_date or self.start_date
                if last_date:
                    start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")

                bs_code = self._to_baostock_code(symbol)

                # 带重试的查询
                rows = []
                query_ok = False
                for attempt in range(max_retries):
                    try:
                        rs = bs.query_history_k_data_plus(
                            bs_code,
                            "date,open,high,low,close,volume,amount",
                            start_date=start,
                            end_date=today_str,
                            frequency="d",
                            adjustflag="1",  # 后复权
                        )

                        if rs.error_code != "0":
                            raise RuntimeError(rs.error_msg)

                        rows = []
                        while rs.next():
                            rows.append(rs.get_row_data())
                        query_ok = True
                        break

                    except Exception as exc:
                        if attempt < max_retries - 1:
                            wait = 2 ** (attempt + 1)
                            logger.warning(
                                f"[{symbol}] 第{attempt + 1}次失败: {exc}，{wait}s 后重试"
                            )
                            time.sleep(wait)
                            # 重连 baostock
                            bs.logout()
                            time.sleep(1)
                            _login()
                        else:
                            logger.warning(f"[{symbol}] {max_retries}次重试均失败，跳过")

                if not query_ok:
                    failed += 1
                    continue

                if not rows:
                    skipped += 1
                    continue

                df = pd.DataFrame(rows, columns=rs.fields)
                for col in ["open", "high", "low", "close", "volume", "amount"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                df = df.dropna(subset=["close"])
                df = df[df["volume"] > 0]

                if df.empty:
                    skipped += 1
                    continue

                df["symbol"] = symbol
                df = df.rename(columns={"amount": "turnover"})
                df = df[["symbol", "date", "open", "high", "low", "close", "volume", "turnover"]]

                try:
                    with _open_db(self.db_path) as conn:
                        df.to_sql(
                            "stock_daily", conn, if_exists="append",
                            index=False, method="multi", chunksize=500,
                        )
                except sqlite3.IntegrityError:
                    pass

                success += 1

                if (i + 1) % 500 == 0:
                    logger.info(
                        f"已处理 {i + 1}/{len(symbols)}，"
                        f"成功 {success} 跳过 {skipped} 失败 {failed}"
                    )

        finally:
            bs.logout()

        logger.info(f"回填完成 — 成功: {success} | 跳过: {skipped} | 失败: {failed}")

    # ── 股票列表 ──

    def get_all_symbols(self) -> list[str]:
        """通过 baostock 获取全市场 A 股代码列表。"""
        import baostock as bs

        lg = bs.login()
        if lg.error_code != "0":
            logger.error(f"baostock 登录失败: {lg.error_msg}")
            return []

        try:
            rs = bs.query_stock_basic(code_name="", code="")
            symbols = []
            while rs.next():
                row = rs.get_row_data()
                code = row[0]           # "sh.600000" or "sz.000001"
                status = row[4]         # "1" = 上市
                stock_type = row[5]     # "1" = 股票
                if status == "1" and stock_type == "1":
                    symbols.append(code.split(".")[1])  # 提取纯数字代码
            logger.info(f"获取股票列表完成，共 {len(symbols)} 只")
            return symbols
        except Exception as e:
            logger.error(f"获取股票列表失败: {e}")
            return []
        finally:
            bs.logout()

    def get_local_symbols(self) -> list[str]:
        with _open_db(self.db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM stock_daily"
            ).fetchall()
        return [row[0] for row in rows]
