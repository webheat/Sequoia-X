"""数据引擎模块：负责 SQLite 行情数据存储与 baostock 增量同步。"""

import gc
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)


# ── sync_today_bulk 调优 ──
# 9/14 18:07 失败直接原因：baostock 服务端 hang，worker login 成功但无 query 返回。
# 整体硬超时 3 分钟不够 → 提到 5 分钟；worker 内单只股票查询加 30s per-task
# 主动放弃，避免一只卡死拖垮整批。
_SYNC_BULK_TIMEOUT_S = 300          # 整体硬超时（原 180s）
_BS_TASK_TIMEOUT_S = 30             # 单只股票 query 最长等待


# ── 模块级 ohlcv 缓存 ──
# 9/14 fd 撞顶根因之二：8 个策略按 symbol 循环调用 engine.get_ohlcv()，每次
# with _open_db() 在 Python 3.14 下不 close fd，6 × 666 = ~4000 fd 泄漏，撞
# ulimit 1024。preload 一次性读全表 → groupby → 共享 dict 引用，内存只 load 一次
# （实测 ~268 MB），后续 get_ohlcv(symbol) 走 O(1) dict 命中，0 fd 开销。
# 仅当显式调用 preload_all_ohlcv() 才填充；否则 get_ohlcv 自动 fall back 到旧路径。
_OHLCV_CACHE: dict[str, pd.DataFrame] | None = None
_OHLCV_FULL_DF: pd.DataFrame | None = None


# ── SQLite 健壮性配置 ──
# journal_mode=WAL：读写不互斥，sync_today_bulk 写盘时策略仍可读
# synchronous=NORMAL：WAL 下安全但比 FULL 快很多
# busy_timeout：单次锁竞争最长等待，避免瞬时报"database is locked"
_DB_OPEN_RETRIES = 3
_DB_OPEN_BACKOFF_BASE = 0.1  # 秒，指数退避：0.1 / 0.3 / 0.9
_RETRYABLE_OPEN_ERRORS = ("unable to open", "database is locked", "disk i/o error")


@contextmanager
def _open_db(path: str):
    """打开 SQLite 连接（context manager），附带 WAL 兼容 PRAGMA，并对瞬时错误重试。

    Python 3.14 下 ``sqlite3.Connection.__exit__`` 不会自动 close，with-block 退出后
    fd 仍持有——单次连接泄漏 1 fd，6 策略 × 666 symbol 累积可达 4000+。
    这里用 try/finally 显式 ``conn.close()``，确保 fd 不泄漏。

    WAL 模式是 DB 级的持久设置（写入 header），只需在某次连接上设置一次。
    busy_timeout / synchronous 是每连接 PRAGMA，每次新连接都设置。
    """
    conn: sqlite3.Connection | None = None
    last_exc: sqlite3.OperationalError | None = None
    for attempt in range(_DB_OPEN_RETRIES):
        try:
            conn = sqlite3.connect(path, timeout=30.0)
            # journal_mode 在 WAL 与 DELETE 之间切换是 DB 级持久操作，
            # 重复执行无副作用（已 WAL 时返回 "wal"）
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            break
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
    if conn is None:
        assert last_exc is not None
        raise last_exc
    try:
        yield conn
    finally:
        # 9/14 fd 撞顶修复：Python 3.14 下 __exit__ 不 close，必须显式释放。
        # 关闭触发 SQLite 把 WAL buffer flush 回 -wal 文件，下次连接仍能看到。
        try:
            conn.close()
        except Exception:
            pass


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
            # per-task 计时：单只查询超 30s 主动放弃，避免 baostock hang 拖垮整批
            task_start = time.monotonic()
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
                    if time.monotonic() - task_start > _BS_TASK_TIMEOUT_S:
                        logger.warning(
                            f"[worker] {symbol} 单只查询超过 {_BS_TASK_TIMEOUT_S}s，"
                            f"提前结束（bs 可能 hang）"
                        )
                        break
            except Exception as exc:
                print(f"[worker] {symbol} 查询失败: {exc}")
                continue
    finally:
        # 9/11 18:07 失败时主进程触发 `OSError: [Errno 24] Too many open files:
        # .../netrc.py`，根因是 fork 后子进程未显式释放 importlib 副作用 fd。
        # logout 已 close baostock 单例 socket，此处再 gc + 二次 close 兜底，
        # 并清掉对 default_socket 的引用，确保 fork 出去的进程从干净状态起。
        try:
            bs.logout()
        except Exception:
            pass
        try:
            sock = getattr(_bsctx, "default_socket", None)
            if sock is not None:
                sock.close()
        except OSError:
            pass
        try:
            delattr(_bsctx, "default_socket")
        except AttributeError:
            pass
        # 强制回收 baostock 单例 / ResultData / netrc 等本地引用
        gc.collect()
    return results


