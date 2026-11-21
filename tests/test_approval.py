"""approval.py 审批中继单测（M0-lite-5 / 5a 三层门控）。

核心契约：**插件永不代替用户做判断。**

- 用户点了 → 回填 allow/deny（decided_by=user）
- 到期没人点 → 只标 ``expired``，**不发任何 decision**（宿主的 onFailure 兜底）
- 连接被宿主断开 → 记 ``host_timeout``，**不发任何 decision**
- L2 自动决定 → 必须门控全过 + 有判断后端，且留可审计记录

全部用真实 HTTP 客户端打真实 socket——用假 socket 验不了"连接断开"这类事。
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from plugin.plugins.agent_deskmate.approval import (
    ACTION_APPROVE,
    ACTION_DENY,
    ACTION_EXPLAIN,
    APPROVE_ALLOW,
    APPROVE_DENY,
    AUTO_LOW_RISK_ONLY,
    AUTO_OFF,
    DECIDED_BY_AI,
    DECIDED_BY_EXPIRED,
    DECIDED_BY_HOST_TIMEOUT,
    DECIDED_BY_USER,
    ENTRY_DECIDE,
    ENTRY_EXPLAIN,
    HOOK_PATH,
    ApprovalGateway,
    ApprovalRequest,
    PendingApproval,
    advice_state,
    assess_risk,
    auto_decide_gate,
    build_card_actions,
    build_card_html,
    build_explain_html,
    build_request,
    build_result_html,
    explain_request,
    hook_response,
    no_decision_response,
)

PAYLOAD = {
    "hook_event_name": "PermissionRequest",
    "session_id": "sess-1",
    "tool_name": "Bash",
    "tool_input": {"command": "rm -rf build/"},
    "cwd": r"D:\workspace\N.E.K.O",
    "permission_mode": "default",
}


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
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def port() -> int:
    """每个用例一个空闲端口。固定端口会在并行/重跑间撞车。"""
    return _free_port()


def _gateway_any_port(timeout: float, **kwargs):
    """起一个网关，端口被别人抢了就换一个再试。

    ``_free_port()`` 只是"当时没人绑"，到真正 bind 之间有个窗口；
    上一个用例刚关的 socket 也可能让这个端口处于 TIME_WAIT。
    那种偶发失败跟被测代码无关，重试比让它红更有意义。
    """
    last = None
    for _ in range(5):
        candidate = _free_port()
        gateway = ApprovalGateway(ListLogger(), port=candidate,
                                  timeout_seconds=timeout, **kwargs)
        if gateway.start():
            return gateway, candidate
        last = gateway
    raise AssertionError(f"换了 5 个端口都绑不上，最后一个：{last}")


def _gateway(port: int, timeout: float = 5.0, **kwargs) -> ApprovalGateway:
    gateway = ApprovalGateway(ListLogger(), port=port, timeout_seconds=timeout, **kwargs)
    assert gateway.start() is True, f"端口 {port} 绑不上"
    return gateway


def _wait_for_pending(gateway: ApprovalGateway, timeout: float = 15.0) -> None:
    """等 hook 请求真的挂上再点按钮。

    带截止时间而不是固定次数：整套测试一起跑时机器有负载，
    固定次数的忙等会偶发超时（实测 flaky）。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if gateway.undecided_count():
            return
        time.sleep(0.005)
    raise AssertionError(f"审批请求在 {timeout}s 内没有挂上")


def _post(port: int, payload: dict, path: str = HOOK_PATH, timeout: float = 30.0):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _post_status(port: int, payload: dict, timeout: float = 30.0) -> tuple[int, dict]:
    """发请求并原样拿回状态码——503 会抛 HTTPError，这里替调用方接住。"""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{HOOK_PATH}", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _only_pending(gateway: ApprovalGateway) -> PendingApproval:
    """等挂起并取走唯一一条待建卡的审批。

    断言"恰好一条"：记录现在过期也不删，若同一 gateway 上有前一条没清，
    take_new 会返回多条，默默取 [0] 会点到错的请求上——那种错很难查。
    """
    _wait_for_pending(gateway)
    fresh = gateway.take_new()
    assert len(fresh) == 1, f"期望恰好一条待建卡审批，实际 {len(fresh)}"
    return fresh[0]


