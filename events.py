"""统一事件 schema 与纯解析函数。

设计依据：2026-11-21 对真实数据的实测（13 个转录文件、57146 行网关日志），不是照搬假设：

- ``tool_use``：``assistant`` 记录的 ``message.content[]`` 里 ``type == "tool_use"`` 的块。
  实测 689 个块，``Bash`` 357 次远超其他——L0 规则层的主要靶子。
- ``turn_end``：``assistant`` 记录的 ``message.stop_reason == "end_turn"``（实测 56 次）。
  ``stop_reason == "tool_use"`` 表示这一轮还在继续，不是结束。
- ``error``：``system`` 记录 ``level == "error"``（实测对应 ``subtype == "api_error"``，
  带 ``error`` dict 与 ``retryInMs``）。

其余 ``system`` 子类型（``turn_duration`` / ``away_summary`` / ``stop_hook_summary`` /
``compact_boundary``）暂不映射：一个事件类型只用一个信号，避免同一件事报多次。

本模块只放纯函数，不做任何 I/O，方便单测。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

# 统一事件的三种类型（任务书规定的枚举，不自创）
EVENT_TOOL_USE = "tool_use"
EVENT_TURN_END = "turn_end"
EVENT_ERROR = "error"
EVENT_TYPES = (EVENT_TOOL_USE, EVENT_TURN_END, EVENT_ERROR)

# summary 里工具输入摘要的最大长度。命令可能含敏感信息，一律截断，
# 且插件日志不得记录 summary（见 AGENTS.md「不得记录会话文本/密钥」）。
SUMMARY_INPUT_MAX = 80

AGENT_CLAUDE_CODE = "claude-code"


class UnifiedEvent(BaseModel):
    """一个观测事件。字段与任务书一致，另加两项实测必需的。"""

    agent: str = Field(description="产生事件的 Agent，如 claude-code")
    session: str = Field(description="会话标识（Claude 的 sessionId）")
    event: str = Field(description=f"事件类型：{' / '.join(EVENT_TYPES)}")
    tool: str | None = Field(default=None, description="tool_use 时的工具名，否则 null")
    summary: str = Field(default="", description="一行人类可读描述")
    ts: str = Field(description="ISO8601 UTC 时间戳")

    # 以下两项是实测后补的，不是凭空加的：
    # sidechain —— 不区分就会把子代理的工具调用记到父会话头上，归因全错
    sidechain: bool = Field(default=False, description="是否来自子代理（isSidechain）")
    # source_type —— 出问题时能回溯到是哪种记录产生的，否则只能瞎猜
    source_type: str = Field(default="", description="产生该事件的源记录 type")
    # command —— summary 被截到 80 字，删除类规则需要完整命令才不漏判；
    #            只对 Bash 的 tool_use 填充，且不进出日志（红线：不记录会话文本）
    command: str | None = Field(default=None, description="Bash 工具调用的完整命令，仅 tool_use 有")
    # status_code —— 失败类规则要按状态码定严重度，不能从 summary 文本反解析
    status_code: int | None = Field(default=None, description="网关请求的 HTTP 状态码")


# ── 时间 ───────────────────────────────────────────────────────────────────

def to_iso_utc(value: Any) -> str:
    """把转录的 ISO 串或网关的 unix 秒统一成 ISO8601 UTC 字符串。"""
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    text = str(value).strip()
    if not text:
        return ""
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


# ── 工具输入摘要 ───────────────────────────────────────────────────────────

def _clip(text: str, limit: int = SUMMARY_INPUT_MAX) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_summary(tool_name: str, tool_input: Any) -> str:
    """从工具输入里取一句人能看懂的话。

    只取各类工具最有信息量的一个字段，绝不把整个 input 倒出来（可能含密钥/长文本）。
    """
    if not isinstance(tool_input, dict):
        return tool_name
    if tool_name == "Bash":
        command = tool_input.get("command")
        if isinstance(command, str) and command.strip():
            return f"Bash: {_clip(command)}"
    for key in ("file_path", "path", "pattern", "query", "url", "prompt", "description"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return f"{tool_name}: {_clip(value)}"
    return tool_name


def _full_command(tool_name: str, tool_input: Any) -> str | None:
    """取出未经截断的完整命令。只有 Bash 有；其他工具返回 None。"""
    if tool_name != "Bash" or not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    return command if isinstance(command, str) and command.strip() else None


# ── Claude Code 转录解析 ───────────────────────────────────────────────────

def parse_transcript_line(line: str) -> list[UnifiedEvent]:
    """把一行转录 JSONL 解析成 0..n 个统一事件。纯函数，不碰文件系统。"""
    text = line.strip()
    if not text:
        return []
    try:
        record = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(record, dict):
        return []

    record_type = str(record.get("type") or "")
    session = str(record.get("sessionId") or "")
    sidechain = bool(record.get("isSidechain"))
    ts = to_iso_utc(record.get("timestamp"))
    common = {
        "agent": AGENT_CLAUDE_CODE,
        "session": session,
        "ts": ts,
        "sidechain": sidechain,
        "source_type": record_type,
    }

    message = record.get("message")
    if not isinstance(message, dict):
        message = {}

    # error：system 记录且 level == error
    if record_type == "system" and str(record.get("level") or "") == "error":
        detail = record.get("error")
        reason = ""
        if isinstance(detail, dict):
            reason = _clip(str(detail.get("message") or detail.get("type") or ""), 60)
        elif detail:
            reason = _clip(str(detail), 60)
        subtype = str(record.get("subtype") or "")
        summary = f"出错：{subtype or 'unknown'}"
        if reason:
            summary += f"（{reason}）"
        return [UnifiedEvent(event=EVENT_ERROR, tool=None, summary=summary, **common)]

    if record_type != "assistant":
        return []

    content = message.get("content")
    events: list[UnifiedEvent] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = str(block.get("name") or "")
            events.append(UnifiedEvent(
                event=EVENT_TOOL_USE,
                tool=name or None,
                summary=_tool_summary(name, block.get("input")),
                # 完整命令单独放一份：summary 截断后规则会漏判长命令里的 rm
                command=_full_command(name, block.get("input")),
                **common,
            ))

    if message.get("stop_reason") == "end_turn":
        events.append(UnifiedEvent(event=EVENT_TURN_END, tool=None,
                                   summary="这一轮说完了", **common))
    return events


# ── 网关元数据解析 ─────────────────────────────────────────────────────────

def parse_gateway_row(row: dict[str, Any]) -> UnifiedEvent | None:
    """把 ``proxy_request_logs`` 的一行转成统一事件。纯函数。

    实测：``app_type`` 是 ``claude``/``codex`` 等；``total_cost_usd`` 是 TEXT，
    必须容错解析；``session_id`` 与转录的 ``sessionId`` 是同一个 UUID，可关联。
    """
    if not isinstance(row, dict):
        return None
    status_raw = row.get("status_code")
    try:
        status = int(status_raw)
    except (TypeError, ValueError):
        status = 0

    error_message = str(row.get("error_message") or "").strip()
    is_error = status >= 400 or bool(error_message)
    event = EVENT_ERROR if is_error else EVENT_TURN_END

    app_type = str(row.get("app_type") or "").strip()
    agent = app_type or "gateway"
    model = str(row.get("model") or "").strip()

    total_cost = _to_float(row.get("total_cost_usd"))
    tokens_in = _to_int(row.get("input_tokens"))
    tokens_out = _to_int(row.get("output_tokens"))
    latency = _to_int(row.get("latency_ms"))

    bits = [f"模型 {model or '?'}"]
    if status:
        bits.append(f"状态 {status}")
    if total_cost is not None:
        bits.append(f"花费 ${total_cost:.4f}")
    if tokens_in is not None or tokens_out is not None:
        bits.append(f"token {tokens_in or 0}→{tokens_out or 0}")
    if latency is not None:
        bits.append(f"耗时 {latency}ms")
    if error_message:
        bits.append(f"错误 {_clip(error_message, 60)}")

    return UnifiedEvent(
        agent=agent,
        session=str(row.get("session_id") or ""),
        event=event,
        tool=None,
        summary="；".join(bits),
        ts=to_iso_utc(row.get("created_at")),
        sidechain=False,
        source_type="proxy_request_logs",
        status_code=status,
    )


def _to_float(value: Any) -> float | None:
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return result if result == result else None  # 排掉 NaN


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
