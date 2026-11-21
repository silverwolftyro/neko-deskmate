"""approval.py 审批中继单测（M0-lite-5）。

覆盖用户点名的六种：允许、拒绝、超时、重复点击、连接已断开、无活跃会话。
全部用真实 HTTP 客户端打真实 socket——用假 socket 验不了"连接断开"这类事。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from plugin.plugins.agent_deskmate.approval import (
    ACTION_APPROVE,
    ACTION_DENY,
    APPROVE_ALLOW,
    APPROVE_DENY,
    ENTRY_DECIDE,
    HOOK_PATH,
    ApprovalGateway,
    build_card_actions,
    build_card_html,
    build_request,
    hook_response,
)


class ListLogger:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def _log(self, level: str, message: str, *args: object) -> None:
        try:
            rendered = str(message).format(*args) if args else str(message)
        except (IndexError, KeyError, ValueError):
            rendered = f"{message} | {args!r}"
        self.lines.append((level, rendered))

    def info(self, message: str, *args: object) -> None:
        self._log("info", message, *args)

    def warning(self, message: str, *args: object) -> None:
        self._log("warning", message, *args)

    def error(self, message: str, *args: object) -> None:
        self._log("error", message, *args)


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def port() -> int:
    """每个用例一个空闲端口。固定端口会在并行/重跑间撞车。"""
    return _free_port()


def _wait_for_pending(gateway: ApprovalGateway, timeout: float = 15.0) -> None:
    """等 hook 请求真的挂上再点按钮。

    带截止时间而不是固定次数：整套测试一起跑时机器有负载，
    固定 100 次 × 10ms 的忙等会偶发超时（实测 flaky）。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if gateway.pending_count():
            return
        time.sleep(0.005)
    raise AssertionError(f"审批请求在 {timeout}s 内没有挂上")


def _gateway(port: int, timeout: float = 30.0) -> ApprovalGateway:
    gateway = ApprovalGateway(ListLogger(), port=port, timeout_seconds=timeout)
    assert gateway.start() is True
    return gateway


def _post(port: int, payload: dict, path: str = HOOK_PATH, timeout: float = 30.0):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


PAYLOAD = {
    "hook_event_name": "PermissionRequest",
    "session_id": "sess-1",
    "tool_name": "Bash",
    "tool_input": {"command": "rm -rf build/"},
    "cwd": r"D:\workspace\N.E.K.O",
    "permission_mode": "default",
}


# ── 请求构造 ────────────────────────────────────────────────────────────────

def test_build_request_generates_its_own_id() -> None:
    """request_id 由我们生成——Claude 的 tool_use_id 在父子代理间会重复。"""
    a = build_request(PAYLOAD)
    b = build_request(PAYLOAD)
    assert a.request_id != b.request_id
    assert a.session_id == "sess-1"
    assert a.tool_name == "Bash"
    assert "rm -rf build/" in a.tool_summary


def test_build_request_tolerates_missing_fields() -> None:
    request = build_request({"hook_event_name": "PermissionRequest"})
    assert request.session_id == ""
    assert request.tool_summary == "未知工具"
    # pending 键是 (session_id, request_id)；会话为空时是 ("", rid)
    assert (request.session_id, request.request_id)[0] == ""


def test_build_request_tolerates_non_dict() -> None:
    assert build_request(None).tool_summary == "未知工具"
    assert build_request("字符串").session_id == ""


def test_tool_summary_never_dumps_the_whole_input() -> None:
    """只取一个字段：tool_input 可能含密钥或长文本，不能整份进卡片。"""
    request = build_request({"tool_name": "Bash",
                             "tool_input": {"command": "ls", "secret": "sk-live-1"}})
    assert "sk-live-1" not in request.tool_summary
    assert "ls" in request.tool_summary


# ── 允许 / 拒绝 ─────────────────────────────────────────────────────────────

