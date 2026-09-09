"""飞书多维表格推送（包装）。

main.py 通过此模块调 writer；实际实现位于 utils/feishu_bitable_writer.py。
这样 main.py 不直接依赖 utils/ 目录，保持分层整洁。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable

_PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_DIR / "utils"))

from feishu_bitable_writer import FeishuBitableWriter, get_writer  # noqa: E402

__all__ = ["append_run_to_bitable", "get_writer", "FeishuBitableWriter"]


def append_run_to_bitable(
    strategy_name: str,
    symbols: Iterable[str],
    run_date: str | None = None,
) -> tuple[int, int]:
    """把一次策略选股结果追加到飞书 bitable（流水 1 行 + 明细 N 行）。"""
    return get_writer().append_run(strategy_name, list(symbols), run_date)