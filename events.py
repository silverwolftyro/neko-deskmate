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

``api-request-blob`` 也不映射：实测它带的 tool_use 块与 ``assistant`` 记录**完全重复**
（45 个 blob id 全是 77 个 assistant id 的子集），是请求回而不是第二次动作。
这里也解析的话每次 MCP 下派都会被记两遍——有单测守着。

本模块只放纯函数，不做任何 I/O，方便单测。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Iterable

from pydantic import BaseModel, Field, model_validator

# 统一事件的三种类型（任务书规定的枚举，不自创）
EVENT_TOOL_USE = "tool_use"
EVENT_TURN_END = "turn_end"
EVENT_ERROR = "error"
EVENT_TYPES = (EVENT_TOOL_USE, EVENT_TURN_END, EVENT_ERROR)

# summary 里工具输入摘要的最大长度。命令可能含敏感信息，一律截断，
# 且插件日志不得记录 summary（见 AGENTS.md「不得记录会话文本/密钥」）。
SUMMARY_INPUT_MAX = 80

AGENT_CLAUDE_CODE = "claude-code"

# ── 归因（2026-11-21 决策记录 §1/§5） ───────────────────────────────────────
# 核心原则：主语位只放「可证的请求源」；执行者降级到括号里；拿不准就写未知。
# 用户会把播报的主语读成「发起者」，所以主语位一旦放执行者，就等于把锅扣给手。

ATTRIBUTION_KNOWN = "known"
ATTRIBUTION_UNKNOWN = "unknown"
ATTRIBUTIONS = (ATTRIBUTION_KNOWN, ATTRIBUTION_UNKNOWN)

# requester_evidence 的可选值。**只有这四个**——加新的必须先有可证判据。
EVIDENCE_NONE = ""
EVIDENCE_SIDECHAIN = "sidechain"            # isSidechain：执行者是子代理
EVIDENCE_MCP_DELEGATION = "mcp-delegation"  # tool 以 mcp__ 开头：本会话是大脑
EVIDENCE_USER_DECLARED = "user-declared"    # cwd 落在用户声明的 roots 里
EVIDENCES = (EVIDENCE_NONE, EVIDENCE_SIDECHAIN, EVIDENCE_MCP_DELEGATION,
             EVIDENCE_USER_DECLARED)

# requester 的取值。session:<sid> 表示"这个会话（或其父级）"，user 表示用户本人。
REQUESTER_USER = "user"

# MCP 工具名形如 mcp__<server>__<tool>。只做字符串切分，不猜它要干什么。
MCP_PREFIX = "mcp__"


def check_attribution(requester: str | None, requester_evidence: str) -> None:
    """归因不变式：**requester 非空 ⟺ requester_evidence 非空**。

    无证据不许填 requester。这条是硬约束 4（不确定的信息不许编）的机械化执行：
    想写"是谁让的"，必须先说得出"凭什么"。
    """
    if bool(requester) != bool(requester_evidence):
        raise ValueError(
            "归因不变式被破坏：requester="
            f"{requester!r} 与 requester_evidence={requester_evidence!r} "
            "必须同时为空或同时非空（无证据不许填 requester）"
        )
    if requester_evidence and requester_evidence not in EVIDENCES:
        raise ValueError(
            f"未知的归因证据 {requester_evidence!r}；可用值：{EVIDENCES}"
        )


def attribution_of(requester: str | None, requester_evidence: str) -> str:
    """有证据才算 known。归因等级由证据推导，不是独立数据——写不歪。"""
    return ATTRIBUTION_KNOWN if (requester and requester_evidence) else ATTRIBUTION_UNKNOWN


def delegated_server(tool_name: str | None) -> str | None:
    """从 MCP 工具名取 server 名：``mcp__deepseek-worker__deepseek_task`` → ``deepseek-worker``。

    非 MCP 工具返回 None。只切字符串，不猜这个 worker 会干什么。
    """
    name = str(tool_name or "")
    if not name.startswith(MCP_PREFIX):
        return None
    parts = name.split("__")
    if len(parts) >= 3 and parts[1]:
        return parts[1]
    return None


