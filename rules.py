"""L0 规则层：零 key 可用的固定公式判定（M0-lite-3a）。

只做两类：**删除类**与**失败类**。这两类不依赖模型、不依赖 key，
所以无 key 时照样能报——这正是零 key 策略里"装得上、看得见规则级红灯"的那部分。

判定方式（2026-11-21 定，含一次设计返工）：
第一版按「命令位置」匹配（开头或 `; && || |` 换行 `(` `$(` 之后），
实测会**漏掉** `sudo rm`、`xargs rm`、`bash -c "rm -rf x"`、`/bin/rm`、`C:\\...\\del`
——这些前面是空格、引号或路径分隔符，都不在分隔符集合里。
改成**整 token 匹配 + 取 basename**：引号内的 `rm` 也会命中，
于是 `echo "rm -rf 是危险操作"` 会误报。这个交换是刻意的：
误报在 summary 里一眼可辨（用户能看到那是 echo），漏判却是隐形的。

严重度阶梯（失败类，我来定）：
  critical  status >= 500      服务端在崩，全线不可用
  warn      400 <= status < 500 客户端/配置问题，范围有限
  notice    status == 429       限流，Agent 会自己重试
  warn      无 status / 坏值 / 转录 error
`info` 不产生规则命中，预留给非失败的一般状态变化。
"""

from __future__ import annotations

import re
import time
from typing import Any

from pydantic import Field

from .events import (
    AttributionFields,
    UnifiedEvent,
    delegated_server,
)

SEVERITY_INFO = "info"
SEVERITY_NOTICE = "notice"
SEVERITY_WARN = "warn"
SEVERITY_CRITICAL = "critical"
SEVERITIES = (SEVERITY_INFO, SEVERITY_NOTICE, SEVERITY_WARN, SEVERITY_CRITICAL)

RULE_DELETE = "delete"
RULE_FAILURE = "failure"

# 任务书点名的六个模式。大小写不敏感（PowerShell 本身不敏感）。
DELETE_TOKENS = frozenset({"rm", "rmdir", "del", "remove-item"})
DELETE_PAIRS = (("git", "clean"), ("drop", "table"))
# 看帮助不等于真删文件；只在危险 token 是命令本身第一个 token 时豁免。
HELP_FLAGS = frozenset({"--help", "-h", "/?", "-?"})

# 分词：先按 shell 命令分隔符切成「简单命令」，再按空白与括号等切 token。
# 必须分段：否则 `rm -rf x && del --help` 里后面那个 --help 会把前面的真删除一起赦免了。
_SIMPLE_SPLIT = re.compile(r"[;|&\n]+")
_SEPARATORS = re.compile(r"[\s;|&()<>]+")


class RuleHit(AttributionFields):
    """一条规则命中的结果。coalesce_key 相同的命中在窗口内合并。

    归因字段来自 ``AttributionFields``（与 ``UnifiedEvent`` 共用同一份不变式），
    外加三个播报层字段。``attribution`` 由 requester/evidence 推导，不手填。
    """

    rule: str = Field(description="规则名：delete / failure")
    severity: str = Field(description="info / notice / warn / critical")
    summary: str = Field(description="给人看的一行描述")
    matched: str = Field(description="具体命中了什么，如 rm / git clean / 429")
    agent: str = Field(default="", description="来源 Agent")
    session: str = Field(default="", description="会话标识")
    ts: str = Field(default="", description="ISO8601 UTC")
    coalesce_key: str = Field(default="", description="合并键；相同则在窗口内只报一次")

    # 播报层归因（2026-11-21 决策记录 §5）：
    # chain_id 委托链根，**今天恒等于 session**。等将来 worker 自己的日志也可观测时，
    #   才靠它把「大脑下派」和「双手执行」并成一条播报；在那之前不要提前造关联键。
    # delegated_to 下派目标（mcp__ 的 server 名），None = 没有下派。
    chain_id: str = Field(default="", description="委托链根；今天恒等于 session")
    delegated_to: str | None = Field(default=None, description="下派目标（MCP server 名）")


