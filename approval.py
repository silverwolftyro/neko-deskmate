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
用户没点 → 若 L2 门控通过且有判断后端，则 AI 决定（``decided_by="ai"``）；
否则只把 pending 标成 ``expired``，**不发任何 decision**——用 503 + 无 decision
的响应体告诉宿主"插件没有决定"。真正拦住这次调用的是宿主自己的策略
（``hooks.json`` 里的 ``onFailure:"block"``），不是我们的判断。

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
from typing import Any

from pydantic import BaseModel, Field

# 决策取值。只有用户点按钮才能产生这两个值。
APPROVE_ALLOW = "allow"
APPROVE_DENY = "deny"
DECISIONS = (APPROVE_ALLOW, APPROVE_DENY)

# decided_by 的取值。
DECIDED_BY_USER = "user"              # 用户点了 → 回填
DECIDED_BY_EXPIRED = "expired"        # 我们先到期 → **不回填**
DECIDED_BY_HOST_TIMEOUT = "host_timeout"  # 连接被宿主断开 → **不回填**
DECIDED_BY_AI = "ai"                  # L2 自动决定 → 回填，但必须可审计
# 下面这个只在用户显式打开 auto_deny_on_timeout 时才可能出现。
# 默认配置下永远走不到；开着的时候必须在日志和卡片上都注明是配置干的。
DECIDED_BY_CONFIG = "config"

HOOK_PATH = "/hook/permission"
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

# 严重度阶梯。与 rules.py 的取值一致，这里只用来比大小。
SEVERITY_ORDER = {"info": 0, "notice": 1, "warn": 2, "critical": 3}

# **永远不进 L2** 的规则（规格 §2 硬规则 3）。删除类先挡住；
# 危险写等 M0-lite-3b 落地后加进来——那时这条集合要跟着扩，并有测试守着。
L2_EXCLUDED_RULES = frozenset({"delete"})

# 风险判定用的默认档：L0 规则没命中就当低风险。
DEFAULT_RISK_SEVERITY = "notice"