def _decide_later(gateway: ApprovalGateway, port: int, payload: dict,
                  decision: str, wait: float = 15.0) -> dict:
    """后台发请求，等它挂起后点按钮，返回回填体。"""
    result: dict = {}
    thread = threading.Thread(
        target=lambda: result.update(response=_post(port, payload)), daemon=True)
    thread.start()
    pending = _only_pending(gateway)
    gateway.resolve(pending.request.request_id, decision, decided_by=DECIDED_BY_USER)
    thread.join(timeout=wait)
    assert "response" in result, "客户端没在限定时间内收到回填"
    return result["response"][1]


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


# ── 风险判定（L0，不接模型） ────────────────────────────────────────────────

def test_assess_risk_marks_delete_as_critical() -> None:
    severity, rule = assess_risk("Bash", {"command": "rm -rf /tmp/x"})
    assert severity == "critical"
    assert rule == "delete"


@pytest.mark.parametrize("command", ["ls -la", "git status", "npm run build"])
def test_assess_risk_defaults_to_notice(command: str) -> None:
    severity, rule = assess_risk("Bash", {"command": command})
    assert severity == "notice"
    assert rule == ""


def test_assess_risk_ignores_non_bash_tools() -> None:
    """Read/Glob 之类没有 command 字段；不猜它干什么。"""
    assert assess_risk("Read", {"file_path": "x"}) == ("notice", "")
    assert assess_risk("Bash", None) == ("notice", "")
    assert assess_risk("Bash", {"command": "rm -rf x"}) == ("critical", "delete")


# ── 三层门控（规格 §1） ─────────────────────────────────────────────────────

def test_gate_defaults_to_closed() -> None:
    """默认全关：auto_decide=off 时其余参数怎么设都不放行。"""
    assert auto_decide_gate(rule="", severity="notice", ai_judgement=True,
                            has_key=True, auto_decide=AUTO_OFF,
                            max_severity="notice")[0] is False


def test_gate_requires_l1_first() -> None:
    """不能跳级：没开 ai_judgement 就没有解释能力，此时自动决定是黑箱代办。"""
    eligible, why = auto_decide_gate(rule="", severity="notice", ai_judgement=False,
                                     has_key=True, auto_decide=AUTO_LOW_RISK_ONLY,
                                     max_severity="notice")
    assert eligible is False
    assert "ai_judgement" in why


def test_gate_requires_a_key() -> None:
    eligible, why = auto_decide_gate(rule="", severity="notice", ai_judgement=True,
                                     has_key=False, auto_decide=AUTO_LOW_RISK_ONLY,
                                     max_severity="notice")
    assert eligible is False
    assert "api_key" in why


def test_gate_excludes_delete_forever() -> None:
    """删除类永远不进 L2，无论阈值怎么设。"""
    for max_severity in ("info", "notice", "warn", "critical"):
        eligible, why = auto_decide_gate(
            rule="delete", severity="critical", ai_judgement=True, has_key=True,
            auto_decide=AUTO_LOW_RISK_ONLY, max_severity=max_severity)
        assert eligible is False, f"阈值 {max_severity} 居然放行了删除类"
        assert "永不进入 L2" in why


def test_gate_respects_the_severity_threshold() -> None:
    eligible, why = auto_decide_gate(rule="", severity="warn", ai_judgement=True,
                                     has_key=True, auto_decide=AUTO_LOW_RISK_ONLY,
                                     max_severity="notice")
    assert eligible is False
    assert "超过阈值" in why


def test_gate_opens_when_all_four_hold() -> None:
    eligible, why = auto_decide_gate(rule="", severity="notice", ai_judgement=True,
                                     has_key=True, auto_decide=AUTO_LOW_RISK_ONLY,
                                     max_severity="notice")
    assert eligible is True
    assert why == "L2 门控通过"


def test_advice_state_degrades_without_silently_succeeding() -> None:
    """四种状态都不许假装成功。"""
    assert advice_state(ai_judgement=False, has_key=False, backend_ready=False) == "disabled"
    assert advice_state(ai_judgement=True, has_key=False, backend_ready=False) == "no_key"
    assert advice_state(ai_judgement=True, has_key=True,
                        backend_ready=False) == "pending_backend"
    assert advice_state(ai_judgement=True, has_key=True, backend_ready=True) == "ready"


# ── 允许 / 拒绝 ─────────────────────────────────────────────────────────────

