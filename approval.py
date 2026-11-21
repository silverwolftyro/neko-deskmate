"""M0-lite-5 审批中继：Claude Code PermissionRequest hook → 本地 HTTP → 卡片 → 回填。

**核心原则（2026-11-21 修订）：插件永不代替用户做判断，除非用户显式授权。**

三层门控（规格《审批自动化-授权层级》§1）：

* **L0 转发**——无任何配置即可用：只转发审批、常驻提醒未决，不解释不判断；
* **L1 建议**——``ai_judgement = true`` **且** ``api_key`` 非空：给出
  「建议 + 理由 + 依据」并摆在卡片上，仍然等用户点；
* **L2 自动**——L1 两条 + ``auto_decide = "low_risk_only"`` + 风险 ≤ 阈值：
  在低风险动作上替用户点，并留可审计记录。

**任何一层都不能跳级**：L2 必须建立在 L1 之上——没解释能力的自动决定等于黑箱代办。
删除类/危险写**永远不进 L2**，无论阈值怎么设。

到期（``timeout_mark_expired``，默认 570s = hook 600s 前 30s）时：
用户没点 → **只把 pending 标成 ``expired``**——不拒绝、不回填、不记成"已拒绝"，
响应体里不得出现任何 decision。用 503 + 无 decision 的响应体告诉宿主"插件没有
决定"；真正拦住这次调用的是宿主自己的策略（``hooks.json`` 里的
``onFailure:"block"``）。文案只写「宿主已自行处理，本插件未做判断」。

**L2 自动放行的时机是事件发生的那一刻，不是等到期。** 四条全中才自动 allow：
用户已在 ``approval_authorize`` 确认授权文案、配置里 L1/L2 全开、风险 ≤ 阈值、
判断后端就绪。自动放行后立刻发一条 blind 通知 + 写一条含授权人/规则/模型/理由的
审计。其余情况一律不自动决定——**包括后端说 deny**（永不 auto-deny）、后端异常、
没授权、没 key。没接后端时也不自己拍一个，把请求留给用户，走到期。

这是三条红线里第 2 条的执行体：**审批交互只能走 create_card + data-neko-action**。

设计要点（都来自官方资料或踩过的坑，不是拍的）：

1. **HTTP 服务跑在守护线程里**，不是 startup 里的 asyncio 任务。
   AGENTS.md 明确写着"startup 返回时它创建的任务会被取消"——任务活不到
   用户点按钮那一刻，线程可以。
2. **回填靠"还挂着的那条连接"，不靠 chain_id 推断身份。** hook 是"挂起等响应"
   语义：handler 线程一直阻塞到用户决定或到期，身份由连接本身保证。
   所以 chain_id 只用于显示，不参与回填（2026-11-21 归因结论 2）。
3. **pending 键是 (session_id, request_id)，request_id 由我们生成。**
   sessionId 在父子代理之间是同一个值（实测：子代理记录的 sessionId 恒等于父会话），
   拿它区分不了父子；但回填不需要区分，所以不影响正确性。
4. **pending 到期不删**，标 ``expired`` 后按 ``retention_seconds`` 保留，
   以便事后查"当时到底问过什么"。
5. **hooks.json 必须带 ``onFailure:"block"``。** 否则插件没起来、返回非 2xx
   或超时，Claude Code 按"非阻断错误"处理 → **直接放行**。这是 fail-open，
   对审批是灾难。我们到期时正是靠它把"没有决定"变成"拦住"。

官方回填格式（03-调研证据/06 §3.3）：::

    {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                            "decision": {"behavior": "allow"|"deny",
                                         "updatedInput": {}, "updatedPermissions": []}}}

deny 可另带 ``message`` 与 ``interrupt``。``interrupt`` 默认 False——
拒绝这一次调用就好，不要顺手掐掉整个回合（那会让 Claude 中途闭嘴）。

本模块**不 import 任何 SDK**：状态机与 HTTP 服务是纯标准库，
才能在没宿主的环境里单测（允许/拒绝/到期/连接断开/无会话/门控）。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from .commands import (
    CONCLUSION_UNSURE,
    CONCLUSIONS,
    card_lines,
)

# 决策取值。只有用户点按钮、或 L2 在用户显式授权下自动放行，才能产生这两个值。
APPROVE_ALLOW = "allow"
APPROVE_DENY = "deny"
DECISIONS = (APPROVE_ALLOW, APPROVE_DENY)

# decided_by 的取值。
DECIDED_BY_USER = "user"                # 用户点了 → 回填
DECIDED_BY_EXPIRED = "expired"          # 我们先到期 → **不回填、不记成拒绝**
DECIDED_BY_HOST_TIMEOUT = "host_timeout"  # 连接被宿主断开 → **不回填**
DECIDED_BY_AI = "ai"                    # L2 自动放行 → 回填，但必须可审计
# 只出现在 kind="authorization" 的审计行上：那是"谁确认了授权"，
# 不是一次审批决定。决定类的 decided_by 永远只有上面四个值。
DECIDED_BY_AUTHORIZATION = "authorization"

# ── 显式授权（规格 §2 硬规则 2） ────────────────────────────────────────────
# ai_judgement / auto_decide 只能由用户写配置打开；但**光写配置还不够**——
# 首次开启前必须先让用户确认授权文案。确认记录写进审计（时间、范围、授权版本）。
# 插件没有任何方法能在运行期自行打开授权。

AUTHORIZATION_VERSION = 1
AUTH_SCOPE_ADVICE = "ai_judgement"
AUTH_SCOPE_AUTO = "auto_decide"
AUTH_SCOPES = (AUTH_SCOPE_ADVICE, AUTH_SCOPE_AUTO)

AUTHORIZATION_TITLE = "同桌猫 · 授权确认"
# 授权文案。必须包含"模型判断可能出错"与"决定由你授权并承担后果"——
# 这是用户签字前该看到的话，测试逐句盯着。
AUTHORIZATION_TEXT = (
    "开启后，插件会把「命令原文、工作目录、命中的 L0 规则、会话标识」"
    "交给你的配置里指定的模型，由它给出建议（ai_judgement），"
    "或在你授权的低风险动作上替你放行（auto_decide）。"
)
AUTHORIZATION_HEADLINE = "模型判断可能出错，决定由你授权并承担后果"
AUTHORIZATION_FOOTER = (
    "插件不会在运行时自行开启以上任何一项；想关闭只需把配置改回 "
    "false / off。确认记录会写进审批审计（含时间、范围、授权版本）。"
)

HOOK_PATH = "/hook/permission"
# 探活端点。中继转发之前先打这里；插件没起来时中继据此交还原生权限流程。
HEALTH_PATH = "/health"
MAX_BODY_BYTES = 256 * 1024

# 默认到期时间。必须**明显小于** hooks.json 里的 hook timeout（600s），
# 否则我们还没到期宿主就先断了。留 30 秒余量。
DEFAULT_TIMEOUT_SECONDS = 570.0
# 到期后的记录保留多久，供 pending_list 事后查。
DEFAULT_RETENTION_SECONDS = 3600.0
# 审计记录的内存条数上限。超了就丢最旧的——内存不能无限涨。
AUDIT_MEMORY_LIMIT = 500

# ── 三层门控（规格 §1） ────────────────────────────────────────────────────

AUTO_OFF = "off"
AUTO_LOW_RISK_ONLY = "low_risk_only"
AUTO_DECIDE_VALUES = (AUTO_OFF, AUTO_LOW_RISK_ONLY)

# ── 动作类别：不可恢复 vs 可恢复 ────────────────────────────────────────────
# 闸门的排除判据是**动作类别**，不是规则名（规格 §16.3 C3）。
#
# 为什么不能按规则名排：规则名会随规则库增删而变——C1 之后"删除类"很可能拆成
# disk_root / system_dir / shadow_copy 好几条。靠 `rule == "delete"` 排除，
# 就等于每加一条规则都得回来改一遍门控；漏改的那条，配上
# `auto_decide_max_severity = "critical"` 就被 L2 自动放行了。按类别排，
# 新规则只要声明自己"不可恢复"就自动继承排除，不需要有人记得改门控。
ACTION_CLASS_RECOVERABLE = "recoverable"      # 可恢复：删了重建得回来
ACTION_CLASS_IRREVERSIBLE = "irreversible"    # 不可恢复：删盘 / 格式化 / 引导项…
# 只列"不可恢复"这一类。**未判定时按可恢复算**——宁可漏，不可错杀，
# 与闸门"精度优先"同向；这也意味着将来新规则要显式声明自己不可恢复。
IRREVERSIBLE_CLASSES = frozenset({ACTION_CLASS_IRREVERSIBLE})
DEFAULT_ACTION_CLASS = ACTION_CLASS_RECOVERABLE

# 严重度阶梯。与 rules.py 的取值一致，这里只用来比大小。
SEVERITY_ORDER = {"info": 0, "notice": 1, "warn": 2, "critical": 3}

# **极高危档本身不可被自动放行**（规格 §3 表：极高危"任何配置不得自动放行"）。
# 这是独立于动作类别的第二道墙：万一将来某条规则忘了声明类别，
# severity=critical 也照样挡住。于是 auto_decide_max_severity 这个旋钮
# 最高只能开到 warn——开到 critical 会被压回 warn 并在启动日志里说明。
AUTO_DECIDE_MAX_SEVERITY_CEILING = "warn"
DEFAULT_MAX_SEVERITY = "notice"

# 风险判定用的默认档：L0 规则没命中就当低风险。
DEFAULT_RISK_SEVERITY = "notice"


def assess_risk(tool_name: str, tool_input: Any) -> tuple[str, str, str]:
    """评估一次工具调用。纯函数，不接模型。

    返回 ``(severity, rule, action_class)``。5a 阶段只跑 L0 规则（删除类）——
    没命中就按 ``notice``（低风险）算，**不猜**。模型判定是 5b 的事。

    ``action_class`` 是**独立于规则名**的一维：规则名会随规则库变，类别不会。
    C1 把闸门规则重写成精度优先之后，这里会多返回几条规则，但门控只认类别，
    不需要跟着改（规格 §16.3 C3）。

    刻意不看 ``tool_input`` 里除命令以外的字段：猜动作是归因错的高发区。
    """
    command = ""
    if isinstance(tool_input, dict):
        raw = tool_input.get("command")
        if isinstance(raw, str):
            command = raw
    if tool_name == "Bash" and command:
        from .rules import match_delete

        if match_delete(command):
            return "critical", "delete", ACTION_CLASS_IRREVERSIBLE
    return DEFAULT_RISK_SEVERITY, "", DEFAULT_ACTION_CLASS


def effective_max_severity(configured: str) -> str:
    """把配置里的 L2 阈值夹到合法上限。纯函数，便于单测。

    极高危档不可被自动放行（规格 §3），所以阈值开到 ``critical`` 是没有意义的——
    它要么是用户没理解这个旋钮管什么，要么是想绕过极高危墙。压回 ``warn``
    并让调用方打日志，比默默接受一个"看起来开了其实开不动"的配置好。
    """
    wanted = str(configured or "").strip().lower()
    if wanted not in SEVERITY_ORDER:
        return DEFAULT_MAX_SEVERITY
    if SEVERITY_ORDER[wanted] > SEVERITY_ORDER[AUTO_DECIDE_MAX_SEVERITY_CEILING]:
        return AUTO_DECIDE_MAX_SEVERITY_CEILING
    return wanted


DEFAULT_MAX_SEVERITY = "notice"


def auto_decide_gate(*, severity: str, ai_judgement: bool,
                     has_key: bool, auto_decide: str,
                     max_severity: str,
                     authorized_scopes: tuple[str, ...] = (),
                     action_class: str = DEFAULT_ACTION_CLASS) -> tuple[bool, str]:
    """L2 门控。返回 ``(是否放行, 原因)``。纯函数，便于单测。

    六条全中才放行，缺任何一条都给出**具体原因**——原因要能写进审计，
    不能只回一个 False 让人猜。授权检查排在 ai_judgement 之后、key 之前：
    配置说"要开"只是意愿，用户在授权入口签了字才算数。

    排除判据按**动作类别**与**极高危档**，不按规则名（规格 §16.3 C3）：
    规则名会随规则库增删而变，靠它排除就意味着每加一条规则都得回来改门控。
    所以这个函数**刻意不收 ``rule`` 参数**——收了就会有人顺着它写规则名判断。
    """
    if auto_decide != AUTO_LOW_RISK_ONLY:
        return False, f"auto_decide={auto_decide!r} 未开启"
    if not ai_judgement:
        # 不能跳级：没开 L1 就没有解释能力，此时的自动决定是黑箱代办。
        return False, "ai_judgement 未开启（L2 必须建立在 L1 之上）"
    if AUTH_SCOPE_ADVICE not in authorized_scopes:
        return False, "未确认 ai_judgement 授权（见 approval_authorize）"
    if AUTH_SCOPE_AUTO not in authorized_scopes:
        return False, "未确认 auto_decide 授权（见 approval_authorize）"
    if not has_key:
        return False, "未配置 api_key（没 key 不判断）"
    # 第一道墙：动作类别。不可恢复的一律排除，阈值开到顶也一样。
    if action_class in IRREVERSIBLE_CLASSES:
        return False, f"{action_class} 类动作永不进入 L2（无论阈值怎么设）"
    # 第二道墙：极高危档。独立于类别，防将来某条规则忘了声明类别。
    if SEVERITY_ORDER.get(severity, 99) >= SEVERITY_ORDER["critical"]:
        return False, f"风险 {severity} 属极高危档，任何配置不得自动放行"
    if SEVERITY_ORDER.get(severity, 99) > SEVERITY_ORDER.get(max_severity, 0):
        return False, f"风险 {severity} 超过阈值 {max_severity}，回落为 L1"
    return True, "L2 门控通过"


def advice_state(*, ai_judgement: bool, has_key: bool, backend_ready: bool,
                 authorized: bool = True) -> str:
    """卡片上"建议"那块显示成什么样。四种状态，都不许假装成功。

    ``authorized`` 单独一档：配置开了但用户没签字时，卡片必须说"未确认授权"，
    不能显示成"没有 key"——那是两件事，混起来用户不知道该去哪补。
    """
    if not ai_judgement:
        return ADVICE_STATE_FALLBACK
    if not authorized:
        return "unauthorized"
    if not has_key:
        return "no_key"
    if not backend_ready:
        return "pending_backend"
    return ADVICE_STATE_READY


ADVICE_TEXT = {
    # 全部是**陈述句**：只说现在是什么状态，不指挥用户去配什么。
    # 规格 §16.1「覆盖 §12.1」三条硬要求：
    #   1. 严禁「填写 API key 即可…」这类索取式文案；
    #   2. 不准暗示零 key 时闸门不工作——安全兜底与模型无关；
    #   3. 不许把配置键名（ai_judgement）或内部入口名（approval_authorize）
    #      泄到用户界面，用户不知道那是什么（这是 698d4a9 的 C5 缺陷）。
    "disabled": "当前只做提醒，未接入模型。安全兜底与模型无关。",
    "unauthorized": "尚未确认授权，暂不给出建议。安全兜底与模型无关。",
    "no_key": "当前只做提醒，未接入模型。安全兜底与模型无关。",
    "pending_backend": "当前只做提醒，模型能力尚未接入。安全兜底与模型无关。",
}

# ── 卡片侧「建议块」的唯一词表 ────────────────────────────────────────────
# **只有本模块拥有这套名字。** model_advice.py 里的 STATE_* 是"调用结果码"
# （ok / timeout / auth_failed…），那是模型客户端自己的事；两套词曾经有重叠
# 又不完全重叠（no_key / disabled / unauthorized 撞名，其余不撞），结果
# _advice_for_card 把调用结果码当成展示状态直接传进 build_card_html，
# 模型一成功就 ADVICE_TEXT["ok"] → KeyError → 整张卡片崩掉（真机实测）。
#
# 修法不是"让卡片多认一个 ok"——那只会把词表改大，下一个人再加第三种。
# 修法是：展示状态只在这里定义，_advice_for_card 是唯一的翻译点，
# 并且它翻译出来的东西必须落在这三个名字里，否则按未知处理。
ADVICE_STATE_READY = "ready"
# 未知状态的兜底文案。取 disabled：它是一句纯粹的"现在没有建议"，
# 不指向任何需要用户去补的动作，也不会暗示闸门不工作。
ADVICE_STATE_FALLBACK = "disabled"
# 合法的展示状态集合。_advice_for_card 与 build_card_html 都拿它自检。
# **必须含 ADVICE_STATE_READY**：ready 态不渲染 ADVICE_TEXT 里的句子，
# 它渲染「建议 + 理由 + 依据」三行，所以它不在 ADVICE_TEXT 的键里——
# 只拿 ADVICE_TEXT 的键当合法集合，就会把 ready 误判成未知状态，
# 模型成功给出建议时反而降级成"没有建议"（写这一段时真犯过，被测试抓住）。
ADVICE_STATES = frozenset(ADVICE_TEXT) | {ADVICE_STATE_READY}


def advice_text(state: str) -> str:
    """取某个展示状态对应的文案；**未知状态不抛异常**。

    建议是锦上添花，卡片是核心功能。建议出任何问题都不能让卡片不出现——
    所以这里永远有值可返回，调用方不需要 try。
    """
    return ADVICE_TEXT.get(state, ADVICE_TEXT[ADVICE_STATE_FALLBACK])


def is_known_advice_state(state: Any) -> bool:
    return state in ADVICE_STATES


class ApprovalRequest(BaseModel):
    """一次审批请求。request_id 由我们生成，不用 Claude 的 tool_use_id——
    那个 id 在父子代理间会重复，而我们可能要同时挂多个请求。"""

    session_id: str = Field(default="", description="Claude Code 的 session_id")
    request_id: str = Field(description="我们生成的请求 id")
    tool_name: str = Field(default="", description="要执行的工具名")
    tool_summary: str = Field(default="", description="工具调用的一行摘要（给人看）")
    working_directory: str | None = Field(default=None, description="会话 cwd")
    agent: str = Field(default="claude-code", description="来源 Agent")
    received_at: float = Field(default_factory=time.time)


class PendingApproval:
    """一条审批记录。

    ``decided_by`` 为 None 表示还没决定（pending）；有值表示已了结。
    了结不等于删除——记录按 ``retention_seconds`` 保留，供事后查。

    ``auto`` 为 True 表示这条是 L2 在用户授权下自动放行的：它**不该弹卡片**
    （自动放行还要人点就失去意义），只要一条 blind 通知 + 一条审计。
    """

    def __init__(self, request: ApprovalRequest) -> None:
        self.request = request
        self.event = threading.Event()
        self.decision: str | None = None       # allow / deny，仅用户或 L2 能填
        self.decided_by: str | None = None
        self.card_id: str | None = None
        self.card_announced = False
        # 风险、规则命中与**动作类别**：建请求时就由 L0 定好。
        # 类别是独立于规则名的一维，门控与红条都只认它（规格 §16.3 C3）。
        self.severity: str = DEFAULT_RISK_SEVERITY
        self.rule: str = ""
        self.action_class: str = DEFAULT_ACTION_CLASS
        # L2 决定时填。
        self.judge_model: str = ""
        self.judge_reason: str = ""
        # 生效中的授权（"v1:ai_judgement,auto_decide by=user"）——L2 审计要能
        # 追溯到"是谁授权的"，不能只写"AI 决定的"。
        self.authorization: str = ""
        # L2 自动放行标记 + 通知是否已发。
        self.auto = False
        self.auto_announced = False
        # 已经进内存的审计行。投递失败时要把它改成最终状态（见 note_host_timeout）。
        self.audit_entry: AuditRecord | None = None
        # 这一条拿到的模型建议。**只给「这是什么」卡用**——主卡只显示结论行，
        # 理由与依据按规格 P3 折叠进解释卡，不让它们干扰扫读。
        self.advice: dict[str, str] = {}

    @property
    def key(self) -> tuple[str, str]:
        return (self.request.session_id, self.request.request_id)

    def is_pending(self) -> bool:
        return self.decided_by is None

    def settled(self) -> bool:
        return self.decided_by is not None

    def needs_card(self) -> bool:
        return self.card_id is None and not self.auto

    def needs_announce(self) -> bool:
        return self.card_id is not None and self.settled() and not self.card_announced

    def as_dict(self) -> dict[str, Any]:
        """pending_list 的一行。"""
        return {
            "request_id": self.request.request_id,
            "session_id": self.request.session_id,
            "tool_name": self.request.tool_name,
            "tool_summary": self.request.tool_summary,
            "working_directory": self.request.working_directory,
            "created_at": self.request.received_at,
            "created_time": _iso(self.request.received_at),
            "state": "pending" if self.is_pending() else "settled",
            "risk": self.severity,
            "rule": self.rule,
            "action_class": self.action_class,
            "decision": self.decision,
            "decided_by": self.decided_by,
            "auto": self.auto,
        }


def _clip(text: str, limit: int = 120) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_summary(tool_name: str, tool_input: Any) -> str:
    """从 tool_input 里取一句人能看懂的话。

    只取一个最有信息量的字段，不把整个 input 倒出来（可能含密钥/长文本）。
    取不到的就只写工具名——**不猜它要干什么**。
    """
    if not isinstance(tool_input, dict):
        return tool_name or "未知工具"
    for key in ("command", "file_path", "path", "pattern", "query", "url",
                "prompt", "description"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return f"{tool_name}: {_clip(value)}"
    return tool_name or "未知工具"


def build_request(payload: Any) -> ApprovalRequest:
    """把 hook 的 JSON 体转成 ApprovalRequest。

    容忍缺字段：``session_id`` 可能是空的（后台/无会话场景），
    那时键变成 ``("", request_id)``，照样能工作——只是定位信息少一截。
    """
    data = payload if isinstance(payload, dict) else {}
    tool_name = str(data.get("tool_name") or "")
    return ApprovalRequest(
        session_id=str(data.get("session_id") or ""),
        request_id=uuid.uuid4().hex[:12],
        tool_name=tool_name,
        tool_summary=_tool_summary(tool_name, data.get("tool_input")),
        working_directory=(str(data["cwd"]) if data.get("cwd") else None),
    )


def _port_in_use(port: int) -> bool:
    """127.0.0.1:port 上有没有别的东西在 listen。

    只连不听：连得上说明已被占用。故意不 accept/不发数据——
    那会干扰对方（可能是另一个插件实例）。
    """
    import socket

    with socket.socket() as probe:
        probe.settimeout(0.3)
        return probe.connect_ex(("127.0.0.1", int(port))) == 0


def hook_response(behavior: str, *, message: str = "") -> dict:
    """按官方 schema 组装回填体。

    ``message`` / ``interrupt`` 是 **deny 专属字段**（官方文档：deny 可带
    message/interrupt）。给 allow 挂 message 不但是无用信息，还是 schema 外的
    字段——L2 自动放行的说明走通知与审计，不回填。

    **allow 时刻意不带 ``updatedInput`` / ``updatedPermissions``**
    （规格 P5.2）：实测无害，但某些版本可能把空对象理解成"把参数清空"。
    我们没有要改写的参数，那就什么都别带——少给一个字段比多给一个
    可能被误解的字段安全。deny 仍按官方示例带齐。
    """
    decision: dict[str, Any] = {"behavior": behavior}
    if behavior != APPROVE_ALLOW:
        decision["updatedInput"] = {}
        decision["updatedPermissions"] = []
    if message:
        decision["message"] = message
        # 刻意不设 interrupt=True：拒绝这一次调用就好，
        # 掐掉整个回合会让 Claude 中途闭嘴，用户还得重新发起。
        decision["interrupt"] = False
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": decision,
        }
    }


def no_decision_response(*, reason: str, timeout_seconds: float) -> dict:
    """到期时的响应体：**不含任何 decision**。

    配 503 一起用。宿主看到非 2xx 且 ``onFailure:"block"`` 已配置，
    就会按它自己的失败策略处理（拦住这次调用）——那是宿主的决定，不是我们的。
    文案只写「宿主已自行处理，本插件未做判断」，不许出现"拒绝"字样。

    **F4（2026-11-21 真机实测）**：只写上面两句时，Claude Code 把到期理解成
    "hook 服务挂了"，反过来建议用户去查端口——用户白担心一场。所以补一句
    人话说明"这是在等你，不是它坏了"。刻意不出现端口号、地址、
    ``onFailure`` 这类内部词：那是给我们排查用的，写进给 CC 看的话里只会添乱。
    """
    return {
        "ok": False,
        "submitted": False,
        "reason": reason,
        "message": (
            f"同桌猫：{timeout_seconds:g} 秒内无人决定。"
            "宿主已自行处理本次调用，本插件未做判断。"
            "（这不是故障——同桌猫一直在等你做决定，没有代替你作答；"
            "超时后由 Claude Code 自己接手这次权限请求。）"
        ),
    }


# 审批按钮的 action id。写死在这里：HTML 里的 data-neko-action 与
# actions 字典的键必须一致，两边各写一遍字符串迟早对不上。
ACTION_APPROVE = "deskmate_approve"
ACTION_DENY = "deskmate_deny"
ACTION_EXPLAIN = "deskmate_explain"
# 「这是什么」卡上的"返回审批"：看完解释能当场回到审批态，
# 不然解释卡把按钮抢光了，用户只能干等到期（2026-11-22 真机反馈）。
ACTION_BACK = "deskmate_back"
# 「这是什么」讲多细（规格 P5.3）。三档，默认 standard。
#   brief     只讲结论与最坏结果
#   standard  加上范围与规则判据
#   detailed  再叠加模型给的理由与依据
VERBOSITY_BRIEF = "brief"
VERBOSITY_STANDARD = "standard"
VERBOSITY_DETAILED = "detailed"
VERBOSITIES = (VERBOSITY_BRIEF, VERBOSITY_STANDARD, VERBOSITY_DETAILED)
DEFAULT_VERBOSITY = VERBOSITY_STANDARD
ACTION_AUTH_CONFIRM = "deskmate_authorize_confirm"
ACTION_AUTH_CANCEL = "deskmate_authorize_cancel"
# 承载这四个按钮的入口 id。
ENTRY_DECIDE = "approval_decide"
ENTRY_EXPLAIN = "approval_explain"
ENTRY_SHOW = "approval_show"
ENTRY_AUTHORIZE = "approval_authorize"

CARD_CSS = (
    "section { padding: 12px; font-size: 13px; }"
    "strong { display: block; margin-bottom: 6px; }"
    "p { margin: 4px 0; }"
    ".loc { color: #888; font-size: 12px; }"
    # 动作行：第一行，说得最直白，稍微大一点
    ".act { font-size: 14px; margin: 8px 0 6px; }"
    # 结论行：独立成行、加粗、置上（规格 P2）
    ".verdict { font-size: 15px; margin: 8px 0 2px; }"
    ".verdict strong { display: inline; font-size: 15px; }"
    ".risk { color: #c0392b; font-size: 13px; margin: 6px 0; }"
    ".src { color: #888; font-size: 12px; margin: 8px 0 2px; }"
    ".hint { color: #aaa; font-size: 12px; margin-top: 8px; }"
    "button { margin: 8px 8px 0 0; padding: 6px 18px; }"
)


def _escape(text: str) -> str:
    """卡片 HTML 转义。工具参数是 Agent 产的，不能当可信 HTML 直接拼。"""
    import html

    return html.escape(str(text), quote=True)


def _minutes(seconds: float) -> str:
    """把秒数说成人话：570s → 「约 10 分钟」，30s → 「约 1 分钟」。"""
    minutes = max(1, round(seconds / 60.0))
    return f"约 {minutes} 分钟"


def build_card_html(*, subject: str, tool_summary: str, session_id: str,
                    working_directory: str | None, timeout_seconds: float,
                    advice_state: str = ADVICE_STATE_FALLBACK,
                    risk: str = DEFAULT_RISK_SEVERITY,
                    action_class: str = DEFAULT_ACTION_CLASS,
                    command: str = "", conclusion: str = "",
                    logger: Any = None) -> str:
    """审批卡片的 HTML。纯函数——卡片文案是验收项，必须能单测。

    六块内容：标题/来源/命令、**极高危红条**、会话与 cwd、
    建议（陈述句）、超时说明、三个按钮。
    ``subject`` 由调用方用归因语法生成（``describe_actor``），
    这样播报和卡片说的是同一套话。

    建议块刻意排在按钮**上方**（规格 §16.1「覆盖 §12.1」）：用户点之前要看到。

    **任何 ``advice_state`` 都不能让这个函数抛异常。** 模型建议是锦上添花，
    卡片是核心功能——建议挂了也必须能正常审批。所以未知状态走
    ``advice_text`` 的兜底文案；传了 ``logger`` 就顺手记一条 warning，
    让"为什么这条没有结论"有据可查（不传也不影响渲染）。
    """
    location = f"会话 {session_id or '未知'}"
    if working_directory:
        location += f" @ {working_directory}"

    if not is_known_advice_state(advice_state):
        # 未知状态：兜底 + 记账。绝不向上抛——那会让整张卡片消失。
        if logger is not None:
            logger.warning(
                "审批卡片收到未知的建议状态 {!r}，按「无建议」渲染（请求 {}）",
                advice_state, session_id or "未知",
            )
        advice_state = ADVICE_STATE_FALLBACK

    # 极高危永远呈现（规格 §2 硬规则 3 + §17.2）：标红 + 明说任何配置都不会
    # 让它自动放行。判据是**动作类别或极高危档**，不是规则名（C3）——
    # 规则名会随规则库变，红条不能跟着漏。
    high_risk = (action_class in IRREVERSIBLE_CLASSES
                 or SEVERITY_ORDER.get(risk, 0) >= SEVERITY_ORDER["critical"])
    risk_html = (
        '<p class="risk"><strong>高风险动作：任何配置都不会让它自动放行，'
        "必须由你亲自点允许或拒绝。</strong></p>"
        if high_risk else ""
    )

    # 主卡正文固定四行（规格 P3）：动作 / 后果 / 范围 / 结论。
    # 前三行由命令翻译词典产出（本地、零 key），第四行结论由调用方填——
    # 词典只描述事实，不给结论。
    lines = card_lines(command=command or tool_summary,
                       working_directory=working_directory)
    # 结论行的取值受 advice_state 约束：**只有真的拿到了模型建议，
    # 结论才可能是"建议允许/建议拒绝"**。建议层降级时（没 key、没签字、
    # 调用失败、状态未知）一律落最保守的第三态——哪怕调用方误传了一个结论，
    # 也不能让一个没有依据的"建议允许"出现在卡片上。
    if advice_state == ADVICE_STATE_READY and conclusion in CONCLUSIONS:
        conclusion_text = conclusion
    else:
        conclusion_text = CONCLUSION_UNSURE
    body_lines = [
        # 结论行**置于最上**（规格 P2）：快速扫读时眼睛先落在哪里，
        # 结论就该在哪里。P3 的四行样例把结论排在最后，与 P2 的"置于最上"
        # 冲突；这里按 P2 排，因为 P2 给了理由（误读），P3 只给了内容清单。
        f'<p class="verdict"><strong>{_escape(conclusion_text)}</strong></p>',
        f'<p class="act">{_escape(lines["action"])}</p>',
        f"<p>{_escape(lines['consequence'])}</p>",
        f"<p>{_escape(lines['scope'])}</p>",
    ]

    return (
        "<section>"
        "<strong>同桌猫 · 审批请求</strong>"
        f"<p class=\"loc\">{_escape(location)}</p>"
        + "".join(body_lines)
        + risk_html
        + f"<p class=\"src\">{_escape(subject)} 请求执行</p>"
        + f"<p class=\"hint\">允许后 Claude Code 继续执行；拒绝则中止这次调用"
          f"（不掐掉整个回合）。{_minutes(timeout_seconds)}后无人决定，"
          "宿主会自行处理，届时这条视为未决定——插件不代你判断。</p>"
          f"<button data-neko-action=\"{ACTION_APPROVE}\">允许</button>"
          f"<button data-neko-action=\"{ACTION_DENY}\">拒绝</button>"
          f"<button data-neko-action=\"{ACTION_EXPLAIN}\">这是什么</button>"
          "</section>"
    )


def explain_request(*, tool_name: str, tool_summary: str,
                    working_directory: str | None, rule: str,
                    severity: str, command: str = "",
                    model_reason: str = "", basis: str = "",
                    model_note: str = "",
                    verbosity: str = DEFAULT_VERBOSITY) -> dict[str, str]:
    """「这是什么」按钮的落点：解释，只说事实，不猜意图。

    主卡的正文只有固定四行（规格 P3），所以模型的理由与依据放在这里——
    「这是什么」就是看细节的地方，主卡不是。

    ``basis`` 那行按规格 P3 整行来自动生成（工具 / 目录 / 风险档 / 模型名），
    不许手写：手写的内容迟早和实际字段不一致。
    """
    if rule == "delete":
        verdict = f"L0 规则判定为**删除类**（{severity}），属于高风险动作。"
        extra = ("这类动作永不进入 L2 自动决定，必须由你亲自点允许或拒绝；"
                 "宿主侧的 onFailure 策略只在你一直不决定时兜底。")
    elif rule:
        verdict = f"L0 规则命中 {rule}（{severity}）。"
        extra = "是否放行由你决定，插件只转发、不代判。"
    else:
        verdict = "L0 规则未命中，按低风险（notice）处理。"
        extra = ("即使风险低，只要没开 L2 自动决定，插件仍然只转发、不代判；"
                 "你一直不决定的话，到期后由宿主按 onFailure 策略处理。")

    explanation = {
        "title": "这是什么",
        "what": f"Claude Code 请求执行 {tool_name or '某个工具'}：{tool_summary}",
        "verdict": verdict,
        "extra": extra,
        "location": (f"会话工作目录 {working_directory}" if working_directory
                     else "会话工作目录未知"),
        "note": ("以上是规则层能讲准的事实。看不出来历的地方不会替你猜——"
                 "要不要放行由你定。"),
    }
    # 讲多细由配置决定（规格 P5.3）。brief 只留结论与后果；standard 加范围
    # 与规则判据；detailed 再叠加模型给的理由与依据。
    if verbosity == VERBOSITY_BRIEF:
        explanation.pop("location", None)
        explanation.pop("extra", None)
    elif verbosity != VERBOSITY_DETAILED:
        model_reason, basis = "", ""
    if model_note:
        # 没接入模型时的那句陈述。放在解释卡而不是主卡：主卡正文是固定四行，
        # 而这句话属于"细节"，正是「这是什么」该放的地方。
        explanation["model_note"] = model_note
    if model_reason:
        explanation["model_reason"] = f"模型给的理由：{model_reason}"
    if basis:
        explanation["basis"] = f"依据：{basis}"
    return explanation


def build_explain_html(explanation: dict[str, str],
                       request_id: str = "") -> str:
    """把 explain_request 的结果渲染成卡片用的 HTML。

    **自带三个按钮**（允许 / 拒绝 / 返回审批）。2026-11-22 真机反馈：
    解释卡替换了审批卡之后按钮全没了，用户只能干等到期——看完解释
    当然要能当场决定，所以按钮必须跟着解释一起出现。
    """
    buttons = (
        f"<button data-neko-action=\"{ACTION_APPROVE}\">允许</button>"
        f"<button data-neko-action=\"{ACTION_DENY}\">拒绝</button>"
        f"<button data-neko-action=\"{ACTION_BACK}\">返回审批</button>"
        if request_id else ""
    )
    return (
        "<section>"
        f"<strong>{_escape(explanation['title'])}</strong>"
        f"<p>{_escape(explanation['what'])}</p>"
        f"<p>{_escape(explanation['verdict'])}</p>"
        f"<p>{_escape(explanation['extra'])}</p>"
        f"<p class=\"loc\">{_escape(explanation['location'])}</p>"
        + (f"<p class=\"src\">{_escape(explanation['model_note'])}</p>"
           if explanation.get("model_note") else "")
        + (f"<p><strong>{_escape(explanation['model_reason'])}</strong></p>"
           if explanation.get("model_reason") else "")
        + (f"<p class=\"hint\">{_escape(explanation['basis'])}</p>"
           if explanation.get("basis") else "")
        + f"<p class=\"hint\">{_escape(explanation['note'])}</p>"
        + buttons
        + "</section>"
    )


def build_explain_actions(request_id: str) -> dict[str, dict[str, Any]]:
    """解释卡的三个按钮 → 两个入口 + 一个返回。"""
    return {
        ACTION_APPROVE: {"entry": ENTRY_DECIDE,
                         "args": {"request_id": request_id, "decision": APPROVE_ALLOW}},
        ACTION_DENY: {"entry": ENTRY_DECIDE,
                      "args": {"request_id": request_id, "decision": APPROVE_DENY}},
        ACTION_BACK: {"entry": ENTRY_SHOW,
                      "args": {"request_id": request_id}},
    }


def build_card_actions(request_id: str) -> dict[str, dict[str, Any]]:
    """三个按钮 → 两个入口，用 args 区分。"""
    return {
        ACTION_APPROVE: {"entry": ENTRY_DECIDE,
                         "args": {"request_id": request_id, "decision": APPROVE_ALLOW}},
        ACTION_DENY: {"entry": ENTRY_DECIDE,
                      "args": {"request_id": request_id, "decision": APPROVE_DENY}},
        ACTION_EXPLAIN: {"entry": ENTRY_EXPLAIN,
                         "args": {"request_id": request_id}},
    }


def build_authorize_actions(scopes: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """授权卡的两个按钮 → 同一个入口，用 args 区分签不签字。"""
    return {
        ACTION_AUTH_CONFIRM: {"entry": ENTRY_AUTHORIZE,
                             "args": {"scopes": ",".join(scopes), "confirm": True}},
        ACTION_AUTH_CANCEL: {"entry": ENTRY_AUTHORIZE,
                             "args": {"scopes": ",".join(scopes), "confirm": False}},
    }


def build_authorize_html(*, scopes: tuple[str, ...]) -> str:
    """授权确认卡。文案固定，不掺变量——用户签字前看到的话不该随上下文变。"""
    return (
        "<section>"
        f"<strong>{_escape(AUTHORIZATION_TITLE)}</strong>"
        f"<p>{_escape(AUTHORIZATION_TEXT)}</p>"
        f"<p class=\"risk\">{_escape(AUTHORIZATION_HEADLINE)}</p>"
        f"<p>本次确认范围：{_escape('、'.join(scopes))}（授权版本 "
        f"v{AUTHORIZATION_VERSION}）</p>"
        f"<p class=\"hint\">{_escape(AUTHORIZATION_FOOTER)}</p>"
        f"<button data-neko-action=\"{ACTION_AUTH_CONFIRM}\">确认授权</button>"
        f"<button data-neko-action=\"{ACTION_AUTH_CANCEL}\">暂不授权</button>"
        "</section>"
    )


def build_result_html(note: str) -> str:
    """结果态卡片：只留一句话，按钮已清掉。"""
    return f"<section><p>{_escape(note)}</p></section>"


class AuthorizationRecord(BaseModel):
    """用户对 L1/L2 的显式授权确认。

    写在磁盘上，插件重启后恢复——恢复的不是"自动开启"：配置里
    ``ai_judgement=false`` / ``auto_decide="off"`` 时，授权记录在手也不会有
    任何动作。它只是避免重启把用户已经做过的确认吞掉。
    """

    version: int = AUTHORIZATION_VERSION
    scopes: tuple[str, ...] = ()
    confirmed_at: float = Field(default_factory=time.time)
    confirmed_by: str = Field(default="user")
    note: str = Field(default="")

    def as_audit_text(self) -> str:
        """审计里的授权溯源：版本 + 范围 + 谁签的字。"""
        return f"v{self.version}:{','.join(self.scopes) or '-'} by={self.confirmed_by}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "scopes": list(self.scopes),
            "confirmed_time": _iso(self.confirmed_at),
            "confirmed_by": self.confirmed_by,
            "note": self.note,
        }


def load_authorization(path: Any) -> AuthorizationRecord | None:
    """从磁盘恢复授权确认。文件不存在/读不懂/校验不过都当没有——不猜。"""
    if path is None:
        return None
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return AuthorizationRecord.model_validate(raw)
    except ValidationError:
        return None


class AuditRecord(BaseModel):
    """一条审计行。每次决定都写一条，含 L2 的授权溯源、模型与理由。"""

    # kind = decision（一次审批决定）| authorization（一次授权确认）。
    # 两者共用一张表：授权是决定的依据，拆开就查不到"这条自动放行凭的是什么"。
    kind: str = Field(default="decision")
    ts: float = Field(default_factory=time.time)
    request_id: str = ""
    session_id: str = ""
    tool_summary: str = ""
    decision: str | None = Field(default=None, description="allow / deny；None = 没发决定")
    decided_by: str = Field(description="user / ai / expired / host_timeout；授权行为 authorization")
    model: str = Field(default="", description="给出该决定的模型；空 = 非模型决定")
    reason: str = Field(default="", description="一句话理由")
    rule: str = Field(default="", description="命中的 L0 规则；空 = 未命中")
    severity: str = Field(default="", description="L0 风险档")
    action_class: str = Field(
        default="", description="动作类别：recoverable / irreversible；空 = 未涉及")
    authorization: str = Field(default="", description="生效中的授权溯源；空 = 未涉及授权")

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ts": self.ts,
            "time": _iso(self.ts),
            "request_id": self.request_id,
            "session_id": self.session_id,
            "tool_summary": self.tool_summary,
            "decision": self.decision,
            "decided_by": self.decided_by,
            "model": self.model or "-",
            "reason": self.reason,
            "rule": self.rule or "-",
            "severity": self.severity or "-",
            "action_class": self.action_class or "-",
            "authorization": self.authorization or "-",
        }

    def as_text(self) -> str:
        return (f"{_iso(self.ts)} | {self.kind} | {self.decision or '无决定'} | "
                f"by={self.decided_by} | rule={self.rule or '-'} | "
                f"class={self.action_class or '-'} | "
                f"auth={self.authorization or '-'} | model={self.model or '-'} | "
                f"req={self.request_id or '-'} | "
                f"session={self.session_id or '无'} | {self.tool_summary} | {self.reason}")


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()


class ApprovalGateway:
    """本地 HTTP 服务 + pending 状态机。线程安全，不依赖宿主。"""

    def __init__(self, logger: Any, *, port: int = 48921,
                 timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
                 retention_seconds: float = DEFAULT_RETENTION_SECONDS,
                 ai_judgement: bool = False,
                 auto_decide: str = AUTO_OFF,
                 auto_decide_max_severity: str = "notice",
                 has_api_key: bool = False,
                 audit_path: Any = None,
                 authorization_path: Any = None,
                 judge: Any = None) -> None:
        self._logger = logger
        self._port = int(port)
        self._timeout_seconds = float(timeout_seconds)
        self._retention_seconds = float(retention_seconds)
        # 门控开关：**只能由用户写配置**，运行期不可改（见 policy_snapshot 的测试）。
        self._ai_judgement = bool(ai_judgement)
        self._auto_decide = str(auto_decide)
        # 极高危档不可被自动放行（规格 §3），所以阈值开到 critical 是无意义的，
        # 夹到 warn 并留一条日志——默默接受一个"开了却开不动"的配置更难查。
        self._auto_decide_max_severity = effective_max_severity(
            auto_decide_max_severity)
        self._has_api_key = bool(has_api_key)
        self._audit_path = audit_path
        self._authorization_path = authorization_path
        # 用户的显式授权确认。默认 None = 没签字 → L1/L2 全不可用。
        self._authorization = load_authorization(authorization_path)
        # 判断后端。5a 恒为 None——门控算得出来，但没有模型就不自动决定。
        self._judge = judge
        self._lock = threading.Lock()
        self._records: dict[tuple[str, str], PendingApproval] = {}
        self._audit: list[AuditRecord] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.stats_allow = 0
        self.stats_deny = 0
        self.stats_ai = 0
        self.stats_expired = 0
        self.stats_host_timeout = 0
        self.stats_authorizations = 0

    # ── 生命周期 ───────────────────────────────────────────────────────────

    @property
    def port(self) -> int:
        return self._port

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    def running(self) -> bool:
        return self._server is not None

    def start(self) -> bool:
        """绑 127.0.0.1 并起守护线程。端口占用等原因失败返回 False，不抛异常。

        失败只是"审批中继不可用"，观测与播报照常跑——不能因为一个端口
        把整个插件拖死。调用方负责把失败写进日志。
        """
        if self._server is not None:
            return True
        if _port_in_use(self._port):
            # 不能只靠 bind 报错：HTTPServer 默认开 SO_REUSEADDR，Windows 上
            # 它允许两个 socket 绑同一个端口，结果第二个服务起得来却永远收不到
            # hook——那种静默失败比直接报错难查得多。所以先探一次。
            self._logger.error(
                "审批服务启动失败：端口 {}（127.0.0.1）已被其它程序占用。"
                "PermissionRequest hook 会打不进来，请改 approval_port 并同步改 hooks.json",
                self._port,
            )
            return False
        gateway = self

        class Handler(_HookHandler):
            pass

        Handler.gateway = gateway  # type: ignore[attr-defined]
        try:
            server = ThreadingHTTPServer(("127.0.0.1", self._port), Handler)
        except OSError as exc:
            self._logger.error(
                "审批服务启动失败，端口 {} 绑不上（{}）：{}。"
                "PermissionRequest hook 将打不进来，请改 approval_port 并同步改 hooks.json",
                self._port, type(exc).__name__, exc,
            )
            return False
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, name="deskmate-approval", daemon=True,
        )
        self._thread.start()
        # 状态行拆成「配置怎么写」与「实际生不生效」两列（规格 P5.1）：
        # 原来只有一个"L1建议=开/关"，而它按 advice_state() 算——授权没确认时
        # 显示"关"，用户明明把配置打开了，看着像插件没听懂。
        self._logger.info(
            "审批服务就绪 http://127.0.0.1:{}{} 到期={}s 保留={}s "
            "配置=建议{}/自动{} 生效=建议{}/自动{} 授权={} 判断后端={}",
            self._port, HOOK_PATH, self._timeout_seconds, self._retention_seconds,
            "开" if self._ai_judgement else "关",
            self._auto_decide,
            "是" if self._l1_ready() else "否",
            "是" if self._auto_decide_live() else "否",
            "已确认" if self._authorization is not None else "未确认",
            "已接入" if self._judge is not None else "未接入（5b）",
        )
        if self._authorization is None and (self._ai_judgement
                                            or self._auto_decide != AUTO_OFF):
            # 配置开了但没签字：不说清楚，用户会以为已经生效了。
            self._logger.warning(
                "approval.ai_judgement / auto_decide 已配置，但用户尚未确认授权，"
                "L1/L2 均不生效。调用 approval_authorize 确认授权文案后才可用",
            )
        return True

    def stop(self) -> None:
        server, self._server, self._thread = self._server, None, None
        if server is None:
            return
        try:
            server.shutdown()
            server.server_close()
        except Exception as exc:  # noqa: BLE001  关闭失败不能炸掉 shutdown
            self._logger.warning("审批服务关闭异常（{}）：{}", type(exc).__name__, exc)
        with self._lock:
            for record in self._records.values():
                if record.decided_by is None:
                    # 关服务时还挂着的请求：我们不代用户决定，只标记，
                    # 让宿主的 onFailure 策略去处理。
                    record.decided_by = DECIDED_BY_EXPIRED
                    entry = self._write_audit_locked(record)
                    self._count_locked(record)
                    # 关服务没有"投递"这一步，直接落盘即可。
                    self._append_audit_file(entry)
                record.event.set()
        self._logger.info("审批服务已关闭")

    # ── hook 侧 ────────────────────────────────────────────────────────────

    def submit(self, payload: Any, *, respond: Any = None) -> dict | None:
        """HTTP handler 调入口：登记 → 事件发生时判 L2 → 等用户 → 回填或**不回填**。

        返回值是「要回填给宿主的响应体」；``None`` 表示**没有决定**，
        调用方应回 503 且不带任何 decision。

        ``respond`` 是可选的投递回调：传了就在拿到结果时立刻投递，
        并由这里统一捕获"连接已断开"（记 ``host_timeout``）。
        不传则只返回结果，由调用方自己投递（单测走这条）。

        这个函数会**阻塞调用它的线程**（每个 hook 请求一个线程），
        所以绝不能在主循环里调。
        """
        request = build_request(payload)
        record = PendingApproval(request)
        record.severity, record.rule, record.action_class = assess_risk(
            request.tool_name, (payload or {}).get("tool_input")
            if isinstance(payload, dict) else None,
        )
        with self._lock:
            self._records[record.key] = record
            self._prune_locked()
        self._logger.info(
            "收到审批请求 {}（会话 {} 工具 {} 风险 {}）", request.request_id,
            request.session_id or "无", request.tool_name or "未知", record.severity,
        )

        # L2 在**事件发生的那一刻**判，不是等到期（规格 §2 硬规则 4）。
        # 放在锁外：判断后端可能慢（网络），不能堵住 resolve / HUD / 建卡。
        self._maybe_auto_allow(record)

        record.event.wait(timeout=self._timeout_seconds)

        with self._lock:
            if record.decided_by is None:
                # 到期：**只标记，不发任何决定、不记成拒绝**（规格 §2 硬规则 1）。
                # 拦住这次调用的是宿主自己的 onFailure:"block"，不是我们的判断。
                self._mark_expired_locked(record)

        # 审计行进内存必须**早于**投递：客户端一收到回填就可能来查审计，
        # "先回客户端后记账"会让 audit_rows() 偶发少一行——实测 CI seed 下
        # 约 1/4 概率红一次。落盘则必须晚于投递：host_timeout 是投递失败才
        # 知道的，JSONL 是追加写的，只能写最终状态。
        with self._lock:
            entry = self._write_audit_locked(record)

        result = self._response_for(record)
        if respond is not None:
            try:
                respond(result)
            except (ConnectionError, TimeoutError) as exc:
                # 连接类失败一律算"没送达"：Windows 上客户端提前离开时抛的是
                # ConnectionAbortedError(10053)，它**不是** BrokenPipeError 的子类，
                # 只捕后两者会漏——实测踩过。不重试：重试等于发两条。
                self._logger.info(
                    "回填时连接已断开（{}），请求 {} 记为 host_timeout，未送达",
                    type(exc).__name__, request.request_id,
                )
                self.note_host_timeout(request.request_id)

        with self._lock:
            # 计数只按**最终**状态记一次：投递失败时这条不算"已允许"。
            self._count_locked(record)
            self._append_audit_file(entry)
            # **不删记录**：过期条目要留在 _records 里供 pending_list 事后查，
            # 由 _prune_locked 按保留期统一清理。删在这里就等于"过期即消失"，
            # 而那正是审计最需要看到的几条。
        return result

    # ── L2 自动放行（事件发生时；规格 §2 硬规则 2/3/4） ─────────────────────

    def _maybe_auto_allow(self, record: PendingApproval) -> bool:
        """门控 + 后端都过，才替用户点一次"允许"。返回是否自动放行了。

        **永不 auto-deny**：后端说 deny 也不代用户拒绝，照样把卡片摆出来让人点。
        没授权 / 没 key / 没后端 / 后端异常 / 后端说 deny / 无效返回 → 一律不自动
        决定，记录留给用户，走到期。也就是说：这条路只有一个可能的产出是 allow。
        """
        with self._lock:
            authorization = self._authorization
        scopes = authorization.scopes if authorization is not None else ()
        eligible, why = auto_decide_gate(
            severity=record.severity, action_class=record.action_class,
            ai_judgement=self._ai_judgement, has_key=self._has_api_key,
            auto_decide=self._auto_decide,
            max_severity=self._auto_decide_max_severity,
            authorized_scopes=scopes,
        )
        if not eligible:
            # 绝大多数请求走这条（默认全关）。不打日志：每次都打就是噪音，
            # 原因在卡片与审计里都能看到。
            return False
        if self._judge is None:
            # 门控算得过但没接判断后端（5a）。**不因为门控通过就自己拍一个**——
            # 那是插件在替用户判断。把请求留给用户，走到期。
            self._logger.info(
                "请求 {} 的 L2 门控通过（{}），但判断后端未接入（5b），"
                "不自动决定，转交用户判断",
                record.request.request_id, why,
            )
            return False

        try:
            verdict = self._judge(record)
        except Exception as exc:  # noqa: BLE001  后端炸了不能炸掉审批
            self._logger.warning(
                "请求 {} 的 L2 判断后端异常（{}），不自动决定，转交用户判断",
                record.request.request_id, type(exc).__name__,
            )
            return False
        if not isinstance(verdict, dict) or verdict.get("decision") not in DECISIONS:
            self._logger.warning(
                "请求 {} 的 L2 判断没给出有效决定，不自动决定，转交用户判断",
                record.request.request_id,
            )
            return False
        if verdict["decision"] != APPROVE_ALLOW:
            # 永不 auto-deny（规格 §2 硬规则 3）：AI 说"拒"也只是个建议，
            # 拒绝必须由用户亲手点。
            self._logger.info(
                "请求 {} 的 L2 判断建议 deny，但插件永不自动拒绝，转交用户判断",
                record.request.request_id,
            )
            return False

        with self._lock:
            # 用户可能在这一瞬间点了按钮——那种情况以用户为准，不覆盖。
            if record.decided_by is not None:
                return False
            record.decision = APPROVE_ALLOW
            record.decided_by = DECIDED_BY_AI
            record.judge_model = str(verdict.get("model") or "")
            record.judge_reason = str(verdict.get("reason") or "")
            record.authorization = (authorization.as_audit_text()
                                    if authorization is not None else "")
            record.auto = True
            # 自动放行不该再弹卡片等人点——但要发一条 blind 通知 + 一条审计。
            # card_id 置成占位空串，timer 就不会为它建卡。
            record.card_id = ""
            record.event.set()
        self._logger.warning(
            "请求 {} 由 L2 自动放行（用户已授权 {}，模型 {}，理由 {}）",
            record.request.request_id,
            authorization.as_audit_text() if authorization is not None else "-",
            record.judge_model or "-", record.judge_reason or "-",
        )
        return True

    def _response_for(self, record: PendingApproval) -> dict | None:
        """记录 → 回填体。``None`` = 没有决定，调用方回 503。"""
        if record.decided_by == DECIDED_BY_EXPIRED:
            return None
        if record.decided_by == DECIDED_BY_AI:
            # L2 只可能自动 allow；且 **allow 不带 message**（deny 专属字段）。
            # 自动放行的说明走 blind 通知、卡片与审计，不回填给宿主。
            return hook_response(APPROVE_ALLOW)
        # message 是 deny 专属字段：allow 时带上"你拒绝了"会自相矛盾
        # （这个 bug 在端到端跑第一遍时撞见过一次，重构后又差点回来，
        #  所以有单测盯着）。
        if record.decision == APPROVE_DENY:
            return hook_response(APPROVE_DENY, message=_user_deny_message())
        return hook_response(APPROVE_ALLOW)

    def _mark_expired_locked(self, record: PendingApproval) -> None:
        """到期：只标记 expired，**不发任何决定**。调用方必须已持有锁。

        这里刻意不碰门控、不调判断后端——L2 的时机是事件发生的那一刻
        （``_maybe_auto_allow``），到期时再判就等于让用户白等 570 秒。
        到期只做一件事：把状态写成"没人决定"。
        """
        record.decided_by = DECIDED_BY_EXPIRED
        self._logger.warning(
            "审批到期（{}s）无人决定：宿主已自行处理，本插件未做判断，"
            "请求 {} 标记 expired 且不回填",
            self._timeout_seconds, record.request.request_id,
        )

    def _count_locked(self, record: PendingApproval) -> None:
        """按最终 decided_by 记一次数。调用方必须已持有锁。

        集中在一处：早期版本把计数写在各个分支里，结果 stats_ai 永远是 0——
        因为那时 if/elif 已经走完，加在分支里的计数没人执行。
        """
        if record.decided_by == DECIDED_BY_USER:
            if record.decision == APPROVE_ALLOW:
                self.stats_allow += 1
            else:
                self.stats_deny += 1
        elif record.decided_by == DECIDED_BY_AI:
            self.stats_ai += 1
        elif record.decided_by == DECIDED_BY_EXPIRED:
            self.stats_expired += 1
        elif record.decided_by == DECIDED_BY_HOST_TIMEOUT:
            self.stats_host_timeout += 1

    def _write_audit_locked(self, record: PendingApproval) -> AuditRecord:
        """建一条审计行并放进内存，返回它。调用方必须已持有锁。

        **只进内存，不落盘、不计数**：落盘要等投递结果（host_timeout 是投递
        失败才知道的），计数要按最终状态记一次，两件事都由 submit 收尾。
        """
        entry = AuditRecord(
            kind="decision",
            request_id=record.request.request_id,
            session_id=record.request.session_id,
            tool_summary=record.request.tool_summary,
            decision=record.decision,
            decided_by=record.decided_by or DECIDED_BY_EXPIRED,
            model=record.judge_model,
            rule=record.rule,
            severity=record.severity,
            action_class=record.action_class,
            authorization=record.authorization,
            reason=self._audit_reason(record),
        )
        self._audit.append(entry)
        if len(self._audit) > AUDIT_MEMORY_LIMIT:
            del self._audit[: len(self._audit) - AUDIT_MEMORY_LIMIT]
        # 记在记录上：投递失败时 note_host_timeout 要能把这一条改成最终状态。
        record.audit_entry = entry
        return entry

    def _append_audit_locked(self, entry: AuditRecord) -> None:
        """进内存 + 落盘。调用方必须已持有锁。"""
        self._audit.append(entry)
        if len(self._audit) > AUDIT_MEMORY_LIMIT:
            del self._audit[: len(self._audit) - AUDIT_MEMORY_LIMIT]
        self._append_audit_file(entry)

    def _audit_reason(self, record: PendingApproval) -> str:
        if record.decided_by == DECIDED_BY_USER:
            return "用户在 N.E.K.O 里点了按钮"
        if record.decided_by == DECIDED_BY_AI:
            return (getattr(record, "judge_reason", "")
                    or "L2 在用户授权下自动放行（低风险）")
        if record.decided_by == DECIDED_BY_HOST_TIMEOUT:
            return "回填时连接已被宿主断开，决定未送达"
        # 到期：**不许出现"拒绝"字样**——我们没拒绝，是宿主自己处理的。
        return "到期无人决定；宿主已自行处理，本插件未做判断"

    def _append_audit_file(self, entry: AuditRecord) -> None:
        """审计落一份 JSONL，插件重启后仍可查。写失败只记日志，不丢内存里的那份。"""
        if self._audit_path is None:
            return
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self._audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry.as_dict(), ensure_ascii=False) + "\n")
        except OSError as exc:
            self._logger.warning("审计写入失败（{}）：{}", type(exc).__name__, exc)

    # ── 显式授权（规格 §2 硬规则 2） ─────────────────────────────────────────

    def grant_authorization(self, scopes: Any, *,
                            confirmed_by: str = "user") -> AuthorizationRecord:
        """记录用户的授权确认。**只有用户在卡片上点了「确认授权」才能走到这。**

        插件自身没有任何路径调用它：没有定时任务、没有 hook、没有配置变更会
        触发授权（``on_config_change`` 刻意不碰它，见 ``__init__.py``）。
        """
        clean = tuple(dict.fromkeys(
            str(scope) for scope in (scopes or ()) if str(scope) in AUTH_SCOPES))
        if not clean:
            raise ValueError(f"没有可授权的范围（可用：{'/'.join(AUTH_SCOPES)}）")
        record = AuthorizationRecord(scopes=clean, confirmed_by=confirmed_by or "user")
        with self._lock:
            self._authorization = record
            self.stats_authorizations += 1
            self._persist_authorization_locked(record)
            self._append_audit_locked(AuditRecord(
                kind="authorization",
                tool_summary="授权确认",
                decision=None,
                decided_by=DECIDED_BY_AUTHORIZATION,
                authorization=record.as_audit_text(),
                reason=f"用户确认授权文案：{AUTHORIZATION_HEADLINE}",
            ))
        self._logger.warning(
            "用户已确认授权：范围={} 版本=v{}。{}",
            ",".join(clean), record.version, AUTHORIZATION_HEADLINE,
        )
        return record

    def _persist_authorization_locked(self, record: AuthorizationRecord) -> None:
        """授权确认落盘，重启后恢复。写失败只记日志——不影响本次生效。"""
        if self._authorization_path is None:
            return
        try:
            self._authorization_path.parent.mkdir(parents=True, exist_ok=True)
            self._authorization_path.write_text(
                json.dumps(record.model_dump(), ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            self._logger.warning("授权确认写入失败（{}）：{}", type(exc).__name__, exc)

    def authorized_scopes(self) -> tuple[str, ...]:
        with self._lock:
            record = self._authorization
        return record.scopes if record is not None else ()

    def authorization_snapshot(self) -> dict[str, Any]:
        """授权状态快照（只读）。给 status 入口和授权卡片用。"""
        with self._lock:
            record = self._authorization
        if record is None:
            return {
                "granted": False, "version": 0, "scopes": [],
                "confirmed_time": None, "confirmed_by": "",
                "text": AUTHORIZATION_TEXT, "headline": AUTHORIZATION_HEADLINE,
            }
        snapshot = record.as_dict()
        snapshot.update({
            "granted": True,
            "text": AUTHORIZATION_TEXT,
            "headline": AUTHORIZATION_HEADLINE,
        })
        return snapshot

    def note_host_timeout(self, request_id: str) -> None:
        """投递失败：决定可能做了但没送到宿主。

        把 ``decided_by`` 覆盖成 ``host_timeout``——从宿主的视角看，
        这条请求**没有发生过任何决定**（它没收到），所以审计必须这么记。
        原本的决定留在 ``decision`` 字段里，人和审计都能看出"用户选了允许但没送达"。

        **同时改已经写进内存的那条审计行**：它比投递更早落（见 submit 里的注释），
        不改的话内存里会留着"by=user"，而落盘的 JSONL 是"by=host_timeout"——
        同一份审计两种说法，比没有审计更糟。
        计数交给 ``_count_locked``，这里只改状态，避免两处各加一次。
        """
        with self._lock:
            for record in self._records.values():
                if record.request.request_id != request_id:
                    continue
                if record.decided_by != DECIDED_BY_HOST_TIMEOUT:
                    record.decided_by = DECIDED_BY_HOST_TIMEOUT
                entry = record.audit_entry
                if entry is not None:
                    entry.decided_by = DECIDED_BY_HOST_TIMEOUT
                    entry.decision = record.decision
                    entry.reason = self._audit_reason(record)
                return

    def _prune_locked(self) -> None:
        """清掉超过保留期的记录。调用方必须已持有锁。"""
        cutoff = time.time() - self._retention_seconds
        stale = [key for key, record in self._records.items()
                 if record.request.received_at < cutoff]
        for key in stale:
            del self._records[key]

    # ── 插件侧（timer / UI 动作 / 入口） ───────────────────────────────────

    def take_new(self) -> list[PendingApproval]:
        """取走还没建卡片的记录（取走即占位，避免重复建卡）。"""
        with self._lock:
            self._prune_locked()
            fresh = [r for r in self._records.values() if r.needs_card()]
            for record in fresh:
                record.card_id = ""      # 占位：表示"已取走，正在建"
            return fresh

    def take_auto_allowed(self) -> list[PendingApproval]:
        """取走已自动放行、还没发过通知的记录（取走即占位，避免重复通知）。

        自动放行不弹卡片，但**必须让用户知道**：看不见的放行等于后门。
        通知由插件的 approval timer 发（push_message 是 async SDK 调用）。
        """
        with self._lock:
            fresh = [r for r in self._records.values() if r.auto and not r.auto_announced]
            for record in fresh:
                record.auto_announced = True
            return fresh

    def attach_card(self, request_id: str, card_id: str) -> None:
        with self._lock:
            for record in self._records.values():
                if record.request.request_id == request_id:
                    record.card_id = card_id
                    return

    def settle_pending_cards(self) -> list[PendingApproval]:
        """已建卡且已了结、但还没在卡片上显示的记录。"""
        with self._lock:
            return [r for r in self._records.values() if r.needs_announce()]

    def mark_announced(self, request_id: str) -> None:
        with self._lock:
            for record in self._records.values():
                if record.request.request_id == request_id:
                    record.card_announced = True
                    return

    def resolve(self, request_id: str, decision: str, *,
                decided_by: str = DECIDED_BY_USER) -> str:
        """UI 按钮的落点。返回 ``resolved`` / ``already-resolved`` / ``expired`` / ``unknown``。

        **幂等**：重复点击（用户连点、多窗口各点一次）只有第一次算数。
        宿主的按钮代理本来就会忽略执行中按钮的重复点击，但那是 UI 层的
        尽力而为，业务层必须自己保证——跨窗口的重复它管不了。
        """
        if decision not in DECISIONS:
            return "unknown"
        with self._lock:
            for record in self._records.values():
                if record.request.request_id != request_id:
                    continue
                if record.decided_by == DECIDED_BY_EXPIRED:
                    # 过期的卡片按钮：不回填，明确告诉用户这条已经不算数了。
                    return "expired"
                if record.decided_by is not None:
                    return "already-resolved"
                record.decision = decision
                record.decided_by = decided_by
                record.event.set()
                self._logger.info(
                    "审批已决定（{} by {}）：请求 {}", decision, decided_by, request_id,
                )
                return "resolved"
        return "unknown"

    def policy_snapshot(self) -> dict[str, Any]:
        """当前门控开关与授权状态的快照。

        **只读**：这些值只能来自用户配置（+ 用户在授权卡上签字），
        运行期没有任何方法能改它们。测试靠这一点守住规格 §2 硬规则 2
        「没有显式授权就不执行任何明确操作」。
        """
        return {
            "ai_judgement": self._ai_judgement,
            "auto_decide": self._auto_decide,
            "auto_decide_max_severity": self._auto_decide_max_severity,
            "has_api_key": self._has_api_key,
            "judge_connected": self._judge is not None,
            "authorized_scopes": list(self.authorized_scopes()),
        }

    def advice_state(self) -> str:
        return advice_state(
            ai_judgement=self._ai_judgement,
            has_key=self._has_api_key,
            backend_ready=self._judge is not None,
            authorized=bool(self.authorized_scopes()),
        )

    def _auto_decide_live(self) -> bool:
        """L2 现在是否真的能自动放行（配置开 + 授权签了 + 有后端）。"""
        if self._auto_decide != AUTO_LOW_RISK_ONLY or self._judge is None:
            return False
        scopes = self._authorization.scopes if self._authorization else ()
        return AUTH_SCOPE_AUTO in scopes

    def _l1_ready(self) -> bool:
        return advice_state(
            ai_judgement=self._ai_judgement,
            has_key=self._has_api_key,
            backend_ready=self._judge is not None,
        ) == ADVICE_STATE_READY

    def undecided_count(self) -> int:
        with self._lock:
            return sum(1 for r in self._records.values() if r.is_pending())

    def snapshot(self) -> list[dict[str, Any]]:
        """pending_list 的数据：未决 + 已了结（未删的）。最新在前。"""
        with self._lock:
            self._prune_locked()
            rows = [r.as_dict() for r in self._records.values()]
        rows.sort(key=lambda row: row["created_at"], reverse=True)
        return rows

    def audit_rows(self) -> list[dict[str, Any]]:
        """audit_list 的数据。最新在前。"""
        with self._lock:
            rows = [entry.as_dict() for entry in self._audit]
        rows.reverse()
        return rows

    def audit_text(self) -> str:
        """可导出的纯文本审计。"""
        with self._lock:
            lines = [entry.as_text() for entry in self._audit]
        lines.reverse()
        header = f"# 同桌猫审批审计（共 {len(lines)} 条）"
        return "\n".join([header, *lines])

    def find(self, request_id: str) -> PendingApproval | None:
        with self._lock:
            for record in self._records.values():
                if record.request.request_id == request_id:
                    return record
        return None

    def stats(self) -> dict[str, int]:
        return {
            "pending": self.undecided_count(),
            "allow": self.stats_allow,
            "deny": self.stats_deny,
            "ai": self.stats_ai,
            "expired": self.stats_expired,
            "host_timeout": self.stats_host_timeout,
            "authorizations": self.stats_authorizations,
            "running": 1 if self.running() else 0,
        }


def _user_deny_message() -> str:
    """用户亲手点拒绝时，回给 Claude Code 的那句话。

    **必须是硬指令**（2026-11-21 真机实测后改）：原来只写"用户拒绝了这次调用"，
    Claude 把它理解成"命令失败"，于是换写法重试、换路径绕过，越拦越忙。
    所以这里明确说清三件事：这是用户的明确拒绝、不要换写法、不要绕过、
    等用户下一步指示。

    这句话会进 Claude 的上下文，是给它看的，不是给用户看的——
    写成人话它会当成建议，写成指令它才会停。
    """
    return ("同桌猫：用户在 N.E.K.O 里明确拒绝了这次调用。"
            "不要换写法重试，不要绕过，也不要尝试等价的其他命令。"
            "停下来，等用户的下一步指示。")


class _HookHandler(BaseHTTPRequestHandler):
    """只认 POST /hook/permission。其余一律 404，不猜意图。"""

    gateway: ApprovalGateway

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # 基类默认往 stderr 打访问日志；插件的日志走 self.logger，这里静音。
        return

    def do_GET(self) -> None:  # noqa: N802  标准库命名
        """探活。中继（hook_relay.py）在转发之前先打这里，1s 超时。

        只回答"活着"，不带任何状态——中继需要知道的就是这一个比特。
        """
        if self.path.split("?", 1)[0] != HEALTH_PATH:
            self._respond(404, {"ok": False, "reason": "not_found"})
            return
        self._respond(200, {"ok": True, "service": "agent_deskmate"})

    def do_POST(self) -> None:  # noqa: N802  标准库命名
        if self.path.split("?", 1)[0] != HOOK_PATH:
            self._respond(404, {"ok": False, "reason": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._respond(400, {"ok": False, "reason": "bad_length"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (ValueError, OSError):
            self._respond(400, {"ok": False, "reason": "bad_json"})
            return

        try:
            self.gateway.submit(payload, respond=self._deliver)
        except Exception as exc:  # noqa: BLE001  handler 崩了会带走整个服务
            self.gateway._logger.warning(  # noqa: SLF001  同类，允许
                "审批处理异常（{}）：{}", type(exc).__name__, exc,
            )
            self._respond(500, {"ok": False, "reason": "internal_error"})
            return

    def _deliver(self, result: dict | None) -> None:
        """把回填结果写回连接。``None`` = 插件没有决定 → 503 + 无 decision。"""
        if result is None:
            self._respond(503, no_decision_response(
                reason="no_decision",
                timeout_seconds=self.gateway.timeout_seconds,
            ))
            return
        self._respond(200, result)

    def _respond(self, status: int, body: dict) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