def _close_inherited_fds() -> None:
    """fork 出 worker 前，关闭父进程 >= 10 的 fd，避免子进程继承膨胀。

    POSIX 0/1/2 是 stdin/stdout/stderr 必须保留；>= 10 的 fd 大多是模块导入副作用
    （netrc / .pyc 缓存 / 临时 .env 文件 / baostock 单例 socket / SQLite WAL/SHM），
    子进程内 _bs_fetch_batch / _open_db 会按需重新打开，关闭父进程旧 fd 无副作用。
    """
    fd_dir = f"/proc/{os.getpid()}/fd"
    try:
        names = os.listdir(fd_dir)
    except OSError:
        return
    closed = 0
    for name in names:
        try:
            n = int(name)
        except ValueError:
            continue
        if n < 10:
            continue
        try:
            os.close(n)
            closed += 1
        except OSError:
            # fd 可能已经被 GC 关闭，忽略
            pass
    if closed:
        logger.info(f"fork 前关闭父进程 {closed} 个 fd（>=10），减少 worker fd 继承")


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
        """取单只股票 OHLCV。优先命中 preload 缓存；未 preload 时走 with-block。

        缓存命中 = 0 sqlite fd；fall back 路径在 Py 3.14 下会泄漏 fd（已修，见
        _open_db contextmanager），但单测 / 手工跑场景下规模小可接受。
        """
        if _OHLCV_CACHE is not None and symbol in _OHLCV_CACHE:
            return _OHLCV_CACHE[symbol]
        with _open_db(self.db_path) as conn:
            df = pd.read_sql(
                "SELECT * FROM stock_daily WHERE symbol = ? ORDER BY date",
                conn,
                params=(symbol,),
            )
        return df

    def preload_all_ohlcv(self) -> None:
        """一次性读全表 → groupby → 填到模块级缓存。8 策略共享 dict 引用，
        内存只 load 一次。失败时 _OHLCV_CACHE 置空 dict 让 get_ohlcv fall back。
        """
        global _OHLCV_CACHE, _OHLCV_FULL_DF
        if _OHLCV_CACHE is not None:
            # 已被填充（含失败 fallback 的 {}）—— 幂等
            return
        try:
            with _open_db(self.db_path) as conn:
                df = pd.read_sql(
                    "SELECT symbol, date, open, high, low, close, volume, turnover "
                    "FROM stock_daily",
                    conn,
                )
            df = df.sort_values(["symbol", "date"])
            _OHLCV_FULL_DF = df  # 给 get_all_close_high 用，避免二次 groupby
            _OHLCV_CACHE = {
                sym: g.drop(columns=["symbol"]).reset_index(drop=True)
                for sym, g in df.groupby("symbol", sort=False)
            }
            logger.info(f"preload_all_ohlcv 完成: {len(_OHLCV_CACHE)} 只股票")
        except Exception as exc:
            logger.warning(
                f"preload_all_ohlcv 失败，fall back 到逐 symbol 查询: {exc}"
            )
            _OHLCV_CACHE = {}  # 标记已尝试，避免每次都重试
            _OHLCV_FULL_DF = None

    def get_all_close_high(self) -> pd.DataFrame:
        """rps_breakout 用：返回包含 symbol/date/close/high 四列的全量 DataFrame。
        优先复用 preload 缓存的原始 df；未 preload 时显式调用一次。
        """
        global _OHLCV_FULL_DF
        if _OHLCV_FULL_DF is None:
            self.preload_all_ohlcv()
        if _OHLCV_FULL_DF is None:
            # preload 失败 → 走 with-block 兜底
            with _open_db(self.db_path) as conn:
                return pd.read_sql(
                    "SELECT symbol, date, close, high FROM stock_daily",
                    conn,
                )
        return _OHLCV_FULL_DF[["symbol", "date", "close", "high"]]

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

        # 9/11 18:07 cron 失败时主进程触发 `OSError: [Errno 24] Too many open files`：
        # fork 出去的 worker 继承父进程全部 fd（含 baostock 单例 socket / SQLite WAL），
        # 各自 import baostock → requests → netrc.py 又新开 fd，叠加触发上限。
        # 减半到 2 worker + fork 前关闭父进程 >= 10 的 fd，把继承面降到最小。
        # 0/1/2 是 stdin/stdout/stderr 必须保留；SQLite/baostock fd 都 >= 10，子进程
        # 内 _bs_fetch_batch / _open_db 会按需重新打开，关闭父进程旧 fd 无副作用。
        n_workers = min(2, len(tasks))
        chunks = [tasks[i::n_workers] for i in range(n_workers)]
        _close_inherited_fds()

        # 用 fork 上下文并设置硬超时，防止某个 worker 卡死拖垮主流程
        ctx = multiprocessing.get_context("fork")
        batch_results: list = []
        with ctx.Pool(n_workers) as pool:
            try:
                async_result = pool.map_async(_bs_fetch_batch, chunks)
                # 9/14 修复：3 → 5 分钟——baostock hang 时 3 分钟临界，
                # 5 分钟留出重连/重试余量；单只 per-task 30s timeout 已在 _bs_fetch_batch 内做。
                batch_results = async_result.get(timeout=_SYNC_BULK_TIMEOUT_S)
            except multiprocessing.TimeoutError:
                logger.error(
                    f"sync_today_bulk 超过 {_SYNC_BULK_TIMEOUT_S}s 未返回，强制终止 Pool"
                )
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
            # 幂等 upsert：先写 TEMP 表，再 INSERT OR REPLACE 回主表。
            # 千万不要 DELETE 整日期：sync_today_bulk 实际只补了"上次同步失败"的
            # 一部分股票，如果按日期 DELETE 再 INSERT，会把日期维度上其他股票的
            # 现有数据也删掉（2026-09-11 那次事故就是这么丢的 5 天 × 583 股数据）。
            # 表上有 UNIQUE(symbol, date)，INSERT OR REPLACE 走单行 upsert。
            conn.execute("CREATE TEMP TABLE _upsert AS SELECT * FROM stock_daily WHERE 0")
            df.to_sql(
                "_upsert", conn, if_exists="append",
                index=False, method="multi", chunksize=500,
            )
            conn.execute(
                "INSERT OR REPLACE INTO stock_daily "
                "(symbol, date, open, high, low, close, volume, turnover) "
                "SELECT symbol, date, open, high, low, close, volume, turnover FROM _upsert"
            )
            conn.execute("DROP TABLE _upsert")
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
