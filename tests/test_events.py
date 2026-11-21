"""events.py 纯解析单测。

夹具来自 2026-11-21 对真实转录与真实 cc-switch.db 的实测：字段名与层级照真实结构，
只把值换成不依赖开发者机器的占位内容。 hermetic——不读 ~/.claude 或 ~/.cc-switch。
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from plugin.plugins.agent_deskmate.events import (
    ATTRIBUTION_KNOWN,
    ATTRIBUTION_UNKNOWN,
    EVENT_ERROR,
    EVENT_TOOL_USE,
    EVENT_TURN_END,
    EVIDENCE_MCP_DELEGATION,
    EVIDENCE_NONE,
    EVIDENCE_SIDECHAIN,
    EVIDENCE_USER_DECLARED,
    UnifiedEvent,
    attribute,
    check_attribution,
    delegated_server,
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


# ── 归因字段与不变式（2026-11-21 决策记录 §5） ──────────────────────────────
# 不变式：requester 非空 ⟺ requester_evidence 非空。无证据不许填。
# 这是硬约束 4「不确定的信息不许编」的机械化执行——想写"是谁让的"，
# 必须先说得出"凭什么"。


def _event(**overrides):
    base = dict(agent="claude-code", session="800257f3-aaaa", event=EVENT_TOOL_USE,
                tool="Bash", command="rm -rf build/", ts="t")
    base.update(overrides)
    return UnifiedEvent(**base)


def test_new_attribution_fields_default_to_unknown() -> None:
    """现有构造点不传归因字段时，必须落回"未知"，不能落成"已知"。"""
    event = _event()
    assert event.requester is None
    assert event.requester_evidence == EVIDENCE_NONE
    # executor 默认空串 = "与 agent 相同"；两个解析器都会显式填上它
    assert event.executor == ""
    assert event.working_directory is None
    assert event.parent_uuid is None
    assert event.tool_use_id is None
    assert event.attribution == ATTRIBUTION_UNKNOWN


def test_transcript_carries_raw_attribution_clues() -> None:
    """cwd / parentUuid / tool_use.id 都是转录里真实存在的字段，必须解析出来。"""
    record = dict(TOOL_USE_RECORD)
    record["cwd"] = r"D:\workspace\N.E.K.O"
    record["parentUuid"] = "parent-uuid-1"
    event = parse_transcript_line(json.dumps(record, ensure_ascii=False))[0]
    assert event.working_directory == r"D:\workspace\N.E.K.O"
    assert event.parent_uuid == "parent-uuid-1"
    assert event.tool_use_id == "toolu_1"
    assert event.executor == "claude-code"


def test_cwd_is_a_top_level_field_not_attachment() -> None:
    """决策记录素材里写的 attachment.environment.snapshot.workingDirectory
    在这批真实数据里不存在（实测 1816 条记录，0 条有它）；
    cwd 是顶层字段。这条测试锁住实测结论，防止又照文档猜。
    """
    record = dict(TOOL_USE_RECORD)
    record["attachment"] = {"environment": {"snapshot": {"workingDirectory": r"C:\attachment"}}}
    event = parse_transcript_line(json.dumps(record, ensure_ascii=False))[0]
    assert event.working_directory is None


def test_gateway_event_defaults_to_unknown_attribution() -> None:
    event = parse_gateway_row(dict(GATEWAY_FAIL_ROW))
    assert event.requester is None
    assert event.attribution == ATTRIBUTION_UNKNOWN
    assert event.executor == event.agent


# ── 不变式 ──────────────────────────────────────────────────────────────────

def test_requester_without_evidence_is_rejected() -> None:
    """有 requester 没证据 → 拒。这是"为了文案好看伪造发起者"的机械拦截。"""
    with pytest.raises(ValidationError) as excinfo:
        _event(requester="user")
    assert "归因不变式被破坏" in str(excinfo.value)


def test_evidence_without_requester_is_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _event(requester_evidence=EVIDENCE_SIDECHAIN)
    assert "归因不变式被破坏" in str(excinfo.value)


def test_unknown_evidence_value_is_rejected() -> None:
    """证据值必须是四个可证值之一；放"猜的"这种自由文本进来就等于开了后门。"""
    with pytest.raises(ValidationError):
        _event(requester="user", requester_evidence="猜的")


def test_invariant_holds_on_rule_hit_too() -> None:
    """不变式必须在播报层同样成立，否则会出现"事件说未知、播报说已知"。"""
    from plugin.plugins.agent_deskmate.rules import RuleHit

    with pytest.raises(ValidationError):
        RuleHit(rule="delete", severity="critical", summary="s", requester="user")


def test_attribution_is_derived_not_independent() -> None:
    """attribution 由证据推导，不许手填成另一个值——否则两边会打架。"""
    event = _event(requester="user", requester_evidence=EVIDENCE_USER_DECLARED,
                   attribution=ATTRIBUTION_UNKNOWN)   # 故意填错
    assert event.attribution == ATTRIBUTION_KNOWN     # 被纠正回推导值


def test_model_copy_keeps_attribution_in_sync() -> None:
    """model_copy 不跑 Pydantic 校验器——归因等级必须跟着证据重算。

    这条曾经是真 bug：attribute() 用 model_copy 填了 requester/evidence，
    attribution 却留在默认的 unknown，于是"已知"被显示成"未知"。
    """
    event = _event()
    copied = event.model_copy(update={"requester": "user",
                                      "requester_evidence": EVIDENCE_USER_DECLARED})
    assert copied.attribution == ATTRIBUTION_KNOWN


# ── attribute()：三条可证判据 ───────────────────────────────────────────────

def test_attribute_sidechain() -> None:
    event = attribute(_event(sidechain=True))
    assert event.requester == "session:800257f3-aaaa"
    assert event.requester_evidence == EVIDENCE_SIDECHAIN
    assert event.attribution == ATTRIBUTION_KNOWN


def test_attribute_mcp_delegation() -> None:
    event = attribute(_event(tool="mcp__deepseek-worker__deepseek_task"))
    assert event.requester == "session:800257f3-aaaa"
    assert event.requester_evidence == EVIDENCE_MCP_DELEGATION


def test_attribute_user_declared_root() -> None:
    event = attribute(_event(working_directory=r"D:\workspace\N.E.K.O\sub"),
                      mine_roots=[r"D:\workspace"])
    assert event.requester == "user"
    assert event.requester_evidence == EVIDENCE_USER_DECLARED
    assert event.attribution == ATTRIBUTION_KNOWN


def test_attribute_user_declared_is_case_insensitive_on_windows() -> None:
    r"""Windows 路径大小写不敏感；用 normcase 比，否则 D:\Workspace 匹配不上。"""
    event = attribute(_event(working_directory=r"d:\workspace\x"),
                      mine_roots=[r"D:\Workspace"])
    assert event.requester == "user"


def test_attribute_unknown_when_no_evidence() -> None:
    event = attribute(_event(working_directory=r"C:\Users\alpha\AppData\Local\Temp"))
    assert event.requester is None
    assert event.requester_evidence == EVIDENCE_NONE
    assert event.attribution == ATTRIBUTION_UNKNOWN


def test_attribute_does_not_guess_from_process_tree_or_entrypoint() -> None:
    """进程树 / entrypoint 一律不参与判定（决策记录 §5）。

    真实事故就是 DSH spawn 的 claude -p——那种会话从转录里看
    和用户手开的没有任何区别。宁可一律未知，也不要升格启发式。
    """
    event = _event(working_directory=r"C:\Users\alpha\AppData\Local\Temp")
    assert attribute(event, mine_roots=[]).attribution == ATTRIBUTION_UNKNOWN
    # 就算是"看起来像用户开的目录"，没声明也一样是未知
    assert attribute(_event(working_directory=r"D:\workspace")).attribution == ATTRIBUTION_UNKNOWN


def test_delegated_server_extraction() -> None:
    assert delegated_server("mcp__deepseek-worker__deepseek_task") == "deepseek-worker"
    assert delegated_server("mcp__hermit__run_task") == "hermit"
    assert delegated_server("Bash") is None
    assert delegated_server(None) is None
    assert delegated_server("mcp____weird") is None


def test_check_attribution_accepts_both_empty() -> None:
    check_attribution(None, EVIDENCE_NONE)   # 不抛即通过


def test_api_request_blob_tool_use_is_not_parsed_twice() -> None:
    """``api-request-blob`` 记录里带同样的 tool_use 块，是请求回显，不是第二次动作。

    实测：全部 45 个 blob 里的 mcp tool_use id 都同时出现在 assistant 记录里
    （77 个 assistant id 的超集）。这里也解析一次的话，每次 MCP 下派都会
    被记两遍——下派播报会翻倍，合并也救不回来（合并键相同，但配额会错）。
    """
    blob = {
        "type": "api-request-blob",
        "sessionId": SESSION,
        "isSidechain": False,
        "timestamp": "2026-11-21T06:00:00Z",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "mcp__deepseek-worker__deepseek_task",
             "input": {"task": "x"}},
        ]},
    }
    assert parse_transcript_line(json.dumps(blob, ensure_ascii=False)) == []
