"""events.py 纯解析单测。

夹具来自 2026-11-21 对真实转录与真实 cc-switch.db 的实测：字段名与层级照真实结构，
只把值换成不依赖开发者机器的占位内容。 hermetic——不读 ~/.claude 或 ~/.cc-switch。
"""

from __future__ import annotations

import json

from plugin.plugins.agent_deskmate.events import (
    EVENT_ERROR,
    EVENT_TOOL_USE,
    EVENT_TURN_END,
    parse_gateway_row,
    parse_transcript_line,
    to_iso_utc,
)

SESSION = "sess-1"

TOOL_USE_RECORD = {
    "type": "assistant",
    "sessionId": SESSION,
    "isSidechain": False,
    "timestamp": "2026-11-15T05:59:49.962Z",
    "message": {
        "role": "assistant",
        "stop_reason": "tool_use",
        "model": "step-5-preview",
        "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Bash",
             "input": {"command": "rm -rf build/"}},
        ],
    },
}

TURN_END_RECORD = {
    "type": "assistant",
    "sessionId": SESSION,
    "isSidechain": False,
    "timestamp": "2026-11-15T06:00:01.000Z",
    "message": {
        "role": "assistant",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "好的"}],
    },
}

API_ERROR_RECORD = {
    "type": "system",
    "sessionId": SESSION,
    "isSidechain": False,
    "timestamp": "2026-11-15T06:41:51.545Z",
    "subtype": "api_error",
    "level": "error",
    "error": {"message": "429 rate_limited", "status": 429},
    "retryInMs": 600,
}

SIDECHAIN_RECORD = {
    "type": "assistant",
    "sessionId": SESSION,
    "isSidechain": True,
    "timestamp": "2026-11-15T05:59:50.000Z",
    "message": {
        "role": "assistant",
        "stop_reason": "tool_use",
        "content": [
            {"type": "tool_use", "id": "toolu_2", "name": "Read",
             "input": {"file_path": "/tmp/x.py"}},
        ],
    },
}

GATEWAY_OK_ROW = {
    "app_type": "claude",
    "model": "step-5-preview",
    "request_model": "claude-sonnet-5",
    "input_tokens": 509,
    "output_tokens": 2112,
    "latency_ms": 25482,
    "status_code": 200,
    "total_cost_usd": "0.01736922",
    "session_id": "547ea690-abd2-47e3-a7f5-c8ddd0e96ccc",
    "created_at": 1795193997,
    "error_message": None,
}

GATEWAY_FAIL_ROW = dict(GATEWAY_OK_ROW, status_code=429, error_message="concurrency reached")


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


# ── 转录解析 ───────────────────────────────────────────────────────────────

def test_tool_use_record_yields_one_event() -> None:
    events = parse_transcript_line(_json(TOOL_USE_RECORD))
    assert len(events) == 1
    event = events[0]
    assert event.event == EVENT_TOOL_USE
    assert event.tool == "Bash"
    assert event.agent == "claude-code"
    assert event.session == SESSION
    assert event.sidechain is False
    assert event.summary.startswith("Bash:")
    assert "rm -rf build/" in event.summary
    assert event.source_type == "assistant"


def test_turn_end_record_yields_one_event() -> None:
    events = parse_transcript_line(_json(TURN_END_RECORD))
    assert len(events) == 1
    assert events[0].event == EVENT_TURN_END
    assert events[0].tool is None


def test_api_error_record_yields_error_event() -> None:
    events = parse_transcript_line(_json(API_ERROR_RECORD))
    assert len(events) == 1
    assert events[0].event == EVENT_ERROR
    assert "api_error" in events[0].summary
    assert events[0].source_type == "system"


def test_sidechain_flag_is_preserved() -> None:
    """不区分 sidechain 就会把子代理的调用记到父会话头上，归因全错。"""
    events = parse_transcript_line(_json(SIDECHAIN_RECORD))
    assert len(events) == 1
    assert events[0].sidechain is True


def test_malformed_lines_are_skipped_not_raised() -> None:
    for bad in ("{ 不是 json", "", "   ", "[]", "null", '"字符串"'):
        assert parse_transcript_line(bad) == [], bad


def test_summary_is_truncated() -> None:
    """命令可能极长或含敏感信息，summary 必须截断。"""
    record = {
        "type": "assistant",
        "sessionId": SESSION,
        "isSidechain": False,
        "timestamp": "2026-11-15T05:59:49.962Z",
        "message": {
            "role": "assistant",
            "stop_reason": "tool_use",
            "content": [
                {"type": "tool_use", "id": "t", "name": "Bash",
                 "input": {"command": "x" * 500}},
            ],
        },
    }
    summary = parse_transcript_line(_json(record))[0].summary
    assert summary.endswith("…")
    assert len(summary) < 100


# ── 时间归一 ───────────────────────────────────────────────────────────────

def test_to_iso_utc_normalizes_shapes() -> None:
    assert to_iso_utc(1795193997).endswith("+00:00")
    assert to_iso_utc("2026-11-15T05:59:49.962Z").endswith("+00:00")
    assert "Z" not in to_iso_utc("2026-11-15T05:59:49.962Z")
    assert to_iso_utc(None) == ""
    assert to_iso_utc("") == ""
    assert to_iso_utc("不是时间") == "不是时间"


# ── 网关解析 ───────────────────────────────────────────────────────────────

def test_gateway_ok_row_is_turn_end() -> None:
    event = parse_gateway_row(dict(GATEWAY_OK_ROW))
    assert event is not None
    assert event.event == EVENT_TURN_END
    assert event.agent == "claude"
    assert event.session == "547ea690-abd2-47e3-a7f5-c8ddd0e96ccc"
    assert "step-5-preview" in event.summary
    assert "200" in event.summary
    assert "$0.0174" in event.summary
    assert event.ts.endswith("+00:00")


def test_gateway_failure_row_is_error() -> None:
    event = parse_gateway_row(dict(GATEWAY_FAIL_ROW))
    assert event is not None
    assert event.event == EVENT_ERROR
    assert "429" in event.summary
    assert "concurrency reached" in event.summary


def test_gateway_tolerates_bad_cost() -> None:
    """total_cost_usd 在真实表里是 TEXT，空串/垃圾值都不能让解析崩。"""
    for bad in ("", "abc", None):
        event = parse_gateway_row(dict(GATEWAY_OK_ROW, total_cost_usd=bad))
        assert event is not None
        assert "$" not in event.summary


def test_gateway_tolerates_bad_status() -> None:
    for bad in (None, "x"):
        event = parse_gateway_row(dict(GATEWAY_OK_ROW, status_code=bad))
        assert event is not None
        assert event.event == EVENT_TURN_END


def test_gateway_rejects_non_dict() -> None:
    assert parse_gateway_row(None) is None
    assert parse_gateway_row("字符串") is None
