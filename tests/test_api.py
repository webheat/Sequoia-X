"""API 层单元测试：db helper 行为 + 真实 DB 冒烟。

注意：``test_db.py`` 涉及读 ``data/sequoia_v2.db`` 时用 monkeypatch 把 DB_PATH
切到 fixture 临时库，避免污染生产数据。
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from sequoia_x.api import db


# ── normalize_symbol ──

class TestNormalizeSymbol:
    def test_plain_six_digits(self) -> None:
        assert db.normalize_symbol("000001") == "000001"

    def test_with_prefix_dot(self) -> None:
        assert db.normalize_symbol("sh.600000") == "600000"
        assert db.normalize_symbol("sz.000001") == "000001"
        assert db.normalize_symbol("bj.830799") == "830799"

    def test_with_prefix_no_dot(self) -> None:
        assert db.normalize_symbol("SH600000") == "600000"
        assert db.normalize_symbol("sz000001") == "000001"

    def test_strip_whitespace(self) -> None:
        assert db.normalize_symbol("  000001  ") == "000001"

    @pytest.mark.parametrize("bad", ["", "abc", "12345", "1234567", "sh.12", "hk.00700"])
    def test_invalid(self, bad: str) -> None:
        with pytest.raises(ValueError):
            db.normalize_symbol(bad)


# ── 业务查询（用临时 DB 隔离）──

def _make_temp_db() -> str:
    """构造一个有 2 只股票 × 5 天数据的临时 SQLite。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "t.db")
        rows = []
        for i, sym in enumerate(["000001", "600000"]):
            for d_off in range(5):
                rows.append({
                    "symbol": sym,
                    "date": str(date(2024, 1, 1 + d_off)),
                    "open": 10.0 + i, "high": 11.0 + i, "low": 9.0 + i,
                    "close": 10.5 + i, "volume": 1000.0 * (d_off + 1),
                    "turnover": 10500.0,
                })
        df = pd.DataFrame(rows)
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE stock_daily ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "symbol TEXT NOT NULL, date TEXT NOT NULL, "
                "open REAL, high REAL, low REAL, close REAL, "
                "volume REAL, turnover REAL, UNIQUE(symbol, date))"
            )
            df.to_sql("stock_daily", conn, if_exists="append", index=False)
        # tempfile 在 with 退出时会被清，必须复制出持久路径
        persist = str(Path(tmp).parent / f"t_{id(df)}.db")
        Path(path).replace(persist)
        return persist


@pytest.fixture
def temp_db() -> str:
    p = _make_temp_db()
    yield p
    Path(p).unlink(missing_ok=True)


class TestListSymbols:
    def test_returns_sorted(self, temp_db: str) -> None:
        syms = db.list_symbols(temp_db)
        assert syms == ["000001", "600000"]


class TestGetSymbolMeta:
    def test_existing(self, temp_db: str) -> None:
        meta = db.get_symbol_meta(temp_db, "000001")
        assert meta is not None
        assert meta["symbol"] == "000001"
        assert meta["row_count"] == 5
        assert meta["first_date"] == "2024-01-01"
        assert meta["last_date"] == "2024-01-05"

    def test_missing(self, temp_db: str) -> None:
        assert db.get_symbol_meta(temp_db, "999999") is None

    def test_with_prefix(self, temp_db: str) -> None:
        meta = db.get_symbol_meta(temp_db, "sh.600000")
        assert meta is not None
        assert meta["symbol"] == "600000"