def test_allow_round_trip(port: int) -> None:
    gateway = _gateway(port)
    try:
        body = _decide_later(gateway, port, PAYLOAD, APPROVE_ALLOW)
        decision = body["hookSpecificOutput"]["decision"]
        assert decision["behavior"] == "allow"
        assert body["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
        # message 是 deny 专属字段，allow 时不带
        assert "message" not in decision
        assert gateway.stats()["allow"] == 1
    finally:
        gateway.stop()


def test_deny_carries_a_message_and_does_not_interrupt(port: int) -> None:
    gateway = _gateway(port)
    try:
        body = _decide_later(gateway, port, PAYLOAD, APPROVE_DENY)
        decision = body["hookSpecificOutput"]["decision"]
        assert decision["behavior"] == "deny"
        assert "拒绝" in decision["message"]
        # 刻意不 interrupt：掐掉整个回合会让 Claude 中途闭嘴
        assert decision["interrupt"] is False
        assert gateway.stats()["deny"] == 1
    finally:
        gateway.stop()


def test_every_decision_is_audited(port: int, tmp_path: Path) -> None:
    gateway = _gateway(port, audit_path=tmp_path / "audit.jsonl")
    try:
        _decide_later(gateway, port, PAYLOAD, APPROVE_ALLOW)
        rows = gateway.audit_rows()
        assert len(rows) == 1
        row = rows[0]
        assert row["decided_by"] == DECIDED_BY_USER
        assert row["decision"] == "allow"
        assert row["session_id"] == "sess-1"
        assert "rm -rf build/" in row["tool_summary"]
        assert row["model"] == "-"
        assert "用户在" in row["reason"]
        # 落了一份 JSONL，插件重启后仍可查
        assert (tmp_path / "audit.jsonl").is_file()
        written = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip()
        assert len(written.splitlines()) == 1
    finally:
        gateway.stop()


def test_audit_text_is_exportable(port: int) -> None:
    gateway = _gateway(port)
    try:
        _decide_later(gateway, port, PAYLOAD, APPROVE_ALLOW)
        text = gateway.audit_text()
        assert text.startswith("# 同桌猫审批审计（共 1 条）")
        assert "by=user" in text
        assert "allow" in text
    finally:
        gateway.stop()


# ── 到期：绝不发决定（本规格的核心） ─────────────────────────────────────────

def test_expiry_sends_no_decision(port: int) -> None:
    """到期只标 expired，**不发任何 decision**。

    这是整个规格的底线：插件不代用户判断。拦住调用的是宿主自己的
    onFailure:"block" 策略，不是我们。
    """
    gateway = _gateway(port, timeout=0.3)
    try:
        status, body = _post_status(port, PAYLOAD)

        assert status == 503                       # 非 2xx → 宿主按失败策略处理
        assert "hookSpecificOutput" not in body    # **没有任何 decision**
        assert body["reason"] == "no_decision"
        assert "不代你决定" in body["message"]

        rows = gateway.audit_rows()
        assert len(rows) == 1
        assert rows[0]["decided_by"] == DECIDED_BY_EXPIRED
        assert rows[0]["decision"] is None         # 审计里也记"没发决定"
        assert gateway.stats()["expired"] == 1
        assert gateway.undecided_count() == 0
    finally:
        gateway.stop()


def test_expired_entry_is_kept_not_deleted(port: int) -> None:
    """过期不删，保留一段时间供 pending_list 事后查。"""
    gateway = _gateway(port, timeout=0.3, retention_seconds=3600)
    try:
        _post_status(port, PAYLOAD)
        rows = gateway.snapshot()
        assert len(rows) == 1
        row = rows[0]
        assert row["state"] == "settled"
        assert row["decided_by"] == DECIDED_BY_EXPIRED
        assert row["decision"] is None
        assert row["session_id"] == "sess-1"
        assert "rm -rf build/" in row["tool_summary"]
        assert row["created_time"]                  # ISO 时间，人能读
        assert row["risk"] == "critical"            # 删除类的风险判定
        assert row["rule"] == "delete"
    finally:
        gateway.stop()


def test_expired_entry_is_pruned_after_retention(port: int) -> None:
    """保留期一到就清掉——内存不能无限涨。

    保留期必须大于到期时间，否则记录一了结就已经算过期，
    这个用例验的就不是"保留"而是"立即删"。
    """
    gateway = _gateway(port, timeout=0.3, retention_seconds=1.0)
    try:
        _post_status(port, PAYLOAD)
        assert len(gateway.snapshot()) == 1      # 保留期内还在
        time.sleep(1.1)
        assert gateway.snapshot() == []          # 过了保留期才清
    finally:
        gateway.stop()


def test_expired_card_button_is_refused(port: int) -> None:
    """点过期的卡片按钮 → 明确告知已过期，不回填。"""
    gateway = _gateway(port, timeout=0.3)
    try:
        _post_status(port, PAYLOAD)
        rows = gateway.snapshot()
        request_id = rows[0]["request_id"]
        assert gateway.resolve(request_id, APPROVE_ALLOW) == "expired"
        assert gateway.stats()["allow"] == 0        # 没有回填
    finally:
        gateway.stop()


def test_delete_never_auto_decides_even_with_l2_open(port: int) -> None:
    """规格 §2 硬规则 3：删除类永远不走 L2。

    就算用户把 L2 全开、阈值调到最高，删除类命中后到期仍然只标 expired。
    """
    gateway = _gateway(port, timeout=0.3, ai_judgement=True, has_api_key=True,
                       auto_decide=AUTO_LOW_RISK_ONLY,
                       auto_decide_max_severity="critical",
                       judge=lambda record: {"decision": "allow", "model": "m",
                                             "reason": "低风险"})
    try:
        status, body = _post_status(port, PAYLOAD)
        assert status == 503
        assert "hookSpecificOutput" not in body
        rows = gateway.audit_rows()
        assert rows[0]["decided_by"] == DECIDED_BY_EXPIRED
        assert rows[0]["decision"] is None
    finally:
        gateway.stop()


def test_l2_gate_open_without_backend_still_refuses(port: int) -> None:
    """门控算得过但没接判断后端（5a）→ 降级 expired，不自己拍一个。

    这正是"插件不许因为门控通过就替用户判断"的机械保证。
    """
    gateway = _gateway(port, timeout=0.3, ai_judgement=True, has_api_key=True,
                       auto_decide=AUTO_LOW_RISK_ONLY,
                       auto_decide_max_severity="notice")
    try:
        payload = dict(PAYLOAD, tool_input={"command": "ls -la"})
        status, body = _post_status(port, payload)
        assert status == 503
        assert "hookSpecificOutput" not in body
        rows = gateway.audit_rows()
        assert rows[0]["decided_by"] == DECIDED_BY_EXPIRED
        assert rows[0]["decision"] is None
        assert any("判断后端未接入" in message for _, message in gateway._logger.lines)
    finally:
        gateway.stop()


# ── L2 自动决定（5b 的接缝；5a 用假后端验通路） ────────────────────────────

def _low_risk_payload() -> dict:
    return dict(PAYLOAD, tool_input={"command": "ls -la"})


def _l2_gateway(port: int, judge) -> ApprovalGateway:
    return _gateway(port, timeout=0.4, ai_judgement=True, has_api_key=True,
                    auto_decide=AUTO_LOW_RISK_ONLY,
                    auto_decide_max_severity="notice", judge=judge)


def test_l2_auto_allow_is_audited_with_model_and_reason(port: int) -> None:
    gateway = _l2_gateway(port, lambda r: {"decision": "allow",
                                           "model": "deepseek-chat",
                                           "reason": "只读命令，无副作用"})
    try:
        status, body = _post_status(port, _low_risk_payload())
        assert status == 200
        assert body["hookSpecificOutput"]["decision"]["behavior"] == "allow"
        assert "非你亲手点的" in body["hookSpecificOutput"]["decision"]["message"]

        row = gateway.audit_rows()[0]
        assert row["decided_by"] == DECIDED_BY_AI
        assert row["decision"] == "allow"
        assert row["model"] == "deepseek-chat"
        assert row["reason"] == "只读命令，无副作用"
        assert gateway.stats()["ai"] == 1
    finally:
        gateway.stop()


def test_l2_auto_deny_is_audited(port: int) -> None:
    gateway = _l2_gateway(port, lambda r: {"decision": "deny", "model": "m",
                                           "reason": "看起来在动不该动的东西"})
    try:
        _, body = _post_status(port, _low_risk_payload())
        assert body["hookSpecificOutput"]["decision"]["behavior"] == "deny"
        row = gateway.audit_rows()[0]
        assert row["decided_by"] == DECIDED_BY_AI
        assert row["decision"] == "deny"
    finally:
        gateway.stop()


@pytest.mark.parametrize("bad", [None, {}, {"decision": "也许吧"}, "不是dict"])
def test_l2_invalid_verdict_falls_back_to_expired(port: int, bad: object) -> None:
    """判断后端给出无效决定 → 降级 expired，绝不猜一个。"""
    gateway = _l2_gateway(port, lambda r: bad)
    try:
        status, body = _post_status(port, _low_risk_payload())
        assert status == 503
        assert "hookSpecificOutput" not in body
        row = gateway.audit_rows()[0]
        assert row["decided_by"] == DECIDED_BY_EXPIRED
        assert row["decision"] is None
    finally:
        gateway.stop()


def test_l2_backend_explosion_falls_back_to_expired(port: int) -> None:
    """后端抛异常不能炸掉审批——降级 expired。"""

    def exploding(_: PendingApproval) -> dict:
        raise RuntimeError("模型服务挂了")

    gateway = _l2_gateway(port, exploding)
    try:
        status, body = _post_status(port, _low_risk_payload())
        assert status == 503
        assert "hookSpecificOutput" not in body
        assert gateway.audit_rows()[0]["decided_by"] == DECIDED_BY_EXPIRED
    finally:
        gateway.stop()


def test_user_click_beats_l2(port: int) -> None:
    """用户点了就以用户为准——L2 只在"用户没点"时才有机会。"""
    gateway = _gateway(port, timeout=30.0, ai_judgement=True, has_api_key=True,
                       auto_decide=AUTO_LOW_RISK_ONLY,
                       auto_decide_max_severity="notice",
                       judge=lambda r: {"decision": "deny", "model": "m",
                                        "reason": "不该放"})
    try:
        result: dict = {}
        thread = threading.Thread(
            target=lambda: result.update(response=_post(port, _low_risk_payload())),
            daemon=True)
        thread.start()
        pending = _only_pending(gateway)
        gateway.resolve(pending.request.request_id, APPROVE_ALLOW)
        thread.join(timeout=10)

        behavior = result["response"][1]["hookSpecificOutput"]["decision"]["behavior"]
        assert behavior == "allow"
        assert gateway.audit_rows()[0]["decided_by"] == DECIDED_BY_USER
    finally:
        gateway.stop()


# ── 连接已断开 ──────────────────────────────────────────────────────────────

def test_host_timeout_is_recorded_without_a_decision(port: int) -> None:
    """连接被宿主断开 → 记 host_timeout，**不发任何 decision**。

    直接驱动 respond 抛连接类异常，不走真实 socket：TCP 写入有缓冲，
    "关掉客户端再写"是否抛异常取决于时序，那种用例是 flaky 的。
    这里要验的是"投递失败时状态怎么变"，用可控注入更准。
    """
    gateway = _gateway(port, timeout=30.0)
    delivered: list[dict | None] = []

    def respond(result: dict | None) -> None:
        delivered.append(result)
        raise ConnectionResetError("宿主提前离开了")

    thread = threading.Thread(
        target=lambda: gateway.submit(PAYLOAD, respond=respond), daemon=True)
    thread.start()
    _wait_for_pending(gateway)
    pending = _only_pending(gateway)
    gateway.resolve(pending.request.request_id, APPROVE_ALLOW)
    thread.join(timeout=10)

    # 决定确实生成过（否则这个用例什么都没验到）
    assert len(delivered) == 1
    assert delivered[0]["hookSpecificOutput"]["decision"]["behavior"] == "allow"

    assert gateway.undecided_count() == 0
    assert gateway.stats()["host_timeout"] == 1
    assert gateway.stats()["allow"] == 0        # 没送到，不算"已允许"
    row = gateway.audit_rows()[0]
    assert row["decided_by"] == DECIDED_BY_HOST_TIMEOUT
    assert row["decision"] == "allow"           # 用户选过允许，但没送达
    assert "未送达" in row["reason"]


def test_real_disconnect_does_not_kill_the_server(port: int) -> None:
    """真实断线只验一件事：服务还活着，下一个请求照样能通。

    Windows 上抛的是 ConnectionAbortedError(10053)，不是 BrokenPipeError 的
    子类——只捕后两者会让这个异常冒出去把服务线程炸掉。
    """
    gateway = _gateway(port, timeout=30.0)
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        body = json.dumps(PAYLOAD).encode("utf-8")
        sock.sendall(
            b"POST " + HOOK_PATH.encode() + b" HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )
        pending = _only_pending(gateway)
        sock.close()
        time.sleep(0.1)
        gateway.resolve(pending.request.request_id, APPROVE_ALLOW)
        time.sleep(0.4)

        assert gateway.running() is True
        assert gateway.undecided_count() == 0
        # 服务还活着：还能接受新连接（不必走完整审批回路，那条上面已验过）
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            pass
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
        pending = _only_pending(gateway)
        request_id = pending.request.request_id

        assert gateway.resolve(request_id, APPROVE_DENY) == "resolved"
        # 第二次、第三次：用户连点 / 多窗口各点一次
        assert gateway.resolve(request_id, APPROVE_ALLOW) == "already-resolved"
        assert gateway.resolve(request_id, APPROVE_ALLOW) == "already-resolved"
        thread.join(timeout=10)

        decision = result["response"][1]["hookSpecificOutput"]["decision"]
        assert decision["behavior"] == "deny"      # 第一次的决定不被翻案
        assert gateway.stats()["allow"] == 0
        assert gateway.stats()["deny"] == 1
        # 审计只有一条——重复点击不重复记账
        assert len(gateway.audit_rows()) == 1
    finally:
        gateway.stop()


def test_concurrent_resolve_has_one_winner(port: int) -> None:
    """两个线程同时点：只能有一个赢家，另一个拿到 already-resolved。"""
    gateway = _gateway(port)
    try:
        result: dict = {}
        thread = threading.Thread(
            target=lambda: result.update(response=_post(port, PAYLOAD)), daemon=True)
        thread.start()
        pending = _only_pending(gateway)
        request_id = pending.request.request_id

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


def test_resolve_unknown_request_id() -> None:
    gateway = ApprovalGateway(ListLogger(), port=_free_port())
    assert gateway.resolve("不存在的id", APPROVE_ALLOW) == "unknown"


def test_resolve_rejects_unknown_decision() -> None:
    gateway = ApprovalGateway(ListLogger(), port=_free_port())
    assert gateway.resolve("x", "也许吧") == "unknown"


# ── 无活跃会话 ──────────────────────────────────────────────────────────────

def test_payload_without_session_still_works(port: int) -> None:
    """后台/无会话场景：session_id 为空也要能建键、能回填，不能崩。"""
    gateway = _gateway(port)
    try:
        payload = {k: v for k, v in PAYLOAD.items() if k != "session_id"}
        body = _decide_later(gateway, port, payload, APPROVE_ALLOW)
        assert body["hookSpecificOutput"]["decision"]["behavior"] == "allow"
        rows = gateway.snapshot()
        assert rows[0]["session_id"] == ""
    finally:
        gateway.stop()


# ── HTTP 边界 ───────────────────────────────────────────────────────────────

def test_wrong_path_is_404(port: int) -> None:
    gateway = _gateway(port)
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(port, PAYLOAD, path="/hook/nope")
        assert excinfo.value.code == 404
        assert gateway.undecided_count() == 0
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
        assert gateway.undecided_count() == 0
    finally:
        gateway.stop()


def test_only_binds_loopback(port: int) -> None:
    """红线：只监听 127.0.0.1，不能监听 0.0.0.0 把审批暴露给局域网。"""
    gateway = _gateway(port)
    try:
        assert gateway._server.server_address[0] == "127.0.0.1"
    finally:
        gateway.stop()


def test_port_conflict_fails_soft(port: int) -> None:
    """端口被占用：起不来但不抛异常，观测与播报照常。

    Windows 上 HTTPServer 默认开 SO_REUSEADDR，两个 socket 能绑同一个端口，
    结果第二个服务起得来却永远收不到 hook——那种静默失败更难查，
    所以 start() 里先 connect_ex 探一次。
    """
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


def test_stop_settles_every_pending_as_expired(port: int) -> None:
    """关服务时还挂着的请求：不代用户决定，只标 expired，且不回填。

    直接驱动 submit（不走 HTTP）：这里要验的是"stop 怎么收尾"，
    走真实客户端还得处理连接被关时的异常，与用例无关。
    """
    gateway = _gateway(port, timeout=30.0)
    delivered: list[dict | None] = []

    def respond(result: dict | None) -> None:
        delivered.append(result)

    thread = threading.Thread(
        target=lambda: gateway.submit(PAYLOAD, respond=respond), daemon=True)
    thread.start()
    _wait_for_pending(gateway)
    gateway.stop()
    thread.join(timeout=10)

    assert delivered == [None]                  # 没有任何 decision
    assert gateway.undecided_count() == 0
    assert gateway.audit_rows()[0]["decided_by"] == DECIDED_BY_EXPIRED
    assert gateway.audit_rows()[0]["decision"] is None


# ── 卡片四块（规格 §4） ─────────────────────────────────────────────────────

def test_card_html_has_all_four_blocks() -> None:
    html = build_card_html(subject="claude-code",
                           tool_summary="Bash: rm -rf build/",
                           session_id="800257f3",
                           working_directory=r"D:\workspace",
                           timeout_seconds=570)
    # 1) 标题/来源/命令
    assert "同桌猫 · 审批请求" in html
    assert "claude-code 请求执行" in html
    assert "rm -rf build/" in html
    # 2) 会话与 cwd
    assert "会话 800257f3 @ D:\\workspace" in html
    # 3) 三个按钮
    assert f'data-neko-action="{ACTION_APPROVE}"' in html
    assert f'data-neko-action="{ACTION_DENY}"' in html
    assert f'data-neko-action="{ACTION_EXPLAIN}"' in html
    # 4) 超时说明：说清"宿主自行拦截、视为未决定"，不写"已自动拒绝"
    assert "宿主会自行拦截" in html
    assert "视为未决定" in html
    assert "插件不代你判断" in html
    assert "自动拒绝" not in html


def test_card_degraded_advice_when_no_key() -> None:
    """没 key：建议块降级成一行说明，不编内容。"""
    html = build_card_html(subject="claude-code", tool_summary="Bash: ls",
                           session_id="s", working_directory=None,
                           timeout_seconds=570, advice_state="no_key")
    assert "未配置 key，无法给出建议" in html
    assert "建议：" not in html          # 没有假建议
    assert "理由" not in html


def test_card_advice_placeholder_when_key_present() -> None:
    """有 key 但后端没接（5a）：占位说明，不假装已经能建议。"""
    html = build_card_html(subject="claude-code", tool_summary="Bash: ls",
                           session_id="s", working_directory=None,
                           timeout_seconds=570, advice_state="pending_backend")
    assert "待接入" in html
    assert "未配置 key" not in html


def test_card_advice_blocks_when_ready() -> None:
    """5b 之后：建议 + 理由 + 依据三行齐备。"""
    html = build_card_html(subject="claude-code", tool_summary="Bash: ls",
                           session_id="s", working_directory=None,
                           timeout_seconds=570, advice_state="ready",
                           suggestion="允许（低风险）",
                           reason="只读命令，无副作用。",
                           basis="命令无 -rf；cwd 不在项目根内。")
    assert "建议：允许（低风险）" in html
    assert "理由：只读命令，无副作用。" in html
    assert "依据：命令无 -rf；cwd 不在项目根内。" in html


def test_card_actions_map_three_buttons() -> None:
    actions = build_card_actions("req-1")
    assert set(actions) == {ACTION_APPROVE, ACTION_DENY, ACTION_EXPLAIN}
    assert actions[ACTION_APPROVE]["entry"] == ENTRY_DECIDE
    assert actions[ACTION_DENY]["entry"] == ENTRY_DECIDE
    assert actions[ACTION_EXPLAIN]["entry"] == ENTRY_EXPLAIN
    assert actions[ACTION_APPROVE]["args"] == {"request_id": "req-1", "decision": "allow"}
    assert actions[ACTION_DENY]["args"] == {"request_id": "req-1", "decision": "deny"}


def test_card_escapes_agent_supplied_text() -> None:
    """工具参数是 Agent 产的：拼进 HTML 前必须转义，否则是注入面。"""
    html = build_card_html(subject="claude-code",
                           tool_summary='</p><script>alert(1)</script>',
                           session_id="s", working_directory=None, timeout_seconds=570)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_result_card_says_expired_not_auto_denied() -> None:
    note = ("已过期：无人决定，插件不代你判断，本次调用交由宿主按 "
            "onFailure 策略处理。")
    html = build_result_html(note)
    assert "已过期" in html
    assert "自动拒绝" not in html


# ── 「这是什么」 ────────────────────────────────────────────────────────────

def test_explain_delete_mentions_it_never_auto_decides() -> None:
    explanation = explain_request(tool_name="Bash", tool_summary="Bash: rm -rf x",
                                  working_directory=r"D:\workspace", rule="delete",
                                  severity="critical")
    assert "删除类" in explanation["verdict"]
    assert "永不进入 L2" in explanation["extra"]
    assert "亲自点允许或拒绝" in explanation["extra"]
    assert "只转发、不代判" in explanation["note"] or "规则层" in explanation["note"]


def test_explain_low_risk_still_does_not_decide() -> None:
    explanation = explain_request(tool_name="Bash", tool_summary="Bash: ls",
                                  working_directory=None, rule="", severity="notice")
    assert "低风险" in explanation["verdict"]
    assert "只转发、不代判" in explanation["extra"]


def test_explain_html_escapes() -> None:
    explanation = explain_request(tool_name="Bash",
                                  tool_summary="<script>x</script>",
                                  working_directory=None, rule="", severity="notice")
    html = build_explain_html(explanation)
    assert "<script>" not in html


# ── 纪律：AI 不能自己给自己授权（规格 §2 硬规则 6） ─────────────────────────

def test_policy_is_read_only_at_runtime() -> None:
    """门控开关只能来自用户配置；运行期没有任何方法能改它们。"""
    gateway = ApprovalGateway(
        ListLogger(), port=_free_port(), ai_judgement=False, auto_decide=AUTO_OFF,
        auto_decide_max_severity="notice", has_api_key=False)
    before = gateway.policy_snapshot()

    # 把能点的操作全点一遍，再看快照有没有被动过
    gateway.resolve("x", APPROVE_ALLOW)
    gateway.undecided_count()
    gateway.snapshot()
    gateway.audit_rows()
    gateway.stats()

    assert gateway.policy_snapshot() == before
    assert before["ai_judgement"] is False
    assert before["auto_decide"] == AUTO_OFF


def test_no_method_enables_the_policy() -> None:
    """反向守护：ApprovalGateway 上不存在能打开门控的公开方法。"""
    forbidden = {"enable_ai", "enable_auto", "set_auto_decide", "set_ai_judgement",
                 "turn_on", "authorize"}
    public = {name for name in dir(ApprovalGateway) if not name.startswith("_")}
    assert not (forbidden & public), f"出现了能自我授权的方法：{forbidden & public}"


def test_policy_snapshot_reflects_config() -> None:
    gateway = ApprovalGateway(ListLogger(), port=_free_port(), ai_judgement=True,
                              auto_decide=AUTO_LOW_RISK_ONLY, has_api_key=True,
                              auto_decide_max_severity="warn")
    assert gateway.policy_snapshot() == {
        "ai_judgement": True, "auto_decide": AUTO_LOW_RISK_ONLY,
        "auto_decide_max_severity": "warn", "has_api_key": True,
        "judge_connected": False,
    }


# ── 响应体形状 ──────────────────────────────────────────────────────────────

def test_hook_response_shape() -> None:
    assert hook_response(APPROVE_ALLOW) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow", "updatedInput": {},
                         "updatedPermissions": []},
        }
    }


