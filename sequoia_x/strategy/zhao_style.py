"""赵新兵总风格选股策略：海龟突破与 RPS 动量的交集。"""

from __future__ import annotations

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class ZhaoStyleStrategy(BaseStrategy):
    """赵新兵总风格：突破 + 动量 + 流动性 + 板块偏好。

    入场信号要求海龟的 4 个条件和 RPS 的 2 个条件全部满足：

    * 收盘价突破前 20 个交易日最高价；
    * 成交额超过 5 亿元；
    * 当日收阳且收盘价高于昨日收盘价；
    * 120 日涨幅位于全市场前 10%；
    * 收盘价处于（或接近）前 120 个交易日高点。

    所有滚动指标都先排除当日，避免把当日最高价泄漏到突破基准中。
    """

    webhook_key = "zhao_style"

    _TURTLE_PERIOD = 20
    _RPS_PERIOD = 120
    _RPS_THRESHOLD = 90
    _MIN_TURNOVER = 500_000_000  # 5 亿元，单位与 stock_daily.turnover 均为元
    _RPS_HIGH_RATIO = 0.90
    _MIN_BARS = _RPS_PERIOD + 1  # 120 日涨幅需要当日和 120 日前各一根 K 线

    def _prepare_ohlcv(self, raw: pd.DataFrame) -> pd.DataFrame:
        """复制并整理一只股票的 K 线，不修改 DataEngine 返回的原始 DataFrame。"""
        if raw is None or raw.empty:
            return pd.DataFrame()

        required = {"open", "high", "close", "turnover"}
        if not required.issubset(raw.columns):
            return pd.DataFrame()

        df = raw.copy()

        # DataEngine 已按日期排序；这里再次稳定排序，兼容测试替身和其他数据源。
        # 保留内部日期列用于横截面 RPS 的同日对齐。
        if "date" in df.columns:
            parsed_dates = pd.to_datetime(df["date"], errors="coerce")
            if parsed_dates.notna().any():
                valid_dates = parsed_dates.notna()
                df = df.loc[valid_dates].copy()
                df["_zhao_date"] = parsed_dates.loc[valid_dates]
                df = df.sort_values("_zhao_date", kind="stable")
            else:
                df["_zhao_date"] = pd.NaT
        else:
            # 没有日期的测试数据只能按传入顺序判断，假定各股票使用同一交易日。
            df["_zhao_date"] = pd.NaT

        for column in ("open", "high", "close", "turnover"):
            df[column] = pd.to_numeric(df[column], errors="coerce")

        return df.reset_index(drop=True)

    def run(self) -> list[str]:
        """遍历全市场，返回同时满足海龟和 RPS 条件的股票代码。"""
        try:
            symbols = self.engine.get_local_symbols()
        except Exception as exc:
            logger.error(f"获取本地股票列表失败：{exc}")
            return []

        # 第一阶段：逐票读取 K 线；每票内部的指标计算均为 pandas 向量化操作。
        # RPS 是横截面指标，因此先收集每只股票最新一行，再统一排名。
        records: list[dict[str, object]] = []
        for symbol in symbols:
            try:
                df = self._prepare_ohlcv(self.engine.get_ohlcv(symbol))
                if len(df) < self._MIN_BARS:
                    continue

                # shift(1) 明确排除当日，防止当前 high 参与自己的突破基准。
                df["high_20_prev"] = (
                    df["high"].shift(1).rolling(
                        self._TURTLE_PERIOD, min_periods=self._TURTLE_PERIOD
                    ).max()
                )
                df["high_120_prev"] = (
                    df["high"].shift(1).rolling(
                        self._RPS_PERIOD, min_periods=self._RPS_PERIOD
                    ).max()
                )
                df["return_120"] = df["close"].div(df["close"].shift(self._RPS_PERIOD)) - 1
                df["prev_close"] = df["close"].shift(1)

                last = df.iloc[-1]
                indicator_columns = (
                    "high_20_prev",
                    "high_120_prev",
                    "return_120",
                    "prev_close",
                    "open",
                    "close",
                    "turnover",
                )
                if last[list(indicator_columns)].isna().any():
                    continue

                # Turtle 四条件：突破、流动性、阳线、较昨日上涨。
                turtle_breakout = bool(last["close"] > last["high_20_prev"])
                liquid = bool(last["turnover"] > self._MIN_TURNOVER)
                is_yang = bool(last["close"] > last["open"])
                is_up = bool(last["close"] > last["prev_close"])

                # RPS 第二条件：靠近前 120 日高点；同样不包含当日 high。
                rps_breakout = bool(
                    last["close"] >= last["high_120_prev"] * self._RPS_HIGH_RATIO
                )

                records.append(
                    {
                        "symbol": str(symbol),
                        "date": last["_zhao_date"],
                        "return_120": last["return_120"],
                        "turtle_breakout": turtle_breakout,
                        "liquid": liquid,
                        "is_yang": is_yang,
                        "is_up": is_up,
                        "rps_breakout": rps_breakout,
                    }
                )

            except Exception as exc:
                logger.warning(f"[{symbol}] ZhaoStyleStrategy 计算失败：{exc}")
                continue

        if not records:
            logger.info("ZhaoStyleStrategy 选出 0 只股票")
            return []

        latest = pd.DataFrame.from_records(records)

        # RPS 必须在同一交易日做横截面排名。停牌股没有最新交易日数据时不参与排名。
        dated = latest["date"].notna()
        if dated.any():
            market_date = latest.loc[dated, "date"].max()
            latest = latest[latest["date"] == market_date].copy()

        # 无效的 120 日涨幅不能参与排名；正常行情下该列应全为有限数值。
        latest = latest.dropna(subset=["return_120"])
        if latest.empty:
            logger.info("ZhaoStyleStrategy 选出 0 只股票")
            return []

        # 横截面 RPS：涨幅百分位排名前 10%。不使用逐行迭代。
        latest["rps"] = latest["return_120"].rank(pct=True) * 100

        # TODO: 接入行业元数据后，对半导体、PCB、锂电、资源、工业互联板块做偏好排序/过滤。
        # TODO: 风控模块接入后，以跌破 20 日均线作为清仓信号；此处只负责入场选股。
        selected = latest[
            latest["turtle_breakout"]
            & latest["liquid"]
            & latest["is_yang"]
            & latest["is_up"]
            & (latest["rps"] >= self._RPS_THRESHOLD)
            & latest["rps_breakout"]
        ]

        candidates = selected["symbol"].drop_duplicates().tolist()
        logger.info(f"ZhaoStyleStrategy 选出 {len(candidates)} 只股票")
        return candidates