def _attribution_of(event: UnifiedEvent | None) -> dict:
    """从源事件取归因字段。

    ``event`` 为 None（测试里直调纯函数）时返回空 dict —— 一切按「未知」处理。
    刻意**不传** ``attribution``：它由 requester/evidence 推导，手填就会打架。
    """
    if event is None:
        return {}
    return {
        "requester": event.requester,
        "requester_evidence": event.requester_evidence,
        "executor": event.executor,
        "working_directory": event.working_directory,
        "chain_id": event.session,
        "delegated_to": delegated_server(event.tool),
    }


# ── 分词 ──────────────────────────────────────────────────────────────────

def _tokens(command: str) -> list[str]:
    return [t for t in _SEPARATORS.split(command or "") if t]


def _simple_commands(command: str) -> list[list[str]]:
    """按 `;` `|` `&` 换行切段，每段再切 token。段内判定互不影响。"""
    commands = []
    for part in _SIMPLE_SPLIT.split(command or ""):
        tokens = [_normalise(t) for t in _tokens(part)]
        if tokens:
            commands.append(tokens)
    return commands


def _normalise(token: str) -> str:
    """去引号、转小写。引号留着会让 `"rm` 匹配不上。"""
    return token.strip("\"'`").lower()


def _basename(token: str) -> str:
    """`/bin/rm` → `rm`，`C:\\Windows\\System32\\del` → `del`。"""
    for sep in ("/", "\\"):
        if sep in token:
            token = token.rsplit(sep, 1)[-1]
    return token


# ── 删除类 ────────────────────────────────────────────────────────────────

def match_delete(command: str) -> str | None:
    """返回命中的危险 token 名，未命中返回 None。纯函数。

    按简单命令逐段判：段内第一个 token 是危险命令且带帮助 flag → 这段只是看帮助，跳过；
    其余任何位置出现危险 token（sudo/xargs 前缀、引号内、路径形式）都算命中。
    """
    for tokens in _simple_commands(command):
        for left, right in DELETE_PAIRS:
            for i in range(len(tokens) - 1):
                if tokens[i] == left and tokens[i + 1] == right:
                    return f"{left} {right}"

        if _basename(tokens[0]) in DELETE_TOKENS:
            if any(t in HELP_FLAGS for t in tokens[1:]):
                continue  # 这一段只是 `del --help` 这类看帮助
            return _basename(tokens[0])

        for token in tokens:
            name = _basename(token)
            if name in DELETE_TOKENS:
                return name
    return None


def evaluate_delete(command: str, *, agent: str = "", session: str = "",
                    ts: str = "", source: UnifiedEvent | None = None) -> RuleHit | None:
    """对一个 Bash 命令判删除类。纯函数，不碰 I/O。

    ``source`` 是产生这个命令的统一事件，只用来带归因字段；不传就是「未知」。
    """
    matched = match_delete(command)
    if matched is None:
        return None
    return RuleHit(
        rule=RULE_DELETE,
        severity=SEVERITY_CRITICAL,
        # 文案模版用「删除文件」而不是「删除类操作」——后者是内部术语，
        # 用户在 3 秒内要读懂的是"发生了什么"，不是我们的规则分类。
        summary=f"删除文件：{_clip(command)}",
        matched=matched,
        agent=agent,
        session=session,
        ts=ts,
        # 合并粒度按规则区分：删除类每次都是独立的危险动作，只合并"同一条命令重复出现"，
        # 不能按会话合并——那会让用户在一分钟内漏掉好几次不同的删除。
        # 实测依据：真实转录里一分钟内有 4 条不同 rm，按会话合并只会播 1 条。
        # 归因**不进**合并键（决策记录 §3）：合并按动作算，归因是显示层的事。
        coalesce_key=f"{RULE_DELETE}|{session}|{agent}|{matched}|{_fingerprint(command)}",
        **_attribution_of(source),
    )