class TestQueryOhlcv:
    def test_basic(self, temp_db: str) -> None:
        rows = db.query_ohlcv(temp_db, "000001", limit=10)
        assert len(rows) == 5
        assert [r["date"] for r in rows] == [
            "2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05",
        ]

    def test_date_range(self, temp_db: str) -> None:
        rows = db.query_ohlcv(temp_db, "000001", start="2024-01-02", end="2024-01-04")
        assert len(rows) == 3
        assert rows[0]["date"] == "2024-01-02"
        assert rows[-1]["date"] == "2024-01-04"

    def test_desc_order(self, temp_db: str) -> None:
        rows = db.query_ohlcv(temp_db, "000001", order="desc", limit=2)
        assert [r["date"] for r in rows] == ["2024-01-05", "2024-01-04"]

    def test_offset(self, temp_db: str) -> None:
        rows = db.query_ohlcv(temp_db, "000001", limit=2, offset=2)
        assert [r["date"] for r in rows] == ["2024-01-03", "2024-01-04"]

    def test_limit_bounds(self, temp_db: str) -> None:
        with pytest.raises(ValueError):
            db.query_ohlcv(temp_db, "000001", limit=0)
        with pytest.raises(ValueError):
            db.query_ohlcv(temp_db, "000001", limit=5001)
        with pytest.raises(ValueError):
            db.query_ohlcv(temp_db, "000001", offset=-1)

    def test_unknown_symbol_raises(self, temp_db: str) -> None:
        with pytest.raises(ValueError, match="不存在"):
            db.query_ohlcv(temp_db, "999999")

    def test_empty_range_returns_empty(self, temp_db: str) -> None:
        rows = db.query_ohlcv(temp_db, "000001", start="2025-01-01", end="2025-12-31")
        assert rows == []


class TestCrossSection:
    def test_hit(self, temp_db: str) -> None:
        rows = db.cross_section(temp_db, ["000001", "600000"], "2024-01-03")
        assert len(rows) == 2
        syms = {r["symbol"] for r in rows}
        assert syms == {"000001", "600000"}

    def test_partial_hit(self, temp_db: str) -> None:
        # 999999 不存在，结果里只有 000001
        rows = db.cross_section(temp_db, ["000001", "999999"], "2024-01-03")
        assert len(rows) == 1
        assert rows[0]["symbol"] == "000001"

    def test_too_many_symbols(self, temp_db: str) -> None:
        with pytest.raises(ValueError, match="最多 200"):
            db.cross_section(temp_db, [f"{i:06d}" for i in range(201)], "2024-01-01")

    def test_empty_symbols(self, temp_db: str) -> None:
        with pytest.raises(ValueError, match="不能为空"):
            db.cross_section(temp_db, [], "2024-01-01")


class TestLatest:
    def test_returns_one_per_symbol(self, temp_db: str) -> None:
        rows = db.latest_for_symbols(temp_db, ["000001", "600000"])
        assert len(rows) == 2
        for r in rows:
            assert r["date"] == "2024-01-05"  # max date

    def test_unknown_symbol_missing(self, temp_db: str) -> None:
        rows = db.latest_for_symbols(temp_db, ["000001", "999999"])
        assert len(rows) == 1
        assert rows[0]["symbol"] == "000001"


class TestDbStats:
    def test_basic(self, temp_db: str) -> None:
        s = db.db_stats(temp_db)
        assert s["symbol_count"] == 2
        assert s["row_count"] == 10
        assert s["first_date"] == "2024-01-01"
        assert s["last_date"] == "2024-01-05"


# ── 真实 DB 冒烟（可选，没数据时 skip）──

class TestRealDb:
    """在 ``data/sequoia_v2.db`` 存在时跑一次冒烟，确保归一化/查询路径真实可用。"""

    REAL_DB = Path(__file__).resolve().parents[1] / "data" / "sequoia_v2.db"

    @pytest.fixture(autouse=True)
    def _skip_if_no_db(self) -> None:
        if not self.REAL_DB.exists():
            pytest.skip(f"未找到 {self.REAL_DB}")

    def test_health_against_real_db(self) -> None:
        stats = db.db_stats(str(self.REAL_DB))
        assert stats["symbol_count"] > 0
        assert stats["row_count"] > 0

    def test_query_real_symbol(self) -> None:
        # 000001（平安银行）实际不在本数据集中；用真实存在的 000034 起手
        rows = db.query_ohlcv(str(self.REAL_DB), "000034", limit=5)
        assert len(rows) > 0
        assert "date" in rows[0]
        assert "close" in rows[0]

    def test_prefix_normalization_real(self) -> None:
        meta_plain = db.get_symbol_meta(str(self.REAL_DB), "000034")
        meta_prefixed = db.get_symbol_meta(str(self.REAL_DB), "sz.000034")
        assert meta_plain is not None
        assert meta_prefixed is not None
        assert meta_plain["first_date"] == meta_prefixed["first_date"]

    def test_sh_prefix_600000(self) -> None:
        # 600xxx 必须配 sh. 前缀
        meta = db.get_symbol_meta(str(self.REAL_DB), "sh.600000")
        assert meta is not None
        assert meta["symbol"] == "600000"
