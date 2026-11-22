"""A 批 A3：中继只转发**合法应答**。

要防的事故：插件没启动时，端口上住的可能是别的程序。老中继会把完整命令
发给它，还会把它返回的任意内容（包括一个长得像 allow 的对象）原样转交
Claude Code——那是把审批权交给一个来路不明的对端。

所以这里有两道门：

1. **转发前核对身份**：``/health`` 必须回服务标识 + token 前缀；
2. **转发后校验形状**：应答必须是合法的 decision schema，否则整个丢掉，
   按"没有决定"处理（输出 ``{}``），交还原生权限流程。

原则：**宁可多弹一次原生权限框，不能把一个非法应答当成批准。**
"""

from __future__ import annotations

import contextlib
import io
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from plugin.plugins.agent_deskmate import hook_relay
from plugin.plugins.agent_deskmate.approval import (
    HEALTH_SERVICE,
    HEALTH_TOKEN,
    MAX_INFLIGHT,
)

GOOD_DECISION = json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PermissionRequest",
        "decision": {"behavior": "allow"},
    },
}).encode("utf-8")

HOOK_BODY = json.dumps({
    "hook_event_name": "PermissionRequest",
    "session_id": "s",
    "tool_name": "Bash",
    "tool_input": {"command": "rm -rf build/"},
    "cwd": "D:\\",
}).encode("utf-8")

# "没配 health"的哨兵。不能用 None——None 本身就是一种要测的应答
# （服务答不出 /health），会被默认值悄悄吃掉。
_UNSET = object()


class _FakePlugin:
    """一个可配置的假对端：想让它健康就健康，想让它说胡话就说胡话。

    ``health`` 是 ``/health`` 的回包；``reply`` 是 ``/hook/permission`` 的回包
    （``None`` = 不发这个路径，用来断言"根本没转发"）。
    """

    def __init__(self, *, health: Any = _UNSET, reply: Any = GOOD_DECISION,
                 delay: float = 0.0) -> None:
        self.health = ({"ok": True, "service": HEALTH_SERVICE,
                        "token": HEALTH_TOKEN} if health is _UNSET else health)
        self.reply = reply
        self.delay = delay
        self.seen: list[str] = []
        self.bodies: list[bytes] = []

    def serve(self) -> tuple[ThreadingHTTPServer, int]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:  # noqa: A002
                return

            def _json(self, payload: Any, status: int = 200) -> None:
                body = (payload if isinstance(payload, bytes)
                        else json.dumps(payload, ensure_ascii=False).encode())
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802  标准库命名
                outer.seen.append(self.path)
                if self.path.split("?", 1)[0] != hook_relay.HEALTH_PATH:
                    self._json({"ok": False}, 404)
                    return
                if outer.health is None:
                    self._json({"ok": False}, 503)
                    return
                self._json(outer.health)

            def do_POST(self) -> None:  # noqa: N802  标准库命名
                outer.seen.append(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                outer.bodies.append(self.rfile.read(length))
                if outer.reply is None:
                    self._json({"ok": False}, 503)
                    return
                if outer.delay:
                    import time
                    time.sleep(outer.delay)
                self._json(outer.reply)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, int(server.server_address[1])


def _run_forward(fake: _FakePlugin) -> dict:
    """在进程内跑一次中继的转发，返回它打印出来的 JSON。"""
    server, port = fake.serve()
    try:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            hook_relay._forward(str(port), HOOK_BODY)  # noqa: SLF001
        return json.loads(buffer.getvalue() or "{}")
    finally:
        server.shutdown()
        server.server_close()


def _alive(fake: _FakePlugin) -> bool:
    server, port = fake.serve()
    try:
        return hook_relay._alive(str(port))  # noqa: SLF001
    finally:
        server.shutdown()
        server.server_close()


# ── 1. 身份核对：端口上住的不一定是我们的插件 ──────────────────────────────

def test_relay_accepts_our_own_health_payload() -> None:
    assert _alive(_FakePlugin()) is True


@pytest.mark.parametrize("health", [
    {"ok": True},                                          # 老格式：只有 ok
    {"ok": True, "service": "agent_deskmate"},              # 没有 token
    {"ok": True, "service": "someone-else",
     "token": "deskmate-approval-v1"},                      # 服务名不对
    {"ok": True, "service": HEALTH_SERVICE,
     "token": "other-approval-v1"},                         # token 前缀不对
    {"ok": True, "service": HEALTH_SERVICE, "token": 7},     # token 不是字符串
    "not a dict",
    None,
])
def test_relay_refuses_to_forward_to_a_stranger(health: Any) -> None:
    """对面答不出身份 → 当它不存在，**不转发**。

    这条是整个 A3 的地基：只答 ``{"ok": true}`` 证明不了"它不会把命令
    送给别人"。老格式（只有 ok）也要拒——那是升级前的旧插件，
    它没有应答校验，把命令交给它等于把命令交给任意程序。
    """
    fake = _FakePlugin(health=health)
    assert _alive(fake) is False


def test_a_stranger_never_receives_the_command() -> None:
    """拒了一次，就要**一次都不发**——包括那条完整命令。"""
    fake = _FakePlugin(health={"ok": True})
    assert _alive(fake) is False
    server, port = fake.serve()
    try:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            hook_relay.main([str(port), HOOK_BODY.decode("utf-8")])
        assert json.loads(buffer.getvalue()) == {}
    finally:
        server.shutdown()
        server.server_close()
    assert "/hook/permission" not in fake.seen


# ── 2. 应答形状：不合法就整个丢掉 ──────────────────────────────────────────

def test_a_valid_decision_is_passed_through() -> None:
    assert _run_forward(_FakePlugin())["hookSpecificOutput"]["decision"]["behavior"] == "allow"


@pytest.mark.parametrize("reply", [
    b'{"allow": true}',                                    # 别家协议的应答
    b'{}',                                                 # 空对象
    b'not json at all',                                    # 非 JSON
    b'[]',                                                 # JSON 但不是对象
    json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse",                    # 事件名不对
        "decision": {"behavior": "allow"}}}).encode(),
    json.dumps({"hookSpecificOutput": {
        "hookEventName": "PermissionRequest"}}).encode(),   # 没有 decision
    json.dumps({"hookSpecificOutput": {
        "hookEventName": "PermissionRequest",
        "decision": {"behavior": "maybe"}}}).encode(),      # behavior 不在枚举里
])
def test_an_invalid_answer_is_discarded(reply: bytes) -> None:
    """形状不对 → 输出空对象，**不把原始应答交出去**。"""
    assert _run_forward(_FakePlugin(reply=reply)) == {}