class AttributionFields(BaseModel):
    """归因字段 + 不变式。``UnifiedEvent`` 与 ``RuleHit`` 共用同一份。

    放基类里而不是各写一遍：不变式必须在"事件"和"播报"两侧同时成立，
    否则会出现「事件说未知、播报说已知」这种自相矛盾的输出。
    """

    requester: str | None = Field(
        default=None, description="可证的请求源；None = 不可证（不许猜）")
    requester_evidence: str = Field(
        default=EVIDENCE_NONE, description="凭什么这么判；空 = 无证据")
    executor: str = Field(
        default="", description="实际执行层；空 = 与 agent 相同")
    working_directory: str | None = Field(
        default=None, description="会话工作目录；定位线索，不是归因依据")
    attribution: str = Field(
        default=ATTRIBUTION_UNKNOWN, description="known / unknown，由上面两者推导")

    @model_validator(mode="after")
    def _attribution_invariant(self) -> "AttributionFields":
        check_attribution(self.requester, self.requester_evidence)
        # attribution 不是独立数据：从证据推导，防止两边说漏嘴不一致。
        self.attribution = attribution_of(self.requester, self.requester_evidence)
        return self

    def model_copy(self, *, update: dict[str, Any] | None = None,
                   **kwargs: Any) -> "AttributionFields":
        """复制之后重算归因等级。

        Pydantic 的 ``model_copy`` **不跑校验器**。普通复制会让
        「requester 有值、attribution 却还是 unknown」这种自相矛盾的状态悄悄存在——
        而归因这件事最不该出的就是自相矛盾（用户刚被"到底是谁让的"搞糊涂过一次）。
        所以在基类里统一补上，而不是指望每个调用点都记得手写 attribution。
        """
        copied = super().model_copy(update=update, **kwargs)
        check_attribution(copied.requester, copied.requester_evidence)
        copied.attribution = attribution_of(copied.requester, copied.requester_evidence)
        return copied


def _under_any(cwd: str, roots: Iterable[Any]) -> bool:
    """cwd 是否落在某个声明的根目录下（Windows 大小写不敏感）。"""
    target = os.path.normcase(os.path.abspath(cwd))
    for root in roots:
        base = os.path.normcase(os.path.abspath(str(root)))
        if target == base or target.startswith(base + os.sep):
            return True
    return False


def attribute(event: "UnifiedEvent", *, mine_roots: Iterable[Any] = ()) -> "UnifiedEvent":
    """按**可证**证据填 requester / requester_evidence。纯函数，返回新事件。

    判据只有三条，全部可证（决策记录 §1/§2）：
      1. ``isSidechain``        → 执行者是子代理，请求源是本会话的父级；
      2. ``tool`` 以 ``mcp__`` 开头 → 本会话是大脑，下派给了外部 worker；
      3. cwd 落在用户声明的 roots 里 → 用户自己开的会话（**用户给的事实，不是猜测**）。
    三条都不中 → ``requester=None``（未知）。

    **不参与判定的**（决策记录 §5，别再升格成依据）：
    进程树（Windows 上父进程可能已退出、中间常隔 shell/node）、
    ``entrypoint``（``sdk-cli`` 是否等于"非用户启动"未经证实）。
    这两个只进日志供人肉排查。
    """
    if event.sidechain and event.session:
        requester: str | None = f"session:{event.session}"
        evidence = EVIDENCE_SIDECHAIN
    elif delegated_server(event.tool):
        requester = f"session:{event.session}" if event.session else None
        evidence = EVIDENCE_MCP_DELEGATION if requester else EVIDENCE_NONE
    elif event.working_directory and _under_any(event.working_directory, mine_roots):
        requester = REQUESTER_USER
        evidence = EVIDENCE_USER_DECLARED
    else:
        requester = None
        evidence = EVIDENCE_NONE
    return event.model_copy(update={
        "requester": requester,
        "requester_evidence": evidence,
    })


class UnifiedEvent(AttributionFields):
    """一个观测事件。字段与任务书一致，另加实测必需的与归因必需的两组。"""

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

    # 归因用的原始线索（2026-11-21）。都是转录里真实存在的字段，不是推测：
    # parent_uuid / tool_use_id —— 与 tool_result 配对，确认动作真的执行了
    parent_uuid: str | None = Field(default=None, description="转录记录的 parentUuid")
    tool_use_id: str | None = Field(default=None, description="tool_use 块的 id")


# ── 时间 ───────────────────────────────────────────────────────────────────

def _clean(value: Any) -> str | None:
    """转录字段的通用清洗：非字符串/空串一律返回 None，不把 ``None`` 当字符串存。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None

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
        # executor 现在就等于 agent；等将来要区分"判断的人"和"动手的人"时才有别。
        # 留成空串会让播报层分不清"没填"和"就是 agent"，所以这里显式填。
        "executor": AGENT_CLAUDE_CODE,
        # cwd 是顶层字段（实测 1816 条记录都有）。
        # 决策记录素材里写的 attachment.environment.snapshot.workingDirectory
        # 在这批数据里不存在——按实测用 cwd。
        "working_directory": _clean(record.get("cwd")),
        "parent_uuid": _clean(record.get("parentUuid")),
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
                tool_use_id=_clean(block.get("id")),
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
        executor=agent,
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
