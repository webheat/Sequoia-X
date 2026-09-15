"""FastAPI app：HTTP 数据服务。

启动::

    python serve.py                       # 默认 0.0.0.0:8000
    uvicorn sequoia_x.api.app:app --host 0.0.0.0 --port 8000

设计：
- 只读，无状态（每请求新开 SQLite 连接）
- 不依赖 main.py / DataEngine，可以独立跑
- 鉴权留 hook（环境变量 ``SEQUOIA_API_TOKEN``，当前未启用 → 仍开 0.0.0.0）
"""

from __future__ import annotations

import os
from datetime import date as _date

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from sequoia_x.api import db
from sequoia_x.api.schemas import (
    CrossSectionResponse,
    DbStats,
    LatestResponse,
    OhlcvQuery,
    SymbolMeta,
    SymbolsResponse,
)
from sequoia_x.core.config import get_settings

# ── 启动时校验配置 ──
# Settings.feishu_webhook_url 是必填，api 服务不需要它；但仍然要 settings 才能
# 拿到 db_path。如果 .env 缺 feishu_webhook_url，假装一个空值以绕开校验。
try:
    settings = get_settings()
    DB_PATH = settings.db_path
except Exception:
    # api 服务允许独立启动，不强制依赖飞书配置
    DB_PATH = os.environ.get("DB_PATH", "data/sequoia_v2.db")


def _validate_date(s: str, field: str) -> str:
    try:
        _date.fromisoformat(s)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{field} 不是合法日期 (YYYY-MM-DD): {s!r}",
        ) from exc
    return s


def create_app() -> FastAPI:
    app = FastAPI(
        title="Sequoia-X Data API",
        version="1.0.0",
        description=(
            "A 股日 K 数据只读服务。后复权，数据源 baostock。\n\n"
            "底层 SQLite WAL 模式，写并发由 main.py 日常 cron 完成；本服务"
            "只读不写。"
        ),
    )

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        """存活探针：检查 DB 是否可读。"""
        try:
            stats = db.db_stats(DB_PATH)
            return {"ok": True, **stats}
        except Exception as exc:
            return JSONResponse(
                status_code=503,
                content={"ok": False, "error": str(exc)},
            )

    @app.get("/meta", response_model=DbStats, tags=["meta"])
    def get_meta() -> DbStats:
        """数据库总览：标的数、row 总数、覆盖区间。"""
        return DbStats(**db.db_stats(DB_PATH))

    @app.get("/symbols", response_model=SymbolsResponse, tags=["symbols"])
    def get_symbols() -> SymbolsResponse:
        """本地有数据的全部股票代码。"""
        syms = db.list_symbols(DB_PATH)
        return SymbolsResponse(count=len(syms), symbols=syms)

    @app.get("/symbols/{symbol}", response_model=SymbolMeta, tags=["symbols"])
    def get_symbol(symbol: str) -> SymbolMeta:
        """单只股票元信息。"""
        try:
            meta = db.get_symbol_meta(DB_PATH, symbol)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if meta is None:
            raise HTTPException(status_code=404, detail=f"symbol 无数据: {symbol}")
        return SymbolMeta(**meta)

    @app.get("/symbols/{symbol}/ohlcv", response_model=OhlcvQuery, tags=["ohlcv"])
    def get_symbol_ohlcv(
        symbol: str,
        start: str | None = Query(None, description="起始日期 YYYY-MM-DD（含）"),
        end: str | None = Query(None, description="结束日期 YYYY-MM-DD（含）"),
        limit: int = Query(500, ge=1, le=5000),
        offset: int = Query(0, ge=0),
        order: str = Query("asc", pattern="^(asc|desc)$"),
    ) -> OhlcvQuery:
        """单只股票 OHLCV，按日期排序。"""
        if start:
            _validate_date(start, "start")
        if end:
            _validate_date(end, "end")
        try:
            rows = db.query_ohlcv(
                DB_PATH, symbol,
                start=start, end=end,
                limit=limit, offset=offset, order=order,
            )
        except ValueError as exc:
            msg = str(exc)
            if "不存在" in msg:
                raise HTTPException(status_code=404, detail=msg) from exc
            raise HTTPException(status_code=400, detail=msg) from exc
        # 归一化后的 symbol 替换入参（用户可能传 ``sh.000001``）
        canonical = db.normalize_symbol(symbol)
        return OhlcvQuery(
            symbol=canonical,
            count=len(rows),
            rows=rows,  # type: ignore[arg-type]
        )

    @app.get("/ohlcv", response_model=CrossSectionResponse, tags=["ohlcv"])
    def get_cross_section(
        date: str = Query(..., description="目标日期 YYYY-MM-DD"),
        symbols: str = Query(
            ..., description="逗号分隔的股票代码列表，1..200 只",
            examples=["000001,600000,300750"],
        ),
    ) -> CrossSectionResponse:
        """横截面：多只股票 × 单一日期。"""
        _validate_date(date, "date")
        sym_list = [s.strip() for s in symbols.split(",") if s.strip()]
        try:
            rows = db.cross_section(DB_PATH, sym_list, date)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return CrossSectionResponse(
            date=date,
            requested=len(sym_list),
            returned=len(rows),
            rows=rows,  # type: ignore[arg-type]
        )

    @app.get("/ohlcv/latest", response_model=LatestResponse, tags=["ohlcv"])
    def get_ohlcv_latest(
        symbols: str = Query(
            ..., description="逗号分隔的股票代码列表，1..500 只",
        ),
    ) -> LatestResponse:
        """批量取每只股票的最新一行。"""
        sym_list = [s.strip() for s in symbols.split(",") if s.strip()]
        try:
            rows = db.latest_for_symbols(DB_PATH, sym_list)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return LatestResponse(
            requested=len(sym_list),
            returned=len(rows),
            rows=rows,  # type: ignore[arg-type]
        )

    return app


app = create_app()