def assess_risk(tool_name: str, tool_input: Any) -> tuple[str, str]:
    """评估一次工具调用的风险。纯函数，不接模型。

    返回 ``(severity, rule)``。5a 阶段只跑 L0 规则（删除类）——
    没命中就按 ``notice``（低风险）算，**不猜**。模型判定是 5b 的事。

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
            return "critical", "delete"
    return DEFAULT_RISK_SEVERITY, ""


def auto_decide_gate(*, rule: str, severity: str, ai_judgement: bool,
                     has_key: bool, auto_decide: str,
                     max_severity: str) -> tuple[bool, str]:
    """L2 门控。返回 ``(是否放行, 原因)``。纯函数，便于单测。

    四条全中才放行，缺任何一条都给出**具体原因**——原因要能写进审计，
    不能只回一个 False 让人猜。
    """
    if auto_decide != AUTO_LOW_RISK_ONLY:
        return False, f"auto_decide={auto_decide!r} 未开启"
    if not ai_judgement:
        # 不能跳级：没开 L1 就没有解释能力，此时的自动决定是黑箱代办。
        return False, "ai_judgement 未开启（L2 必须建立在 L1 之上）"
    if not has_key:
        return False, "未配置 api_key（没 key 不判断）"
    if rule in L2_EXCLUDED_RULES:
        return False, f"{rule} 类动作永不进入 L2（无论阈值怎么设）"
    if SEVERITY_ORDER.get(severity, 99) > SEVERITY_ORDER.get(max_severity, 0):
        return False, f"风险 {severity} 超过阈值 {max_severity}，回落为 L1"
    return True, "L2 门控通过"


def advice_state(*, ai_judgement: bool, has_key: bool, backend_ready: bool) -> str:
    """卡片上"建议"那块显示成什么样。三种状态，都不许假装成功。"""
    if not ai_judgement:
        return "disabled"
    if not has_key:
        return "no_key"
    if not backend_ready:
        return "pending_backend"
    return "ready"


ADVICE_TEXT = {
    "disabled": "未开启 AI 建议（ai_judgement = false）",
    "no_key": "未配置 key，无法给出建议",
    "pending_backend": "已配置 key，建议能力待接入（5b）",
}


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
    """

    def __init__(self, request: ApprovalRequest) -> None:
        self.request = request
        self.event = threading.Event()
        self.decision: str | None = None       # allow / deny，仅用户或 L2 能填
        self.decided_by: str | None = None
        self.card_id: str | None = None
        self.card_announced = False
        # 风险与规则命中：建请求时就由 L0 定好，供 L2 门控与审计用。
        self.severity: str = DEFAULT_RISK_SEVERITY
        self.rule: str = ""
        # L2 决定时填。
        self.judge_model: str = ""
        self.judge_reason: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.request.session_id, self.request.request_id)

    def is_pending(self) -> bool:
        return self.decided_by is None

    def settled(self) -> bool:
        return self.decided_by is not None

    def needs_card(self) -> bool:
        return self.card_id is None

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
            "decision": self.decision,
            "decided_by": self.decided_by,
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
    """按官方 schema 组装回填体。只有用户（或显式开 auto_deny 时的配置）能走到这。"""
    decision: dict[str, Any] = {
        "behavior": behavior,
        "updatedInput": {},
        "updatedPermissions": [],
    }
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
    """
    return {
        "ok": False,
        "submitted": False,
        "reason": reason,
        "message": (
            f"同桌猫：{timeout_seconds:g} 秒内无人决定，插件不代你决定。"
            "本次调用交由宿主按 onFailure 策略处理。"
        ),
    }


# 审批按钮的 action id。写死在这里：HTML 里的 data-neko-action 与
# actions 字典的键必须一致，两边各写一遍字符串迟早对不上。
ACTION_APPROVE = "deskmate_approve"
ACTION_DENY = "deskmate_deny"
ACTION_EXPLAIN = "deskmate_explain"
# 承载这三个按钮的入口 id。
ENTRY_DECIDE = "approval_decide"
ENTRY_EXPLAIN = "approval_explain"

CARD_CSS = (
    "section { padding: 12px; font-size: 13px; }"
    "strong { display: block; margin-bottom: 6px; }"
    "p { margin: 4px 0; }"
    ".loc { color: #888; font-size: 12px; }"
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
                    advice_state: str = "disabled",
                    suggestion: str = "", reason: str = "", basis: str = "") -> str:
    """审批卡片的 HTML。纯函数——卡片文案是验收项，必须能单测。

    四块内容（规格 §4）：标题/来源/命令/会话与 cwd、建议+理由+依据、三个按钮、
    超时说明。``subject`` 由调用方用归因语法生成（``describe_actor``），
    这样播报和卡片说的是同一套话。
    """
    location = f"会话 {session_id or '未知'}"
    if working_directory:
        location += f" @ {working_directory}"

    # 建议块：没 key / 没开开关时降级成一行说明，不编内容。
    if advice_state == "ready":
        advice_lines = [
            f"<p><strong>建议：{_escape(suggestion)}</strong></p>",
            f"<p>理由：{_escape(reason)}</p>",
            f"<p>依据：{_escape(basis)}</p>",
        ]
    else:
        advice_lines = [f"<strong>{_escape(ADVICE_TEXT[advice_state])}</strong>"]

    return (
        "<section>"
        "<strong>同桌猫 · 审批请求</strong>"
        f"<p>{_escape(subject)} 请求执行：{_escape(tool_summary)}</p>"
        f"<p class=\"loc\">{_escape(location)}</p>"
        + "".join(advice_lines)
        + f"<p class=\"hint\">允许后 Claude Code 继续执行；拒绝则中止这次调用"
          f"（不掐掉整个回合）。{_minutes(timeout_seconds)}后无人决定，"
          "宿主会自行拦截，届时这条视为未决定——插件不代你判断。</p>"
          f"<button data-neko-action=\"{ACTION_APPROVE}\">允许</button>"
          f"<button data-neko-action=\"{ACTION_DENY}\">拒绝</button>"
          f"<button data-neko-action=\"{ACTION_EXPLAIN}\">这是什么</button>"
          "</section>"
    )


def explain_request(*, tool_name: str, tool_summary: str,
                    working_directory: str | None, rule: str,
                    severity: str) -> dict[str, str]:
    """「这是什么」按钮的落点：L0 级解释，只说事实，不猜意图。

    5a 阶段不接模型，所以这里只讲三件能讲准的事：这条请求是什么、
    L0 规则对它有什么判断、以及插件不会替你做决定。
    模型生成的自然语言解释是 5b 的事。
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

    return {
        "title": "这是什么",
        "what": f"Claude Code 请求执行 {tool_name or '某个工具'}：{tool_summary}",
        "verdict": verdict,
        "extra": extra,
        "location": (f"会话工作目录 {working_directory}" if working_directory
                     else "会话工作目录未知"),
        "note": "以上是规则层能讲准的事实；接模型后的自然语言解释在 5b。",
    }