def test_allow_round_trip(port: int) -> None:
    gateway = _gateway(port)
    try:
        result: dict = {}

        def worker() -> None:
            result["response"] = _post(port, PAYLOAD)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        # 等请求真的挂上再点按钮
        _wait_for_pending(gateway)
        pending = gateway.take_new()
        assert len(pending) == 1
        assert gateway.resolve(pending[0].request.request_id, APPROVE_ALLOW) == "resolved"
        thread.join(timeout=10)

        status, body = result["response"]
        assert status == 200
        decision = body["hookSpecificOutput"]["decision"]
        assert decision["behavior"] == "allow"
        assert body["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
        assert gateway.stats()["allow"] == 1
    finally:
        gateway.stop()


def test_deny_carries_a_message_and_does_not_interrupt(port: int) -> None:
    gateway = _gateway(port)
    try:
        result: dict = {}
        thread = threading.Thread(
            target=lambda: result.update(response=_post(port, PAYLOAD)), daemon=True)
        thread.start()
        _wait_for_pending(gateway)
        pending = gateway.take_new()
        gateway.resolve(pending[0].request.request_id, APPROVE_DENY)
        thread.join(timeout=10)

        decision = result["response"][1]["hookSpecificOutput"]["decision"]
        assert decision["behavior"] == "deny"
        assert "拒绝" in decision["message"]
        # 刻意不 interrupt：掐掉整个回合会让 Claude 中途闭嘴
        assert decision["interrupt"] is False
        assert gateway.stats()["deny"] == 1
    finally:
        gateway.stop()


# ── 超时默认拒绝 ────────────────────────────────────────────────────────────

def test_timeout_defaults_to_deny(port: int) -> None:
    gateway = _gateway(port, timeout=0.3)
    try:
        started = time.monotonic()
        status, body = _post(port, PAYLOAD, timeout=10)
        elapsed = time.monotonic() - started

        assert status == 200
        decision = body["hookSpecificOutput"]["decision"]
        assert decision["behavior"] == "deny"
        assert "超时" in decision["message"] or "默认拒绝" in decision["message"]
        assert 0.2 <= elapsed < 5, f"超时没生效（{elapsed:.2f}s）"
        assert gateway.stats()["timeout"] == 1
        assert gateway.pending_count() == 0
    finally:
        gateway.stop()


# ── 重复点击 ────────────────────────────────────────────────────────────────

def test_double_click_is_idempotent(port: int) -> None:
    gateway = _gateway(port)
    try:
        result: dict = {}
        thread = threading.Thread(
            target=lambda: result.update(response=_post(port, PAYLOAD)), daemon=True)
        thread.start()
        _wait_for_pending(gateway)
        pending = gateway.take_new()
        request_id = pending[0].request.request_id

        assert gateway.resolve(request_id, APPROVE_DENY) == "resolved"
        # 第二次、第三次：用户连点 / 多窗口各点一次
        assert gateway.resolve(request_id, APPROVE_ALLOW) == "already-resolved"
        assert gateway.resolve(request_id, APPROVE_ALLOW) == "already-resolved"
        thread.join(timeout=10)

        decision = result["response"][1]["hookSpecificOutput"]["decision"]
        assert decision["behavior"] == "deny"      # 第一次的决定不被翻案
        assert gateway.stats()["allow"] == 0
        assert gateway.stats()["deny"] == 1
    finally:
        gateway.stop()


def test_resolve_unknown_request_id() -> None:
    gateway = ApprovalGateway(ListLogger(), port=_free_port())
    assert gateway.resolve("不存在的id", APPROVE_ALLOW) == "unknown"


def test_resolve_rejects_unknown_decision() -> None:
    gateway = ApprovalGateway(ListLogger(), port=_free_port())
    assert gateway.resolve("x", "也许吧") == "unknown"


def test_concurrent_resolve_has_one_winner(port: int) -> None:
    """两个线程同时点：只能有一个赢家，另一个拿到 already-resolved。"""
    gateway = _gateway(port)
    try:
        result: dict = {}
        thread = threading.Thread(
            target=lambda: result.update(response=_post(port, PAYLOAD)), daemon=True)
        thread.start()
        _wait_for_pending(gateway)
        pending = gateway.take_new()
        request_id = pending[0].request.request_id

        outcomes: list[str] = []
        barrier = threading.Barrier(2)

        def click(decision: str) -> None:
            barrier.wait()
            outcomes.append(gateway.resolve(request_id, decision))

        threads = [threading.Thread(target=click, args=(d,))
                   for d in (APPROVE_ALLOW, APPROVE_DENY)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        thread.join(timeout=10)

        assert sorted(outcomes) == ["already-resolved", "resolved"]
        behavior = result["response"][1]["hookSpecificOutput"]["decision"]["behavior"]
        assert behavior in ("allow", "deny")
        assert gateway.stats()["allow"] + gateway.stats()["deny"] == 1
    finally:
        gateway.stop()


# ── 连接已断开 ──────────────────────────────────────────────────────────────

def test_client_disconnect_does_not_kill_the_server(port: int) -> None:
    """客户端在收到回填前就断开：写失败不能冒出去把服务线程炸掉。"""
    gateway = _gateway(port, timeout=30.0)
    try:
        # 直接裸 socket：发完请求就读一点点然后立刻关，模拟 Ctrl-C
        import socket

        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        body = json.dumps(PAYLOAD).encode("utf-8")
        sock.sendall(
            b"POST " + HOOK_PATH.encode() + b" HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )
        _wait_for_pending(gateway)
        pending = gateway.take_new()
        gateway.resolve(pending[0].request.request_id, APPROVE_ALLOW)
        time.sleep(0.3)          # 让 handler 有机会往已断开的连接里写
        sock.close()

        # 服务必须还活着：下一个请求照样能通
        assert gateway.running() is True
        assert gateway.pending_count() == 0
    finally:
        gateway.stop()


# ── 无活跃会话 ──────────────────────────────────────────────────────────────

def test_payload_without_session_still_works(port: int) -> None:
    """后台/无会话场景：session_id 为空也要能建键、能回填，不能崩。"""
    gateway = _gateway(port)
    try:
        payload = {k: v for k, v in PAYLOAD.items() if k != "session_id"}
        result: dict = {}
        thread = threading.Thread(
            target=lambda: result.update(response=_post(port, payload)), daemon=True)
        thread.start()
        _wait_for_pending(gateway)
        pending = gateway.take_new()
        assert pending[0].request.session_id == ""
        assert pending[0].key[0] == ""
        gateway.resolve(pending[0].request.request_id, APPROVE_ALLOW)
        thread.join(timeout=10)

        assert result["response"][1]["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    finally:
        gateway.stop()


# ── HTTP 边界 ───────────────────────────────────────────────────────────────

def test_wrong_path_is_404(port: int) -> None:
    gateway = _gateway(port)
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(port, PAYLOAD, path="/hook/nope")
        assert excinfo.value.code == 404
        assert gateway.pending_count() == 0
    finally:
        gateway.stop()


def test_bad_json_is_400(port: int) -> None:
    gateway = _gateway(port)
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{HOOK_PATH}",
            data=b"{ this is not json",
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=10)
        assert excinfo.value.code == 400
        assert gateway.pending_count() == 0
    finally:
        gateway.stop()


def test_only_binds_loopback(port: int) -> None:
    """红线：只监听 127.0.0.1，不能监听 0.0.0.0 把审批暴露给局域网。"""
    gateway = _gateway(port)
    try:
        server = gateway._server
        assert server is not None
        assert server.server_address[0] == "127.0.0.1"
    finally:
        gateway.stop()


def test_port_conflict_fails_soft(port: int) -> None:
    """端口被占用：起不来但不抛异常，观测与播报照常。"""
    first = _gateway(port)
    try:
        logger = ListLogger()
        second = ApprovalGateway(logger, port=port, timeout_seconds=5)
        assert second.start() is False
        assert second.running() is False
        assert any(level == "error" for level, _ in logger.lines)
        assert any("端口" in message for _, message in logger.lines)
    finally:
        first.stop()


def test_stop_settles_every_pending_as_deny(port: int) -> None:
    """关服务时还挂着的请求一律按拒绝收尾，别让 handler 线程挂死。"""
    gateway = _gateway(port)
    result: dict = {}
    thread = threading.Thread(
        target=lambda: result.update(response=_post(port, PAYLOAD)), daemon=True)
    thread.start()
    _wait_for_pending(gateway)
    gateway.stop()
    thread.join(timeout=10)

    decision = result["response"][1]["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "deny"
    assert gateway.pending_count() == 0


# ── 卡片协作面 ──────────────────────────────────────────────────────────────

def test_take_new_is_exclusive() -> None:
    """取走即占位，timer 每 1 秒跑一次，不能重复建卡。"""
    gateway = ApprovalGateway(ListLogger(), port=_free_port())
    with gateway._lock:
        gateway._pending[("s", "r1")] = _FakePending("r1")
    first = gateway.take_new()
    assert len(first) == 1
    assert gateway.take_new() == []
    gateway.attach_card("r1", "card-1")
    with gateway._lock:
        assert gateway._pending[("s", "r1")].card_id == "card-1"


def test_settle_pending_cards_only_once() -> None:
    gateway = ApprovalGateway(ListLogger(), port=_free_port())
    with gateway._lock:
        pending = _FakePending("r1")
        pending.card_id = "card-1"
        pending.decision = APPROVE_ALLOW
        gateway._pending[("s", "r1")] = pending
    assert len(gateway.settle_pending_cards()) == 1
    gateway.mark_announced("r1")
    assert gateway.settle_pending_cards() == []


class _FakePending:
    """只带状态的假 pending，用来测 gateway 的协作面而不必真发 HTTP。"""

    def __init__(self, request_id: str) -> None:
        self.request = build_request({"session_id": "s", "tool_name": "Bash",
                                      "request_id": request_id})
        self.request.request_id = request_id
        self.card_id = None
        self.decision = None
        self.decided_by = None
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


def test_hook_response_shape() -> None:
    body = hook_response(APPROVE_ALLOW)
    assert body == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow", "updatedInput": {}, "updatedPermissions": []},
        }
    }


def test_stats_shape() -> None:
    gateway = ApprovalGateway(ListLogger(), port=_free_port())
    stats = gateway.stats()
    assert stats == {"pending": 0, "allow": 0, "deny": 0, "timeout": 0, "running": 0}


# ── 卡片内容（文案是验收项，必须能单测） ────────────────────────────────────


def test_card_html_has_both_buttons() -> None:
    html = build_card_html(subject="claude-code", tool_summary="Bash: rm -rf build/",
                           session_id="800257f3", working_directory=r"D:\workspace",
                           timeout_seconds=60)
    assert f'data-neko-action="{ACTION_APPROVE}"' in html
    assert f'data-neko-action="{ACTION_DENY}"' in html
    assert "允许" in html and "拒绝" in html
    assert "同桌猫 · 审批请求" in html
    assert r"会话 800257f3 @ D:\workspace" in html
    assert "60 秒无人决定将默认拒绝" in html


def test_card_actions_map_to_the_decide_entry() -> None:
    actions = build_card_actions("req-1")
    assert set(actions) == {ACTION_APPROVE, ACTION_DENY}
    for action in actions.values():
        assert action["entry"] == ENTRY_DECIDE
    assert actions[ACTION_APPROVE]["args"] == {"request_id": "req-1", "decision": "allow"}
    assert actions[ACTION_DENY]["args"] == {"request_id": "req-1", "decision": "deny"}


def test_card_escapes_agent_supplied_text() -> None:
    """工具参数是 Agent 产的：拼进 HTML 前必须转义，否则是注入面。"""
    html = build_card_html(subject="claude-code",
                           tool_summary='</p><script>alert(1)</script>',
                           session_id="s", working_directory=None, timeout_seconds=60)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_card_handles_empty_session() -> None:
    html = build_card_html(subject="claude-code", tool_summary="Bash: ls",
                           session_id="", working_directory=None, timeout_seconds=60)
    assert "会话 未知" in html
    assert "@" not in html.split("loc\">")[1].split("</p>")[0]


def test_allow_carries_no_deny_message(port: int) -> None:
    """message 是 deny 专属字段：allow 时带上"你拒绝了"会自相矛盾。

    这条是端到端跑第一遍时撞见的真 bug——当时 allow 的回填里
    写着「用户在 N.E.K.O 里拒绝了这次调用」。
    """
    gateway = _gateway(port)
    try:
        result: dict = {}
        thread = threading.Thread(
            target=lambda: result.update(response=_post(port, PAYLOAD)), daemon=True)
        thread.start()
        _wait_for_pending(gateway)
        pending = gateway.take_new()
        gateway.resolve(pending[0].request.request_id, APPROVE_ALLOW)
        thread.join(timeout=10)

        decision = result["response"][1]["hookSpecificOutput"]["decision"]
        assert decision["behavior"] == "allow"
        assert "message" not in decision
        assert "interrupt" not in decision
    finally:
        gateway.stop()


def test_timeout_message_says_it_timed_out(port: int) -> None:
    """超时的拒绝必须和用户主动拒绝区分开——审计上这是两件事。"""
    gateway = _gateway(port, timeout=0.3)
    try:
        _, body = _post(port, PAYLOAD, timeout=10)
        decision = body["hookSpecificOutput"]["decision"]
        assert decision["behavior"] == "deny"
        assert "无人决定" in decision["message"]
        assert "默认拒绝" in decision["message"]
    finally:
        gateway.stop()


def test_user_deny_message_differs_from_timeout(port: int) -> None:
    gateway = _gateway(port)
    try:
        result: dict = {}
        thread = threading.Thread(
            target=lambda: result.update(response=_post(port, PAYLOAD)), daemon=True)
        thread.start()
        _wait_for_pending(gateway)
        pending = gateway.take_new()
        gateway.resolve(pending[0].request.request_id, APPROVE_DENY)
        thread.join(timeout=10)

        decision = result["response"][1]["hookSpecificOutput"]["decision"]
        assert "拒绝" in decision["message"]
        assert "无人决定" not in decision["message"]
    finally:
        gateway.stop()