@pytest.mark.parametrize("size,why", [
    (hook_relay.MAX_RESPONSE_BYTES + 1, "刚超上限"),
    (hook_relay.MAX_RESPONSE_BYTES * 4, "远超上限"),
])
def test_an_over_long_answer_is_discarded(size: int, why: str) -> None:
    """1 MiB 以上的应答：合法的 decision 只有几百字节。

    这种体量只能说明对面在塞别的东西。**不读完就丢**——
    读下去既浪费时间，又把一大段来路不明的内容吃进内存。
    """
    assert _run_forward(_FakePlugin(reply=b"y" * size)) == {}, why


def test_extra_fields_in_a_valid_answer_are_tolerated() -> None:
    """合法应答多带字段 → **照样放行**。

    校验只查必须存在的那几项，不做白名单：将来官方 schema 加了字段，
    老中继不该因此把一次正确的批准丢掉（那是另一种静默失败——
    卡片不弹、原生框也不弹，用户只看到请求挂住）。
    """
    reply = json.dumps({"hookSpecificOutput": {
        "hookEventName": "PermissionRequest",
        "decision": {"behavior": "deny", "message": "用户拒绝",
                     "interrupt": False, "futureField": [1, 2]}}}).encode()
    body = _run_forward(_FakePlugin(reply=reply))
    assert body["hookSpecificOutput"]["decision"]["behavior"] == "deny"


def test_an_over_long_answer_is_not_forwarded() -> None:
    """1 MiB 以上的应答：合法的 decision 只有几百字节。"""
    fake = _FakePlugin(reply=b"y" * (hook_relay.MAX_RESPONSE_BYTES + 1))
    assert _run_forward(fake) == {}


def test_a_slow_answer_is_treated_as_no_answer(monkeypatch: Any) -> None:
    """慢响应按超时处理——不能让用户干等一个说不清的对端。"""
    monkeypatch.setattr(hook_relay, "FORWARD_TIMEOUT", 0.4)
    fake = _FakePlugin(delay=3.0)
    assert _run_forward(fake) == {}


# ── 3. 两边常量必须一致 ───────────────────────────────────────────────────