def test_no_decision_response_has_no_decision() -> None:
    body = no_decision_response(reason="no_decision", timeout_seconds=570)
    assert body["ok"] is False
    assert "hookSpecificOutput" not in body
    assert "不代你决定" in body["message"]


def test_stats_shape() -> None:
    gateway = ApprovalGateway(ListLogger(), port=_free_port())
    assert gateway.stats() == {"pending": 0, "allow": 0, "deny": 0, "ai": 0,
                               "expired": 0, "host_timeout": 0, "running": 0}


# ── 协作面（不需要真发 HTTP） ────────────────────────────────────────────────

def _record(rid: str, **overrides) -> PendingApproval:
    request = ApprovalRequest(session_id="s", request_id=rid, tool_name="Bash",
                              tool_summary="Bash: ls")
    record = PendingApproval(request)
    for key, value in overrides.items():
        if hasattr(record, key):
            setattr(record, key, value)
    return record


def test_take_new_is_exclusive() -> None:
    """取走即占位，timer 每 1 秒跑一次，不能重复建卡。"""
    gateway = ApprovalGateway(ListLogger(), port=_free_port())
    with gateway._lock:
        gateway._records[("s", "r1")] = _record("r1")
    assert len(gateway.take_new()) == 1
    assert gateway.take_new() == []
    gateway.attach_card("r1", "card-1")
    with gateway._lock:
        assert gateway._records[("s", "r1")].card_id == "card-1"


def test_settle_pending_cards_only_once() -> None:
    gateway = ApprovalGateway(ListLogger(), port=_free_port())
    with gateway._lock:
        gateway._records[("s", "r1")] = _record(
            "r1", card_id="card-1", decided_by=DECIDED_BY_USER, decision=APPROVE_ALLOW)
    assert len(gateway.settle_pending_cards()) == 1
    gateway.mark_announced("r1")
    assert gateway.settle_pending_cards() == []


def test_find_returns_none_for_unknown() -> None:
    gateway = ApprovalGateway(ListLogger(), port=_free_port())
    assert gateway.find("nope") is None
