"""cc-switch 网关元数据适配器（只读）。

数据源：``~/.cc-switch/cc-switch.db`` 的 ``proxy_request_logs`` 表，按 ``rowid`` 水位取增量。
实测（2026-11-21）：57146 行、max(rowid)=76129（rowid 有空洞，说明有删除），
28 列。关键列：``app_type``（claude/codex…）、``model``、``total_cost_usd``（**TEXT**，
必须容错转 float）、``status_code``、``latency_ms``、``created_at``（unix 秒）、
``session_id``（**与转录的 sessionId 是同一个 UUID**，可关联）。

已知限制（任务书已注明，别当新发现）：这张表**请求结束后才写入**，延迟 <1 秒，
没有 in-flight 行 → 只能做事后账本，不能报"正在请求"。

铁律：**只读打开**（``mode=ro``）。写模式会产生 -wal/-shm 文件，
那是改别人的数据库，绝对不行。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ..events import UnifiedEvent, parse_gateway_row

_TABLE = "proxy_request_logs"


def read_new_gateway_events(db_path: Any, watermark: int = 0,
                            limit: int = 500) -> tuple[list[UnifiedEvent], int]:
    """按 ``rowid`` 水位读增量，返回 ``(事件, 新水位)``。

    ``limit`` 防止第一次就吞掉几万行——水位从 0 起跑时分批拉，
    但调用方通常会给一个「已初始化」的水位（见 ``GatewayAdapter``）。
    """
    path = Path(db_path)
    if not path.is_file():
        return [], watermark

    events: list[UnifiedEvent] = []
    new_watermark = watermark
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        cursor = connection.execute(
            f"SELECT rowid AS _rowid, * FROM {_TABLE} WHERE rowid > ? ORDER BY rowid LIMIT ?",
            (watermark, limit),
        )
        for row in cursor:
            data = dict(row)
            new_watermark = max(new_watermark, int(data.pop("_rowid")))
            event = parse_gateway_row(data)
            if event is not None:
                events.append(event)
    finally:
        connection.close()
    return events, new_watermark


def current_watermark(db_path: Any) -> int:
    """取当前最大 rowid，用于「从现在开始看」而不是把历史全量倒进来。"""
    path = Path(db_path)
    if not path.is_file():
        return 0
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        row = connection.execute(f"SELECT MAX(rowid) FROM {_TABLE}").fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    finally:
        connection.close()


class GatewayAdapter:
    """持水位位的网关账本读取器。水位在内存里，重启后从当前 max(rowid) 开始。"""

    def __init__(self, db_path: Any, *, start_at_end: bool = True) -> None:
        self.db_path = Path(db_path)
        self._watermark = current_watermark(self.db_path) if start_at_end else 0

    def available(self) -> bool:
        return self.db_path.is_file()

    @property
    def watermark(self) -> int:
        return self._watermark

    def poll(self, limit: int = 500) -> list[UnifiedEvent]:
        events, self._watermark = read_new_gateway_events(self.db_path, self._watermark, limit)
        return events