def test_relay_and_gateway_agree_on_the_health_identity() -> None:
    """中继认的服务标识/token 前缀，必须是网关回的那一套。

    两边各写一份（中继不能 import 插件包）。写歪了的症状是
    "插件明明活着，却被当成陌生人"——静默且难查。
    """
    assert hook_relay.HEALTH_SERVICE == HEALTH_SERVICE
    assert HEALTH_TOKEN.startswith(hook_relay.HEALTH_TOKEN_PREFIX)
    assert hook_relay.HOOK_EVENT_NAME == "PermissionRequest"
    assert hook_relay.MAX_RESPONSE_BYTES == 1024 * 1024


def test_health_endpoint_answers_with_its_identity() -> None:
    """网关的 /health 必须带服务标识与 token，中继才认得出它。"""
    from plugin.plugins.agent_deskmate.approval import ApprovalGateway

    class _Log:
        def info(self, *_a: Any) -> None:
            return

        def warning(self, *_a: Any) -> None:
            return

        def error(self, *_a: Any) -> None:
            return

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    gateway = ApprovalGateway(_Log(), port=port, timeout_seconds=0.5)
    assert gateway.start()
    try:
        import urllib.request

        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{hook_relay.HEALTH_PATH}", timeout=2
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["service"] == HEALTH_SERVICE
        assert payload["token"].startswith(hook_relay.HEALTH_TOKEN_PREFIX)
        # 中继用它探活，必须认得过
        assert hook_relay._alive(str(port)) is True  # noqa: SLF001
    finally:
        gateway.stop()


# ── 4. 挂起请求上限 ───────────────────────────────────────────────────────

def test_inflight_cap_protects_the_thread_pool() -> None:
    """同时挂起的审批请求有上限（A3 第 3 条）。

    每个请求占一个 handler 线程并阻塞到用户决定或到期（默认 570s），
    不设上限的话一个乱发的客户端就能把线程吃光。
    """
    from plugin.plugins.agent_deskmate.approval import ApprovalGateway

    class _Log:
        def info(self, *_a: Any) -> None:
            return

        def warning(self, *_a: Any) -> None:
            return

        def error(self, *_a: Any) -> None:
            return

    gateway = ApprovalGateway(_Log(), port=48999, timeout_seconds=30.0)
    assert gateway.inflight == 0
    for index in range(MAX_INFLIGHT):
        assert gateway.acquire_slot() is True, f"第 {index} 个额度都没拿到"
        assert gateway.inflight == index + 1
    # 满了：再多一个也进不来
    assert gateway.acquire_slot() is False
    assert gateway.inflight == MAX_INFLIGHT
    # 还额度之后又能进
    gateway.release_slot()
    assert gateway.inflight == MAX_INFLIGHT - 1
    assert gateway.acquire_slot() is True


def test_releasing_a_slot_never_goes_negative() -> None:
    """重复释放只会把额度越放越多——那是限形同虚设，所以夹在 0。"""
    from plugin.plugins.agent_deskmate.approval import ApprovalGateway

    class _Log:
        def info(self, *_a: Any) -> None:
            return

        def warning(self, *_a: Any) -> None:
            return

        def error(self, *_a: Any) -> None:
            return

    gateway = ApprovalGateway(_Log(), port=48999, timeout_seconds=30.0)
    gateway.release_slot()
    gateway.release_slot()
    assert gateway.inflight == 0
    assert gateway.acquire_slot() is True


def test_health_still_answers_when_the_gateway_is_saturated() -> None:
    """探活端点**不占额度**：插件一忙中继就以为它死了，比忙更糟。"""
    from plugin.plugins.agent_deskmate.approval import ApprovalGateway

    class _Log:
        def info(self, *_a: Any) -> None:
            return

        def warning(self, *_a: Any) -> None:
            return

        def error(self, *_a: Any) -> None:
            return

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    gateway = ApprovalGateway(_Log(), port=port, timeout_seconds=30.0)
    assert gateway.start()
    try:
        for _ in range(MAX_INFLIGHT):
            assert gateway.acquire_slot() is True
        import urllib.request

        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{hook_relay.HEALTH_PATH}", timeout=2
        ) as response:
            assert response.status == 200
        # 满载时新请求拿到 503 busy，中继据此交还原生流程
        with pytest.raises(Exception):  # noqa: B017  PT011
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/hook/permission", data=b"{}",
                headers={"Content-Length": "2"}, timeout=2)
    finally:
        for _ in range(MAX_INFLIGHT):
            gateway.release_slot()
        gateway.stop()
