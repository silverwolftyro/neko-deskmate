"""adapters I/O 层单测。

全部用 tmp_path 造数据，**不碰开发者 home 目录**。重点验证两件容易错的事：
  1. 字节 offset 增量：残行不能提前消费，否则下次从半行中间开始，整行永远对不上；
  2. 网关库必须只读：读完不能冒出 -wal / -shm / -journal，那是改别人的数据库。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from plugin.plugins.agent_deskmate.adapters.claude_code import (
    ClaudeCodeAdapter,
    iter_transcript_files,
    read_new_transcript_events,
)
from plugin.plugins.agent_deskmate.adapters.gateway import (
    GatewayAdapter,
    current_watermark,
    read_new_gateway_events,
)
from plugin.plugins.agent_deskmate.events import EVENT_TOOL_USE

TOOL_LINE = json.dumps({
    "type": "assistant",
    "sessionId": "sess-1",
    "isSidechain": False,
    "timestamp": "2026-11-15T05:59:49.962Z",
    "message": {
        "role": "assistant",
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": "t", "name": "Bash",
                     "input": {"command": "ls"}}],
    },
}, ensure_ascii=False)

GATEWAY_SCHEMA = """
CREATE TABLE proxy_request_logs (
    request_id TEXT, app_type TEXT, model TEXT,
    input_tokens INTEGER, output_tokens INTEGER,
    total_cost_usd TEXT, latency_ms INTEGER,
    status_code INTEGER, error_message TEXT,
    session_id TEXT, created_at INTEGER
)
"""


def _write_jsonl(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_db(path: Path, rows: int = 3) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(GATEWAY_SCHEMA)
        for i in range(rows):
            connection.execute(
                "INSERT INTO proxy_request_logs (app_type, model, status_code, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("claude", "step-5-preview", 429 if i == 0 else 200, 1795193997 + i),
            )
        connection.commit()
    finally:
        connection.close()


# ── 转录 tail ──────────────────────────────────────────────────────────────

def test_reads_whole_file_and_returns_end_offset(tmp_path: Path) -> None:
    target = tmp_path / "a.jsonl"
    _write_jsonl(target, [TOOL_LINE, TOOL_LINE, TOOL_LINE])
    events, offset = read_new_transcript_events(target, 0)
    assert len(events) == 3
    assert all(e.event == EVENT_TOOL_USE for e in events)
    assert offset == target.stat().st_size


def test_partial_trailing_line_is_not_consumed(tmp_path: Path) -> None:
    """残行留给下一次——提前消费 会让下次从半行中间开始，整行永远对不上。

    这里用二进制写：文本模式写在 Windows 上会把 \n 变成 \r\n，
    字节账就和字符数对不上了（那正是被测代码曾经踩的坑）。
    """
    target = tmp_path / "a.jsonl"
    line = TOOL_LINE.encode("utf-8")
    target.write_bytes(line + b"\n" + line[:40])
    events, offset = read_new_transcript_events(target, 0)
    assert len(events) == 1
    assert offset == len(line) + 1

    with target.open("ab") as handle:
        handle.write(line[40:] + b"\n")
    events, offset = read_new_transcript_events(target, offset)
    assert len(events) == 1
    assert offset == target.stat().st_size


def test_offset_beyond_size_restarts(tmp_path: Path) -> None:
    """模拟轮转/截断：不能卡死在旧位置。"""
    target = tmp_path / "a.jsonl"
    _write_jsonl(target, [TOOL_LINE])
    events, offset = read_new_transcript_events(target, 999999)
    assert len(events) == 1
    assert offset == target.stat().st_size


def test_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_new_transcript_events(tmp_path / "nope.jsonl", 0) == ([], 0)


def test_bad_lines_are_skipped(tmp_path: Path) -> None:
    target = tmp_path / "a.jsonl"
    _write_jsonl(target, [TOOL_LINE, "{ 坏行", TOOL_LINE])
    events, _ = read_new_transcript_events(target, 0)
    assert len(events) == 2


def test_iter_transcript_files_only_jsonl_sorted_by_mtime(tmp_path: Path) -> None:
    _write_jsonl(tmp_path / "old.jsonl", [TOOL_LINE])
    time.sleep(0.02)
    _write_jsonl(tmp_path / "sub" / "new.jsonl", [TOOL_LINE])
    (tmp_path / "note.txt").write_text("x", encoding="utf-8")
    found = iter_transcript_files(tmp_path)
    assert [p.name for p in found] == ["new.jsonl", "old.jsonl"]


def test_iter_transcript_files_missing_dir(tmp_path: Path) -> None:
    assert iter_transcript_files(tmp_path / "nope") == []


# ── ClaudeCodeAdapter ──────────────────────────────────────────────────────

def test_adapter_first_poll_records_watermark_only(tmp_path: Path) -> None:
    """第一次见到文件只记水位，不把历史全量倒进来。"""
    _write_jsonl(tmp_path / "a.jsonl", [TOOL_LINE, TOOL_LINE])
    adapter = ClaudeCodeAdapter(tmp_path)
    assert adapter.available() is True
    assert adapter.poll() == []
    assert adapter.poll() == []


def test_adapter_picks_up_appended_lines(tmp_path: Path) -> None:
    target = tmp_path / "a.jsonl"
    _write_jsonl(target, [TOOL_LINE])
    adapter = ClaudeCodeAdapter(tmp_path)
    assert adapter.poll() == []
    with target.open("a", encoding="utf-8") as handle:
        handle.write(TOOL_LINE + "\n")
    events = adapter.poll()
    assert len(events) == 1


def test_adapter_unavailable_dir(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter(tmp_path / "nope")
    assert adapter.available() is False
    assert adapter.poll() == []


# ── 网关账本 ──────────────────────────────────────────────────────────────

def test_watermark_and_incremental_read(tmp_path: Path) -> None:
    db = tmp_path / "cc-switch.db"
    _make_db(db, rows=3)
    assert current_watermark(db) == 3

    events, watermark = read_new_gateway_events(db, 0)
    assert len(events) == 3
    assert watermark == 3
    assert read_new_gateway_events(db, 3) == ([], 3)

    connection = sqlite3.connect(db)
    with connection:
        connection.execute(
            "INSERT INTO proxy_request_logs (app_type, status_code, created_at) "
            "VALUES ('claude', 200, 1795194000)"
        )
    connection.close()
    events, watermark = read_new_gateway_events(db, watermark)
    assert len(events) == 1
    assert watermark == 4


def test_gateway_read_is_strictly_read_only(tmp_path: Path) -> None:
    """读完不许冒出 -wal / -shm / -journal——那等于改了别人的数据库。"""
    db = tmp_path / "cc-switch.db"
    _make_db(db, rows=2)
    before = {p.name for p in tmp_path.iterdir()}
    read_new_gateway_events(db, 0)
    GatewayAdapter(db).poll()
    after = {p.name for p in tmp_path.iterdir()}
    assert after == before, f"多出了 {after - before}"


def test_gateway_adapter_starts_at_end(tmp_path: Path) -> None:
    db = tmp_path / "cc-switch.db"
    _make_db(db, rows=3)
    adapter = GatewayAdapter(db)
    assert adapter.watermark == 3
    assert adapter.poll() == []

    connection = sqlite3.connect(db)
    with connection:
        connection.execute(
            "INSERT INTO proxy_request_logs (app_type, status_code, created_at) "
            "VALUES ('codex', 200, 1795194001)"
        )
    connection.close()
    assert len(adapter.poll()) == 1


def test_gateway_adapter_can_replay_history(tmp_path: Path) -> None:
    db = tmp_path / "cc-switch.db"
    _make_db(db, rows=3)
    adapter = GatewayAdapter(db, start_at_end=False)
    assert adapter.watermark == 0
    assert len(adapter.poll()) == 3


def test_gateway_missing_db(tmp_path: Path) -> None:
    db = tmp_path / "nope.db"
    assert current_watermark(db) == 0
    assert read_new_gateway_events(db, 0) == ([], 0)
    assert GatewayAdapter(db).poll() == []