def build_explain_html(explanation: dict[str, str]) -> str:
    """把 explain_request 的结果渲染成卡片用的 HTML。"""
    return (
        "<section>"
        f"<strong>{_escape(explanation['title'])}</strong>"
        f"<p>{_escape(explanation['what'])}</p>"
        f"<p>{_escape(explanation['verdict'])}</p>"
        f"<p>{_escape(explanation['extra'])}</p>"
        f"<p class=\"loc\">{_escape(explanation['location'])}</p>"
        f"<p class=\"hint\">{_escape(explanation['note'])}</p>"
        "</section>"
    )


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


def build_result_html(note: str) -> str:
    """结果态卡片：只留一句话，按钮已清掉。"""
    return f"<section><p>{_escape(note)}</p></section>"


class AuditRecord(BaseModel):
    """一次决定的审计行。每次决定都写一条，含 L2 的理由与模型。"""

    ts: float = Field(default_factory=time.time)
    request_id: str = ""
    session_id: str = ""
    tool_summary: str = ""
    decision: str | None = Field(default=None, description="allow / deny；None = 没发决定")
    decided_by: str = Field(description="user / ai / expired / host_timeout")
    model: str = Field(default="", description="给出该决定的模型；空 = 非模型决定")
    reason: str = Field(default="", description="一句话理由")

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "time": _iso(self.ts),
            "request_id": self.request_id,
            "session_id": self.session_id,
            "tool_summary": self.tool_summary,
            "decision": self.decision,
            "decided_by": self.decided_by,
            "model": self.model or "-",
            "reason": self.reason,
        }

    def as_text(self) -> str:
        return (f"{_iso(self.ts)} | {self.decision or '无决定'} | by={self.decided_by} | "
                f"model={self.model or '-'} | req={self.request_id} | "
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
                 judge: Any = None) -> None:
        self._logger = logger
        self._port = int(port)
        self._timeout_seconds = float(timeout_seconds)
        self._retention_seconds = float(retention_seconds)
        # 门控开关：**只能由用户写配置**，运行期不可改（见 policy_snapshot 的测试）。
        self._ai_judgement = bool(ai_judgement)
        self._auto_decide = str(auto_decide)
        self._auto_decide_max_severity = str(auto_decide_max_severity)
        self._has_api_key = bool(has_api_key)
        self._audit_path = audit_path
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
        self._logger.info(
            "审批服务就绪 http://127.0.0.1:{}{} 到期={}s 保留={}s "
            "L1建议={} L2自动={} 判断后端={}",
            self._port, HOOK_PATH, self._timeout_seconds, self._retention_seconds,
            "开" if self._l1_ready() else "关",
            self._auto_decide,
            "已接入" if self._judge is not None else "未接入（5b）",
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
                    self._count_locked(record)
                    self._write_audit_locked(record)
                record.event.set()
        self._logger.info("审批服务已关闭")

    # ── hook 侧 ────────────────────────────────────────────────────────────

    def submit(self, payload: Any, *, respond: Any = None) -> dict | None:
        """HTTP handler 调入口：登记 → 等用户 → 回填或**不回填**。

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
        record.severity, record.rule = assess_risk(
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

        record.event.wait(timeout=self._timeout_seconds)

        with self._lock:
            if record.decided_by is None:
                self._settle_at_expiry(record)

        # 先投递再记账：投递失败要把 decided_by 改成 host_timeout，
        # 而审计必须反映**最终**状态——先写审计再投递的话，
        # 审计里会留着一条"已允许"，实际却没送到。
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
            self._count_locked(record)
            self._write_audit_locked(record)
            # **不删记录**：过期条目要留在 _records 里供 pending_list 事后查，
            # 由 _prune_locked 按保留期统一清理。删在这里就等于"过期即消失"，
            # 而那正是审计最需要看到的几条。
        return result

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
        return result

    def _response_for(self, record: PendingApproval) -> dict | None:
        """记录 → 回填体。``None`` = 没有决定，调用方回 503。"""
        if record.decided_by == DECIDED_BY_EXPIRED:
            return None
        if record.decided_by == DECIDED_BY_AI:
            return hook_response(
                record.decision or APPROVE_DENY,
                message=(f"同桌猫：由 L2 自动决定（{record.judge_reason or '低风险'}），"
                         "非你亲手点的"),
            )
        # message 是 deny 专属字段：allow 时带上"你拒绝了"会自相矛盾
        # （这个 bug 在端到端跑第一遍时撞见过一次，重构后又差点回来，
        #  所以有单测盯着）。
        if record.decision == APPROVE_DENY:
            return hook_response(APPROVE_DENY, message=_user_deny_message())
        return hook_response(APPROVE_ALLOW)

    def _settle_at_expiry(self, record: PendingApproval) -> None:
        """到期时的了结。调用方必须已持有锁。

        顺序固定：先看 L2 门控，通过**且**有判断后端才让 AI 决定；
        否则只标 ``expired``，**绝不发任何 decision**。
        统计计数统一交给 ``_count_locked``，这里不碰，避免两处各加一次。
        """
        eligible, why = auto_decide_gate(
            rule=record.rule, severity=record.severity,
            ai_judgement=self._ai_judgement, has_key=self._has_api_key,
            auto_decide=self._auto_decide,
            max_severity=self._auto_decide_max_severity,
        )
        if not eligible:
            record.decided_by = DECIDED_BY_EXPIRED
            self._logger.warning(
                "审批到期（{}s）无人决定，插件不代你决定，请求 {} 标记 expired 且不回填"
                "（L2 未放行：{}）",
                self._timeout_seconds, record.request.request_id, why,
            )
            return
        if self._judge is None:
            # 门控算得过，但 5a 没接判断后端。**不因为门控通过就自己拍一个**——
            # 那是插件在替用户判断。明确降级，留日志说明原因。
            record.decided_by = DECIDED_BY_EXPIRED
            self._logger.warning(
                "请求 {} 的 L2 门控通过（{}），但判断后端未接入（5b），"
                "降级为 expired 且不回填",
                record.request.request_id, why,
            )
            return

        try:
            verdict = self._judge(record)          # 5b 接模型；异常按 expired 处理
        except Exception as exc:  # noqa: BLE001  后端炸了不能炸掉审批
            record.decided_by = DECIDED_BY_EXPIRED
            self._logger.warning(
                "请求 {} 的 L2 判断后端异常（{}），降级为 expired 且不回填",
                record.request.request_id, type(exc).__name__,
            )
            return
        if not isinstance(verdict, dict) or verdict.get("decision") not in DECISIONS:
            record.decided_by = DECIDED_BY_EXPIRED
            self._logger.warning(
                "请求 {} 的 L2 判断没给出有效决定，降级为 expired 且不回填",
                record.request.request_id,
            )
            return
        record.decision = verdict["decision"]
        record.decided_by = DECIDED_BY_AI
        record.judge_model = str(verdict.get("model") or "")
        record.judge_reason = str(verdict.get("reason") or "")
        self._logger.info(
            "请求 {} 由 L2 自动决定（{}，模型 {}，理由 {}）",
            record.request.request_id, record.decision,
            record.judge_model or "-", record.judge_reason,
        )

    def _count_locked(self, record: PendingApproval) -> None:
        """按最终 decided_by 记一次数。调用方必须已持有锁。

        集中在一处：``_settle_at_expiry`` 里也加过计数，结果 stats_ai 永远是 0——
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

    def _write_audit_locked(self, record: PendingApproval) -> None:
        """每次决定都写一条审计。调用方必须已持有锁。"""
        entry = AuditRecord(
            request_id=record.request.request_id,
            session_id=record.request.session_id,
            tool_summary=record.request.tool_summary,
            decision=record.decision,
            decided_by=record.decided_by or DECIDED_BY_EXPIRED,
            model=getattr(record, "judge_model", ""),
            reason=self._audit_reason(record),
        )
        self._audit.append(entry)
        if len(self._audit) > AUDIT_MEMORY_LIMIT:
            del self._audit[: len(self._audit) - AUDIT_MEMORY_LIMIT]
        self._append_audit_file(entry)

    def _audit_reason(self, record: PendingApproval) -> str:
        if record.decided_by == DECIDED_BY_USER:
            return "用户在 N.E.K.O 里点了按钮"
        if record.decided_by == DECIDED_BY_AI:
            return getattr(record, "judge_reason", "") or "L2 自动决定"
        if record.decided_by == DECIDED_BY_HOST_TIMEOUT:
            return "回填时连接已被宿主断开，决定未送达"
        return "到期无人决定，插件不代判，交由宿主 onFailure 策略处理"

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

    def note_host_timeout(self, request_id: str) -> None:
        """投递失败：决定可能做了但没送到宿主。

        把 ``decided_by`` 覆盖成 ``host_timeout``——从宿主的视角看，
        这条请求**没有发生过任何决定**（它没收到），所以审计必须这么记。
        原本的决定留在 ``decision`` 字段里，人和审计都能看出"用户选了允许但没送达"。
        计数交给 ``_count_locked``，这里只改状态，避免两处各加一次。
        """
        with self._lock:
            for record in self._records.values():
                if record.request.request_id != request_id:
                    continue
                if record.decided_by != DECIDED_BY_HOST_TIMEOUT:
                    record.decided_by = DECIDED_BY_HOST_TIMEOUT
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
        """当前门控开关的快照。

        **只读**：这些值只能来自用户配置，运行期没有任何方法能改它们。
        测试靠这一点守住规格 §2 硬规则 6「AI 不能自己给自己授权」。
        """
        return {
            "ai_judgement": self._ai_judgement,
            "auto_decide": self._auto_decide,
            "auto_decide_max_severity": self._auto_decide_max_severity,
            "has_api_key": self._has_api_key,
            "judge_connected": self._judge is not None,
        }

    def advice_state(self) -> str:
        return advice_state(
            ai_judgement=self._ai_judgement,
            has_key=self._has_api_key,
            backend_ready=self._judge is not None,
        )

    def _l1_ready(self) -> bool:
        return advice_state(
            ai_judgement=self._ai_judgement,
            has_key=self._has_api_key,
            backend_ready=self._judge is not None,
        ) == "ready"

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
            "running": 1 if self.running() else 0,
        }


def _user_deny_message() -> str:
    return "同桌猫：用户在 N.E.K.O 里拒绝了这次调用"


class _HookHandler(BaseHTTPRequestHandler):
    """只认 POST /hook/permission。其余一律 404，不猜意图。"""

    gateway: ApprovalGateway

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # 基类默认往 stderr 打访问日志；插件的日志走 self.logger，这里静音。
        return

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