# ── 失败类 ────────────────────────────────────────────────────────────────

def _to_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def severity_for_status(status_code: Any) -> str | None:
    """把 HTTP 状态码映射成严重度；2xx/3xx 返回 None（不算失败）。"""
    status = _to_int(status_code)
    if status is None:
        return SEVERITY_WARN  # 无状态码但已知失败
    if status >= 500:
        return SEVERITY_CRITICAL
    if status == 429:
        return SEVERITY_NOTICE
    if status >= 400:
        return SEVERITY_WARN
    return None


def evaluate_failure(*, status_code: Any = None, error_message: str = "",
                     agent: str = "", session: str = "", ts: str = "",
                     source: UnifiedEvent | None = None) -> RuleHit | None:
    """对一个网关请求判失败类。纯函数。

    ``status_code`` 为 None 但给了 ``error_message`` 也算失败（网络层错误没有状态码）。
    ``source`` 只用来带归因字段，不传就是「未知」。
    """
    severity = severity_for_status(status_code)
    if severity is None:
        return None  # 2xx/3xx 不是失败；有 message 但状态正常也不算失败
    message = str(error_message or "").strip()
    status = _to_int(status_code)
    matched = str(status) if status is not None else "no-status"
    summary = "请求失败"
    if status is not None:
        summary += f"（状态 {status}）"
    if message:
        summary += f"：{_clip(message, 60)}"
    return RuleHit(
        rule=RULE_FAILURE,
        severity=severity,
        summary=summary,
        matched=matched,
        agent=agent,
        session=session,
        ts=ts,
        # 失败类按 会话+状态码 合并：网关每个请求都记一行，同一会话连续 503
        # 不合并就会刷屏。这与删除类刻意不同。
        coalesce_key=f"{RULE_FAILURE}|{session}|{agent}|{matched}",
        **_attribution_of(source),
    )


def evaluate_transcript_error(event: UnifiedEvent) -> RuleHit | None:
    """转录里的 error 事件 → 失败类。纯函数。"""
    if event.event != "error":
        return None
    return RuleHit(
        rule=RULE_FAILURE,
        severity=SEVERITY_WARN,
        summary=f"转录报错：{_clip(event.summary, 80)}",
        matched="transcript-error",
        agent=event.agent,
        session=event.session,
        ts=event.ts,
        coalesce_key=f"{RULE_FAILURE}|{event.session}|{event.agent}|transcript-error",
        # 事件本身就是 source：归因字段直接从它带过来
        **_attribution_of(event),
    )


def _fingerprint(text: str) -> str:
    """命令的短指纹，用于"同一条命令重复出现才合并"。"""
    import hashlib

    return hashlib.sha1(" ".join(str(text).split()).encode("utf-8")).hexdigest()[:12]


def _clip(text: str, limit: int = 80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── 合并 ──────────────────────────────────────────────────────────────────

class Coalescer:
    """同一 coalesce_key 在窗口内只放行第一条，避免刷屏。"""

    def __init__(self, window_seconds: float = 60.0) -> None:
        self.window_seconds = float(window_seconds)
        self._last: dict[str, float] = {}

    def offer(self, hit: RuleHit, now: float | None = None) -> RuleHit | None:
        """窗口内重复返回 None（被合并），否则原样返回并记账。"""
        moment = time.monotonic() if now is None else float(now)
        previous = self._last.get(hit.coalesce_key)
        if previous is not None and moment - previous < self.window_seconds:
            return None
        self._last[hit.coalesce_key] = moment
        return hit

    def evaluate_event(self, event: UnifiedEvent, now: float | None = None) -> list[RuleHit]:
        """对一个统一事件跑当前阶段所有规则，返回未被合并的命中。"""
        hits: list[RuleHit] = []
        if event.event == "error":
            candidate = evaluate_transcript_error(event)
            if candidate is not None:
                kept = self.offer(candidate, now)
                if kept is not None:
                    hits.append(kept)
        return hits
