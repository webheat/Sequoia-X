"""Pydantic schemas：HTTP 响应模型。

snake_case 与 DB 列对齐；前端要驼峰自己在边界层转。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class OhlcvRow(BaseModel):
    """单日 K 线一行。"""

    date: str
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None
    turnover: float | None = None


class CrossSectionRow(BaseModel):
    """横截面一行（多只 × 单日）。"""

    symbol: str
    date: str
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None
    turnover: float | None = None


class SymbolMeta(BaseModel):
    symbol: str
    first_date: str
    last_date: str
    row_count: int


class DbStats(BaseModel):
    symbol_count: int
    row_count: int
    first_date: str
    last_date: str


class OhlcvQuery(BaseModel):
    """``GET /symbols/{code}/ohlcv`` 的响应包装。"""

    symbol: str
    count: int
    rows: list[OhlcvRow]


class CrossSectionResponse(BaseModel):
    date: str
    requested: int = Field(description="请求的 symbol 数")
    returned: int = Field(description="命中的行数")
    rows: list[CrossSectionRow]


class LatestResponse(BaseModel):
    requested: int
    returned: int
    rows: list[CrossSectionRow]


class SymbolsResponse(BaseModel):
    count: int
    symbols: list[str]


class ErrorResponse(BaseModel):
    detail: str
    code: Literal["bad_request", "not_found", "internal"] = "bad_request"
