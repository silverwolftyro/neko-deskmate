"""M0-lite-5 审批中继：Claude Code PermissionRequest hook → 本地 HTTP → 卡片 → 回填。

这是三条红线里第 2 条的执行体：**审批交互只能走 create_card + data-neko-action**。

设计要点（都来自官方资料或踩过的坑，不是拍的）：

1. **HTTP 服务跑在守护线程里**，不是 startup 里的 asyncio 任务。
   AGENTS.md 明确写着"startup 返回时它创建的任务会被取消"——任务活不到
   用户点按钮那一刻，线程可以。
2. **回填靠"还挂着的那条连接"，不靠 chain_id 推断身份。** hook 是"挂起等响应"
   语义：handler 线程一直阻塞到用户决定或超时，身份由连接本身保证。
   所以 chain_id 只用于显示，不参与回填（2026-11-21 归因结论 2）。
3. **pending 键是 (session_id, request_id)，request_id 由我们生成。**
   sessionId 在父子代理之间是同一个值（实测：子代理记录的 sessionId 恒等于父会话），
   拿它区分不了父子；但回填不需要区分，所以不影响正确性。
4. **超时默认拒绝。** 审批类交互宁可慢一步，也不能无人点击就一直挂着。
   Claude Code 的 hook 默认超时是 600s，所以我们的超时要明显小于它。
5. **hooks.json 必须带 ``onFailure:"block"``。** 否则我们的服务挂了、返回非 2xx
   或超时，Claude Code 按"非阻断错误"处理 → **直接放行**。这是 fail-open，
   对审批是灾难。

官方回填格式（03-调研证据/06 §3.3）：::

    {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                            "decision": {"behavior": "allow"|"deny",
                                         "updatedInput": {}, "updatedPermissions": []}}}

deny 可另带 ``message`` 与 ``interrupt``。``interrupt`` 默认 False——
拒绝这一次调用就好，不要顺手掐掉整个回合（那会让 Claude 中途闭嘴）。

本模块**不 import 任何 SDK**：状态机与 HTTP 服务是纯标准库，
才能在没宿主的环境里单测（允许/拒绝/超时/重复点击/连接断开/无会话）。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from pydantic import BaseModel, Field

# 决策取值。与卡片按钮一一对应。
APPROVE_ALLOW = "allow"
APPROVE_DENY = "deny"
DECISIONS = (APPROVE_ALLOW, APPROVE_DENY)

# 谁做的决定。timeout 是"没人做决定"，必须和用户明确点的拒绝区分开——
# 审计上这两件事完全不同。
DECIDED_BY_USER = "user"
DECIDED_BY_TIMEOUT = "timeout"

HOOK_PATH = "/hook/permission"
MAX_BODY_BYTES = 256 * 1024


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
    """一个挂起的审批。``event`` 由 handler 线程等，由 UI 动作或超时置位。"""

    def __init__(self, request: ApprovalRequest) -> None:
        self.request = request
        self.event = threading.Event()
        self.decision: str | None = None
        self.decided_by: str | None = None
        self.card_id: str | None = None
        self.card_announced = False

    @property
    def key(self) -> tuple[str, str]:
        return (self.request.session_id, self.request.request_id)

    def settled(self) -> bool:
        return self.decision is not None

    def needs_card(self) -> bool:
        return self.card_id is None

    def needs_announce(self) -> bool:
        return self.card_id is not None and self.settled() and not self.card_announced


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
    """按官方 schema 组装回填体。"""
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


# 审批按钮的 action id。写死在这里：HTML 里的 data-neko-action 与
# actions 字典的键必须一致，两边各写一遍字符串迟早对不上。
ACTION_APPROVE = "deskmate_approve"
ACTION_DENY = "deskmate_deny"
# 承载这两个按钮的入口 id。
ENTRY_DECIDE = "approval_decide"

CARD_CSS = (
    "section { padding: 12px; font-size: 13px; }"
    "strong { display: block; margin-bottom: 6px; }"
    "p { margin: 4px 0; }"
    ".loc { color: #888; font-size: 12px; }"
    ".hint { color: #aaa; font-size: 12px; margin-top: 8px; }"
    "button { margin: 8px 8px 0 0; padding: 6px 18px; }"
)


def build_card_html(*, subject: str, tool_summary: str, session_id: str,
                    working_directory: str | None, timeout_seconds: float) -> str:
    """审批卡片的 HTML。纯函数——卡片文案是验收项，必须能单测。

    ``subject`` 由调用方用归因语法生成（``describe_actor``），
    这样播报和卡片说的是同一套话。
    """
    location = f"会话 {session_id or '未知'}"
    if working_directory:
        location += f" @ {working_directory}"
    return (
        "<section>"
        "<strong>同桌猫 · 审批请求</strong>"
        f"<p>{_escape(subject)} 请求执行：{_escape(tool_summary)}</p>"
        f"<p class=\"loc\">{_escape(location)}</p>"
        f"<p class=\"hint\">允许后 Claude Code 继续执行；拒绝则中止这次调用"
        f"（不掐掉整个回合）。{timeout_seconds:g} 秒无人决定将默认拒绝。</p>"
        f"<button data-neko-action=\"{ACTION_APPROVE}\">允许</button>"
        f"<button data-neko-action=\"{ACTION_DENY}\">拒绝</button>"
        "</section>"
    )


def build_card_actions(request_id: str) -> dict[str, dict[str, Any]]:
    """两个按钮 → 同一个入口，用 args 区分决定。"""
    return {
        ACTION_APPROVE: {"entry": ENTRY_DECIDE,
                         "args": {"request_id": request_id, "decision": APPROVE_ALLOW}},
        ACTION_DENY: {"entry": ENTRY_DECIDE,
                      "args": {"request_id": request_id, "decision": APPROVE_DENY}},
    }


def _escape(text: str) -> str:
    """卡片 HTML 转义。工具参数是 Agent 产的，不能当可信 HTML 直接拼。"""
    import html

    return html.escape(str(text), quote=True)


class ApprovalGateway:
    """本地 HTTP 服务 + pending 状态机。线程安全，不依赖宿主。"""

    def __init__(self, logger: Any, *, port: int = 48921,
                 timeout_seconds: float = 60.0) -> None:
        self._logger = logger
        self._port = int(port)
        self._timeout_seconds = float(timeout_seconds)
        self._lock = threading.Lock()
        self._pending: dict[tuple[str, str], PendingApproval] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.stats_allow = 0
        self.stats_deny = 0
        self.stats_timeout = 0

    # ── 生命周期 ───────────────────────────────────────────────────────────

    @property
    def port(self) -> int:
        return self._port

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
        self._logger.info("审批服务就绪 http://127.0.0.1:{}{} 超时={}s",
                          self._port, HOOK_PATH, self._timeout_seconds)
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
            for pending in self._pending.values():
                # 关服务时还挂着的请求一律按拒绝收尾，别让 handler 线程挂死。
                if pending.decision is None:
                    pending.decision = APPROVE_DENY
                    pending.decided_by = DECIDED_BY_TIMEOUT
                pending.event.set()
            self._pending.clear()
        self._logger.info("审批服务已关闭")

    # ── hook 侧 ────────────────────────────────────────────────────────────

    def submit(self, payload: Any) -> dict:
        """HTTP handler 调入口：登记 → 等卡片 → 阻塞到决定或超时 → 回填。

        这个函数会**阻塞调用它的线程**（每个 hook 请求一个线程），
        所以绝不能在主循环里调。
        """
        request = build_request(payload)
        pending = PendingApproval(request)
        with self._lock:
            self._pending[pending.key] = pending
        self._logger.info(
            "收到审批请求 {}（会话 {} 工具 {}）", request.request_id,
            request.session_id or "无", request.tool_name or "未知",
        )

        pending.event.wait(timeout=self._timeout_seconds)

        with self._lock:
            self._pending.pop(pending.key, None)
            if pending.decision is None:
                # wait 超时没人决定。也可能是 resolve 与超时同时发生——
                # 锁保证了这里只会有一个赢家。
                pending.decision = APPROVE_DENY
                pending.decided_by = DECIDED_BY_TIMEOUT
                self.stats_timeout += 1
                self._logger.warning(
                    "审批超时（{}s），按拒绝回填：请求 {}",
                    self._timeout_seconds, request.request_id,
                )
            elif pending.decision == APPROVE_ALLOW:
                self.stats_allow += 1
            else:
                self.stats_deny += 1

        # message 是 deny 专属字段（官方 schema：deny 可带 message / interrupt）。
        # allow 时带上它没有意义，而且会写出"允许 + 说你拒绝了"这种自相矛盾的
        # 回填——端到端跑第一遍时就撞见了这个。
        if pending.decision == APPROVE_DENY:
            if pending.decided_by == DECIDED_BY_TIMEOUT:
                message = f"同桌猫：{self._timeout_seconds:g} 秒内无人决定，默认拒绝"
            else:
                message = "同桌猫：用户在 N.E.K.O 里拒绝了这次调用"
        else:
            message = ""
        return hook_response(pending.decision, message=message)

    # ── 插件侧（timer / UI 动作） ──────────────────────────────────────────

    def take_new(self) -> list[PendingApproval]:
        """取走还没建卡片的请求（取走即标记，避免重复建卡）。"""
        with self._lock:
            fresh = [p for p in self._pending.values() if p.needs_card()]
            for pending in fresh:
                pending.card_id = ""      # 占位：表示"已取走，正在建"
            return fresh

    def attach_card(self, request_id: str, card_id: str) -> None:
        with self._lock:
            for pending in self._pending.values():
                if pending.request.request_id == request_id:
                    pending.card_id = card_id
                    return

    def settle_pending_cards(self) -> list[PendingApproval]:
        """已建卡且已有决定、但还没在卡片上显示的请求。"""
        with self._lock:
            return [p for p in self._pending.values() if p.needs_announce()]

    def mark_announced(self, request_id: str) -> None:
        with self._lock:
            for pending in self._pending.values():
                if pending.request.request_id == request_id:
                    pending.card_announced = True
                    return

    def resolve(self, request_id: str, decision: str, *,
                decided_by: str = DECIDED_BY_USER) -> str:
        """UI 按钮的落点。返回 ``resolved`` / ``already-resolved`` / ``unknown``。

        **幂等**：重复点击（用户连点、多窗口各点一次）只有第一次算数。
        宿主的按钮代理本来就会忽略执行中按钮的重复点击，但那是 UI 层的
        尽力而为，业务层必须自己保证——跨窗口的重复它管不了。
        """
        if decision not in DECISIONS:
            return "unknown"
        with self._lock:
            for pending in self._pending.values():
                if pending.request.request_id != request_id:
                    continue
                if pending.decision is not None:
                    return "already-resolved"
                pending.decision = decision
                pending.decided_by = decided_by
                pending.event.set()
                self._logger.info(
                    "审批已决定（{} by {}）：请求 {}",
                    decision, decided_by, request_id,
                )
                return "resolved"
        return "unknown"

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def stats(self) -> dict[str, int]:
        return {
            "pending": self.pending_count(),
            "allow": self.stats_allow,
            "deny": self.stats_deny,
            "timeout": self.stats_timeout,
            "running": 1 if self.running() else 0,
        }


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
            result = self.gateway.submit(payload)
        except Exception as exc:  # noqa: BLE001  handler 崩了会带走整个服务
            self.gateway._logger.warning(  # noqa: SLF001  同类，允许
                "审批处理异常（{}）：{}", type(exc).__name__, exc,
            )
            self._respond(500, {"ok": False, "reason": "internal_error"})
            return

        # 客户端可能在等我们回填之前就断开（用户 Ctrl-C 了 Claude）。
        # 写失败只是这次回填丢了，不能让它冒出去把服务线程炸掉。
        try:
            self._respond(200, result)
        except (BrokenPipeError, ConnectionResetError):
            self.gateway._logger.info(  # noqa: SLF001  同类，允许
                "审批已回填但连接已断开（客户端提前离开）",
            )

    def _respond(self, status: int, body: dict) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
