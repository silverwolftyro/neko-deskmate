"""approval.py 审批中继单测（M0-lite-5 / 5a 三层门控 + 显式授权）。

核心契约：**插件永不代替用户做判断，除非用户显式授权；而授权也换不来 auto-deny。**

- 用户点了 → 回填 allow/deny（decided_by=user）
- 到期没人点 → 只标 ``expired``，**不发任何 decision、不记成拒绝**
- 连接被宿主断开 → 记 ``host_timeout``，**不发任何 decision**
- L2 自动放行 → 只在事件发生那一刻，且必须已授权；**只可能 allow**
- 没授权 / 没 key / 没后端 / 后端说 deny / 后端异常 → 一律不自动决定

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
    ACTION_AUTH_CANCEL,
    ACTION_AUTH_CONFIRM,
    ACTION_CLASS_IRREVERSIBLE,
    ACTION_CLASS_RECOVERABLE,
    ACTION_DENY,
    ACTION_EXPLAIN,
    ADVICE_STATE_READY,
    ADVICE_TEXT,
    APPROVE_ALLOW,
    APPROVE_DENY,
    AUTH_SCOPE_ADVICE,
    AUTH_SCOPE_AUTO,
    AUTH_SCOPES,
    AUTHORIZATION_HEADLINE,
    AUTO_LOW_RISK_ONLY,
    AUTO_OFF,
    DECIDED_BY_AI,
    DECIDED_BY_AUTHORIZATION,
    DECIDED_BY_EXPIRED,
    DECIDED_BY_HOST_TIMEOUT,
    DECIDED_BY_USER,
    ENTRY_AUTHORIZE,
    ENTRY_DECIDE,
    ENTRY_EXPLAIN,
    HOOK_PATH,
    ApprovalGateway,
    ApprovalRequest,
    AuthorizationRecord,
    PendingApproval,
    advice_state,
    advice_text,
    assess_risk,
    auto_decide_gate,
    build_authorize_actions,
    build_authorize_html,
    build_card_actions,
    build_card_html,
    build_explain_html,
    build_request,
    build_result_html,
    effective_max_severity,
    explain_request,
    hook_response,
    load_authorization,
    no_decision_response,
)
from plugin.plugins.agent_deskmate.commands import (
    BANNED_INTERNAL,
    BANNED_PHRASES,
    CONCLUSION_ALLOW,
    CONCLUSION_DENY,
    CONCLUSION_UNSURE,
    CONCLUSIONS,
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


class _Port:
    """可变端口号。

    ``_free_port()`` 只是"当时没人绑"，到真正 bind 之间有个窗口：上个用例刚关的
    socket 可能还占着这个号，OS 也可能把它分给别的连接。那种偶发失败跟被测代码
    无关（实测 CI seed 下会红），所以 ``_gateway`` 绑不上时换一个端口重试，
    并把这个对象的值一起改掉——调用方持有的是同一个对象，不用跟着动。
    """

    def __init__(self, value: int) -> None:
        self.value = int(value)

    def __int__(self) -> int:
        return self.value

    def __index__(self) -> int:
        return self.value

    def __repr__(self) -> str:
        return f"_Port({self.value})"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def port() -> _Port:
    """每个用例一个空闲端口（可能被 _gateway 换掉，以 gateway.port 为准）。"""
    return _Port(_free_port())


def _gateway(port, timeout: float = 5.0, **kwargs) -> ApprovalGateway:
    """起一个网关。端口被别人抢了就换一个再试，并同步改调用方那个 _Port。

    ``_free_port()`` 只是"当时没人绑"，到真正 bind 之间有个窗口；
    上一个用例刚关的 socket 也可能让这个端口处于 TIME_WAIT。
    那种偶发失败跟被测代码无关，重试比让它红更有意义。
    """
    last = None
    for candidate in (int(port), *(_free_port() for _ in range(4))):
        gateway = ApprovalGateway(ListLogger(), port=candidate,
                                  timeout_seconds=timeout, **kwargs)
        if gateway.start():
            if isinstance(port, _Port):
                port.value = candidate      # 换过端口：让调用方也拿到新的
            return gateway
        last = gateway
    raise AssertionError(f"换了 5 个端口都绑不上，最后一个：{last}")


def _wait_for_audit_file(path, expected_lines: int,
                        timeout: float = 10.0) -> str:
    """等审计 JSONL 真的落下来再读。

    落盘**刻意晚于投递**：``host_timeout`` 是投递失败才知道的，JSONL 是追加写的，
    只能写最终状态（见 submit 里的注释）。代价是客户端一收到回填，文件可能还没写。
    所以这里等，而不是假设它在——"收到响应 = 文件已写"是个假等式，
    实测会偶发 FileNotFoundError。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            written = path.read_text(encoding="utf-8")
        except OSError:
            written = ""
        if len([line for line in written.splitlines() if line.strip()]) >= expected_lines:
            return written
        time.sleep(0.005)
    raise AssertionError(
        f"审计文件在 {timeout}s 内没落够 {expected_lines} 行：{path}")


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


def _post(port, payload: dict, path: str = HOOK_PATH, timeout: float = 30.0):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{int(port)}{path}", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _post_status(port, payload: dict, timeout: float = 30.0) -> tuple[int, dict]:
    """发请求并原样拿回状态码——503 会抛 HTTPError，这里替调用方接住。"""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{int(port)}{HOOK_PATH}", data=body,
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
    severity, rule, action_class = assess_risk("Bash", {"command": "rm -rf /tmp/x"})
    assert severity == "critical"
    assert rule == "delete"
    # 类别才是门控认的那一维；规则名会随规则库变，类别不会（规格 §16.3 C3）
    assert action_class == ACTION_CLASS_IRREVERSIBLE


@pytest.mark.parametrize("command", ["ls -la", "git status", "npm run build"])
def test_assess_risk_defaults_to_notice(command: str) -> None:
    severity, rule, action_class = assess_risk("Bash", {"command": command})
    assert severity == "notice"
    assert rule == ""
    # 未判定按**可恢复**算：宁可漏，不可错杀
    assert action_class == ACTION_CLASS_RECOVERABLE


def test_assess_risk_ignores_non_bash_tools() -> None:
    """Read/Glob 之类没有 command 字段；不猜它干什么。"""
    assert assess_risk("Read", {"file_path": "x"}) == (
        "notice", "", ACTION_CLASS_RECOVERABLE)
    assert assess_risk("Bash", None) == (
        "notice", "", ACTION_CLASS_RECOVERABLE)
    assert assess_risk("Bash", {"command": "rm -rf x"}) == (
        "critical", "delete", ACTION_CLASS_IRREVERSIBLE)


# ── 三层门控（规格 §1） ─────────────────────────────────────────────────────

# 已签字的授权范围：L2 相关用例默认带上它，单独验"没授权"的用例再显式去掉。
SIGNED = AUTH_SCOPES


def test_gate_defaults_to_closed() -> None:
    """默认全关：auto_decide=off 时其余参数怎么设都不放行。"""
    assert auto_decide_gate(severity="notice", ai_judgement=True,
                            has_key=True, auto_decide=AUTO_OFF,
                            max_severity="notice", authorized_scopes=SIGNED)[0] is False


def test_gate_requires_l1_first() -> None:
    """不能跳级：没开 ai_judgement 就没有解释能力，此时自动决定是黑箱代办。"""
    eligible, why = auto_decide_gate(severity="notice", ai_judgement=False,
                                     has_key=True, auto_decide=AUTO_LOW_RISK_ONLY,
                                     max_severity="notice", authorized_scopes=SIGNED)
    assert eligible is False
    assert "ai_judgement" in why


def test_gate_requires_a_key() -> None:
    eligible, why = auto_decide_gate(severity="notice", ai_judgement=True,
                                     has_key=False, auto_decide=AUTO_LOW_RISK_ONLY,
                                     max_severity="notice", authorized_scopes=SIGNED)
    assert eligible is False
    assert "api_key" in why


def test_gate_requires_an_explicit_authorization() -> None:
    """规格 §2 硬规则 2：配置全开也不够，用户必须在授权入口签过字。

    这是"没有显式授权就不执行任何明确操作"的机械保证——光写配置不生效。
    """
    for scopes in ((), (AUTH_SCOPE_ADVICE,), (AUTH_SCOPE_AUTO,)):
        eligible, why = auto_decide_gate(
            severity="notice", ai_judgement=True, has_key=True,
            auto_decide=AUTO_LOW_RISK_ONLY, max_severity="notice",
            authorized_scopes=scopes)
        assert eligible is False, f"授权范围 {scopes} 居然放行了"
        assert "授权" in why


@pytest.mark.parametrize("rule_name", ["delete", "disk_root", "system_dir",
                                        "shadow_copy", "format_volume", "boot_entry"])
def test_gate_exclusion_does_not_depend_on_the_rule_name(rule_name: str) -> None:
    """C3 的核心：排除判据是**动作类别**，不是规则名。

    规则名会随规则库增删而变（C1 之后"删除类"会拆成好几条）。靠规则名排除，
    每加一条规则都得回来改门控，漏改的那条配上阈值开到顶就被 L2 放行了。
    所以这里用六个不同的规则名配同一个不可恢复类别，要求**全部**被挡住。
    """
    for max_severity in ("info", "notice", "warn", "critical"):
        eligible, why = auto_decide_gate(
            severity="critical", action_class=ACTION_CLASS_IRREVERSIBLE,
            ai_judgement=True, has_key=True,
            auto_decide=AUTO_LOW_RISK_ONLY, max_severity=max_severity,
            authorized_scopes=SIGNED)
        assert eligible is False, f"规则 {rule_name} / 阈值 {max_severity} 居然放行了"
        assert "永不进入 L2" in why


def test_critical_severity_is_never_auto_allowed() -> None:
    """第二道墙：极高危档本身不可自动放行，与动作类别无关（规格 §3）。

    防的是"将来某条规则忘了声明类别"——那时类别默认按可恢复算，
    若没有这道墙，一个 critical 动作就能被 L2 放行。
    """
    for max_severity in ("info", "notice", "warn", "critical"):
        eligible, why = auto_decide_gate(
            severity="critical", action_class=ACTION_CLASS_RECOVERABLE,
            ai_judgement=True, has_key=True,
            auto_decide=AUTO_LOW_RISK_ONLY, max_severity=max_severity,
            authorized_scopes=SIGNED)
        assert eligible is False, f"阈值 {max_severity} 放行了 critical"
        assert "极高危档" in why


def test_gate_takes_no_rule_argument() -> None:
    """门控签名里**刻意没有 rule**——收了就会有人顺着它写规则名判断。"""
    import inspect

    params = set(inspect.signature(auto_decide_gate).parameters)
    assert "rule" not in params, "门控又收 rule 了，规则名排除会跟着回来"
    assert "action_class" in params


@pytest.mark.parametrize("configured,expected", [
    ("notice", "notice"),
    ("warn", "warn"),
    ("critical", "warn"),        # 极高危档不可自动放行 → 压回 warn
    ("CRITICAL", "warn"),
    ("", "notice"),
    ("不认识的档", "notice"),
])
def test_max_severity_is_clamped_below_critical(configured: str, expected: str) -> None:
    """阈值旋钮最高只能开到 warn（规格 §3 + C3）。

    开到 critical 要么是没理解这个旋钮管什么，要么是想绕过极高危墙；
    两种情况下压回去并留日志，都比默默接受一个"开了却开不动"的配置好。
    """
    assert effective_max_severity(configured) == expected


def test_gate_respects_the_severity_threshold() -> None:
    eligible, why = auto_decide_gate(severity="warn", ai_judgement=True,
                                     has_key=True, auto_decide=AUTO_LOW_RISK_ONLY,
                                     max_severity="notice", authorized_scopes=SIGNED)
    assert eligible is False
    assert "超过阈值" in why


def test_gate_opens_when_all_six_hold() -> None:
    eligible, why = auto_decide_gate(severity="notice", ai_judgement=True,
                                     has_key=True, auto_decide=AUTO_LOW_RISK_ONLY,
                                     max_severity="notice", authorized_scopes=SIGNED)
    assert eligible is True
    assert why == "L2 门控通过"


def test_advice_state_degrades_without_silently_succeeding() -> None:
    """四种状态都不许假装成功。"""
    assert advice_state(ai_judgement=False, has_key=False,
                        backend_ready=False) == "disabled"
    # 配置开了但没签字：必须说"未授权"，不能显示成"没有 key"——那是两件事
    assert advice_state(ai_judgement=True, has_key=True,
                        backend_ready=False, authorized=False) == "unauthorized"
    assert advice_state(ai_judgement=True, has_key=False,
                        backend_ready=False) == "no_key"
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


def test_deny_message_forbids_retrying_in_another_form(port: int) -> None:
    """规格 §18.2 F3：deny 的 message 必须是**禁止重试的硬指令**。

    真机实测：旧文案只写"用户拒绝了这次调用"，Claude Code 把它理解成
    "命令失败/路径写法有问题"，于是回「需要我改用这个形式再试一次吗？」——
    越拦越忙。所以这句话是写给 Claude 看的指令，不是写给用户的陈述。
    """
    gateway = _gateway(port)
    try:
        body = _decide_later(gateway, port, PAYLOAD, APPROVE_DENY)
        message = body["hookSpecificOutput"]["decision"]["message"]
        assert "不要换写法重试" in message
        assert "不要绕过" in message
        assert "等价" in message
        assert "等用户的下一步指示" in message
    finally:
        gateway.stop()


def test_deny_message_is_not_merely_a_statement(port: int) -> None:
    """反向守护：别退化成"用户拒绝了这次调用"那种陈述句。

    上面那条测试锁的是"该有的都有"，这条锁的是"不该出现的别出现"——
    两件事都要有人守，否则一次"简化文案"就会把 F3 带回来。
    """
    gateway = _gateway(port)
    try:
        body = _decide_later(gateway, port, PAYLOAD, APPROVE_DENY)
        message = body["hookSpecificOutput"]["decision"]["message"]
        assert message != "同桌猫：用户在 N.E.K.O 里拒绝了这次调用"
        # 至少出现一个祈使/禁止词，说明它是在下指令而不是在陈述
        assert any(word in message for word in ("不要", "停下", "等用户"))
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
        written = _wait_for_audit_file(tmp_path / "audit.jsonl", 1).strip()
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
        # 文案只写「宿主已自行处理，本插件未做判断」，不写"拒绝"
        assert "宿主已自行处理" in body["message"]
        assert "本插件未做判断" in body["message"]
        assert "拒绝" not in body["message"]

        rows = gateway.audit_rows()
        assert len(rows) == 1
        assert rows[0]["decided_by"] == DECIDED_BY_EXPIRED
        assert rows[0]["decision"] is None         # 审计里也记"没发决定"
        assert gateway.stats()["expired"] == 1
        assert gateway.undecided_count() == 0
    finally:
        gateway.stop()


def test_audit_row_exists_the_moment_the_client_gets_its_answer(port: int) -> None:
    """客户端一收到回填，审计行就必须已经在内存里。

    曾经的顺序是"先投递、后记账"，于是 `_post_status()` 返回后立刻读
    ``audit_rows()`` 会偶发少一行——实测 CI seed 下约 1/4 概率红一次。
    这不是测试写法问题，是产品语义问题：用户刚点的决定却查不到，像丢了。
    所以审计行进内存必须早于投递；只有**落盘**要等投递结果
    （``host_timeout`` 是投递失败才知道的，JSONL 是追加写的）。
    """
    gateway = _gateway(port, timeout=0.3, audit_path=None)
    try:
        status, _ = _post_status(port, PAYLOAD)
        assert status == 503
        # 不做任何等待：返回的这一刻就必须查得到
        rows = gateway.audit_rows()
        assert len(rows) == 1, f"回填已送达但审计行还没落：{rows}"
        assert rows[0]["decided_by"] == DECIDED_BY_EXPIRED
    finally:
        gateway.stop()


def test_expiry_audit_never_says_deny(port: int, tmp_path: Path) -> None:
    """规格 §2 硬规则 1：到期**不记录成「已拒绝」**。

    审计是最容易被误读的地方——一旦某行写着 deny，事后就说不清
    "是用户拒的还是插件拒的"。所以整份审计文本里都不许出现拒绝字样。
    """
    audit = tmp_path / "audit.jsonl"
    gateway = _gateway(port, timeout=0.3, audit_path=audit)
    try:
        _post_status(port, PAYLOAD)
        row = gateway.audit_rows()[0]
        assert row["decision"] is None
        assert row["decided_by"] == DECIDED_BY_EXPIRED
        text = gateway.audit_text()
        assert "deny" not in text
        assert "拒绝" not in text
        assert "无决定" in text          # 明写"没有决定"，不是"决定为拒绝"
        # 落盘的 JSONL 同样不能有
        written = _wait_for_audit_file(audit, 1)
        assert "deny" not in written
        assert "拒绝" not in written
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

    就算用户把 L2 全开、签了字、阈值调到最高，删除类命中后仍然只标 expired
    ——而且到期是"没决定"，不是"自动拒绝"。
    """
    gateway = _l2_gateway(port, lambda r: {"decision": "allow", "model": "m",
                                          "reason": "低风险"},
                          auto_decide_max_severity="critical")
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
    """门控算得过但没接判断后端（5a）→ 不自动决定，转交用户。

    这正是"插件不许因为门控通过就替用户判断"的机械保证。
    """
    gateway = _l2_gateway(port, None)
    try:
        payload = dict(PAYLOAD, tool_input={"command": "ls -la"})
        status, body = _post_status(port, payload)
        assert status == 503
        assert "hookSpecificOutput" not in body
        rows = gateway.audit_rows()
        assert rows[0]["decided_by"] == DECIDED_BY_EXPIRED
        assert rows[0]["decision"] is None
        assert any("不自动决定" in message for _, message in gateway._logger.lines)
    finally:
        gateway.stop()


def test_l2_needs_authorization_before_it_fires(port: int) -> None:
    """配置全开 + 后端就绪，但用户没签字 → 依然不自动决定。

    这是"没有显式授权就不执行任何明确操作"的端到端版本：单测里把授权去掉，
    其余条件全部满足，看它是否仍然什么都不做。
    """
    gateway = _l2_gateway(port, lambda r: {"decision": "allow", "model": "m",
                                          "reason": "低风险"},
                          authorize=False)
    try:
        status, body = _post_status(port, _low_risk_payload())
        assert status == 503
        assert "hookSpecificOutput" not in body
        assert gateway.audit_rows()[0]["decided_by"] == DECIDED_BY_EXPIRED
        assert gateway.stats()["ai"] == 0
    finally:
        gateway.stop()


# ── L2 自动放行（5b 的接缝；5a 用假后端验通路） ────────────────────────────

def _low_risk_payload() -> dict:
    return dict(PAYLOAD, tool_input={"command": "ls -la"})


def _l2_gateway(port: int, judge, *, authorize: bool = True,
                auto_decide_max_severity: str = "notice",
                timeout: float = 0.4) -> ApprovalGateway:
    gateway = _gateway(port, timeout=timeout, ai_judgement=True, has_api_key=True,
                       auto_decide=AUTO_LOW_RISK_ONLY,
                       auto_decide_max_severity=auto_decide_max_severity, judge=judge)
    if authorize:
        gateway.grant_authorization(AUTH_SCOPES)
    return gateway


def test_l2_auto_allow_is_audited_with_model_and_reason(port: int) -> None:
    gateway = _l2_gateway(port, lambda r: {"decision": "allow",
                                           "model": "deepseek-chat",
                                           "reason": "只读命令，无副作用"})
    try:
        status, body = _post_status(port, _low_risk_payload())
        assert status == 200
        decision = body["hookSpecificOutput"]["decision"]
        assert decision["behavior"] == "allow"
        # allow 不带 message（官方 schema：message/interrupt 是 deny 专属字段）
        assert "message" not in decision

        row = gateway.audit_rows()[0]
        assert row["decided_by"] == DECIDED_BY_AI
        assert row["decision"] == "allow"
        assert row["model"] == "deepseek-chat"
        assert row["reason"] == "只读命令，无副作用"
        # 授权溯源必须能在审计里查到：谁授权的、授权到什么范围、哪个版本
        assert "ai_judgement" in row["authorization"]
        assert "auto_decide" in row["authorization"]
        assert "v1" in row["authorization"]
        assert gateway.stats()["ai"] == 1
    finally:
        gateway.stop()


def test_l2_decides_at_event_time_not_at_expiry(port: int) -> None:
    """规格 §2 硬规则 4：事件发生那一刻就判，不让用户白等 570 秒。

    用 8 秒的到期窗口：如果实现是"等到期才判"，这个用例会等满 8 秒才回填，
    耗时直接暴露它。判定条件用耗时而不是等待循环，flaky 更低。
    """
    gateway = _l2_gateway(port, lambda r: {"decision": "allow", "model": "m",
                                          "reason": "低风险"}, timeout=8.0)
    try:
        started = time.monotonic()
        status, _ = _post_status(port, _low_risk_payload(), timeout=30.0)
        elapsed = time.monotonic() - started
        assert status == 200
        assert elapsed < 3.0, f"自动放行花了 {elapsed:.1f}s，像是等到期才判的"
    finally:
        gateway.stop()


def test_l2_never_auto_denies(port: int) -> None:
    """规格 §2 硬规则 3：L2 只可能 auto-allow，**永不 auto-deny**。

    后端说 deny 也只是个建议：插件不代用户拒绝，把卡片摆出来让人亲手点。
    """
    gateway = _l2_gateway(port, lambda r: {"decision": "deny", "model": "m",
                                          "reason": "看起来在动不该动的东西"},
                          timeout=30.0)
    try:
        result: dict = {}
        thread = threading.Thread(
            target=lambda: result.update(response=_post(port, _low_risk_payload())),
            daemon=True)
        thread.start()
        # 没有自动回填：请求应该还挂着等用户
        _wait_for_pending(gateway)
        assert gateway.undecided_count() == 1
        assert not result, "后端说 deny，插件却自己回填了"
        pending = _only_pending(gateway)
        gateway.resolve(pending.request.request_id, APPROVE_DENY)
        thread.join(timeout=10)

        decision = result["response"][1]["hookSpecificOutput"]["decision"]
        assert decision["behavior"] == "deny"
        row = gateway.audit_rows()[0]
        # 是用户点的 deny，不是 AI 代决的 deny
        assert row["decided_by"] == DECIDED_BY_USER
        assert gateway.stats()["ai"] == 0
    finally:
        gateway.stop()


def test_l2_auto_allow_does_not_pop_a_card(port: int) -> None:
    """自动放行不弹卡片——要人点就失去意义。

    但它必须留下一条可通知的记录（``take_auto_allowed``），否则用户根本看不见
    放过什么。看不见的放行等于后门。
    """
    gateway = _l2_gateway(port, lambda r: {"decision": "allow", "model": "m",
                                          "reason": "低风险"})
    try:
        status, _ = _post_status(port, _low_risk_payload())
        assert status == 200
        assert gateway.take_new() == []                 # 不建卡
        assert gateway.undecided_count() == 0
        auto = gateway.take_auto_allowed()
        assert len(auto) == 1
        assert auto[0].judge_model == "m"
        assert gateway.take_auto_allowed() == []        # 只通知一次
    finally:
        gateway.stop()


@pytest.mark.parametrize("bad", [None, {}, {"decision": "也许吧"}, "不是dict"])
def test_l2_invalid_verdict_falls_back_to_expired(port: int, bad: object) -> None:
    """判断后端给出无效决定 → 不自动决定，请求留给用户，走到期。"""
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
    """后端抛异常不能炸掉审批——不自动决定，转交用户。"""

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
    """用户点了就以用户为准——L2 判完之前用户就点了，不许覆盖用户的决定。

    判断后端刻意放慢（等用户先点）：不这么做，"用户点"和"AI 判"谁先谁后
    是竞态，测出来的东西不稳定。要验的正是"用户已经决定了，AI 不许翻案"。
    """
    clicked = threading.Event()

    def slow_judge(_: PendingApproval) -> dict:
        clicked.wait(timeout=5.0)
        return {"decision": "allow", "model": "m", "reason": "低风险"}

    gateway = _gateway(port, timeout=30.0, ai_judgement=True, has_api_key=True,
                       auto_decide=AUTO_LOW_RISK_ONLY,
                       auto_decide_max_severity="notice", judge=slow_judge)
    gateway.grant_authorization(AUTH_SCOPES)
    try:
        result: dict = {}
        thread = threading.Thread(
            target=lambda: result.update(response=_post(port, _low_risk_payload())),
            daemon=True)
        thread.start()
        pending = _only_pending(gateway)
        gateway.resolve(pending.request.request_id, APPROVE_DENY)
        clicked.set()
        thread.join(timeout=10)

        behavior = result["response"][1]["hookSpecificOutput"]["decision"]["behavior"]
        assert behavior == "deny"
        assert gateway.audit_rows()[0]["decided_by"] == DECIDED_BY_USER
        assert gateway.stats()["ai"] == 0
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
        sock = socket.create_connection(("127.0.0.1", int(port)), timeout=10)
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
        with socket.create_connection(("127.0.0.1", int(port)), timeout=5):
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
            f"http://127.0.0.1:{int(port)}{HOOK_PATH}",
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

def _visible(html: str) -> str:
    """剥掉标签与实体之后的纯文本。

    断言"某个词有没有泄到界面上"时必须看**可见文本**：按钮上的
    ``data-neko-action="deskmate_deny"`` 是内部 id，不是给用户看的字，
    按整个 HTML 搜会把这种合法内容误判成泄漏（第一版就这么误报过）。
    """
    import html as _html
    import re as _re

    return _html.unescape(_re.sub(r"<[^>]+>", "", html))


def _card(**overrides) -> str:
    base = dict(subject="claude-code", tool_summary="Bash: rm -f x.txt",
                session_id="800257f3", working_directory=r"D:\workspace",
                timeout_seconds=570, command="rm -f D:/other/x.txt",
                conclusion=CONCLUSION_DENY, advice_state=ADVICE_STATE_READY)
    base.update(overrides)
    return build_card_html(**base)


def test_card_html_has_all_four_blocks() -> None:
    html = _card()
    # 1) 标题
    assert "同桌猫 · 审批请求" in html
    # 2) 定位
    assert "会话 800257f3 @ D:\workspace" in html
    # 3) 主卡正文固定四行（规格 P3）
    assert "⚠️ 它要删除文件 D:/other/x.txt" in html
    assert "后果：不进回收站，删了就找不回来" in html
    assert "范围：这个文件在它的工作目录之外" in html
    # 4) 结论行
    assert "<strong>建议拒绝</strong>" in html
    # 5) 来源与超时说明
    assert "claude-code 请求执行" in html
    assert "宿主会自行处理" in html
    assert "视为未决定" in html
    assert "插件不代你判断" in html
    assert "自动拒绝" not in html
    # 6) 三个按钮
    for action in (ACTION_APPROVE, ACTION_DENY, ACTION_EXPLAIN):
        assert f'data-neko-action="{action}"' in html


def test_card_conclusion_line_is_independent_and_first() -> None:
    """规格 P2：结论独立成行、加粗，而且排在正文最前面。"""
    html = _card()
    verdict_at = html.index("<strong>建议拒绝</strong>")
    for later in ("⚠️ 它要", "后果：", "范围："):
        assert verdict_at < html.index(later), f"{later} 排在结论前面了"


@pytest.mark.parametrize("conclusion", CONCLUSIONS)
def test_card_accepts_only_the_three_legal_conclusions(conclusion: str) -> None:
    """规格 P2：结论只准三种，一个不多一个不少。"""
    html = _card(conclusion=conclusion, advice_state=ADVICE_STATE_READY)
    assert f"<strong>{conclusion}</strong>" in html


@pytest.mark.parametrize("conclusion", [
    "倾向别放行", "不建议", "最好不", "可以但", "拒绝或允许都可以",
    "allow", "deny", "", "建议放行", "同意", "批准", None,
])
def test_card_rejects_any_other_conclusion(conclusion: object) -> None:
    """非法结论一律落最保守的第三态，原文不外溢。"""
    html = _card(conclusion=conclusion, advice_state=ADVICE_STATE_READY)
    assert f"<strong>{CONCLUSION_UNSURE}</strong>" in html
    if isinstance(conclusion, str) and conclusion and conclusion != CONCLUSION_UNSURE:
        assert conclusion not in _visible(html), (
            f"非法结论 {conclusion!r} 外溢到卡片上了")


def test_conclusion_requires_a_real_advice_state() -> None:
    """建议层降级时，结论只能是第三态——哪怕调用方误传了一个结论。

    这是防"没有依据的建议允许出现在卡片上"：调用方说 ready 却给了降级的
    状态，或者状态是 disabled 却给了结论，两种都不许放行。
    """
    for state in ("disabled", "unauthorized", "no_key", "pending_backend"):
        html = _card(conclusion=CONCLUSION_ALLOW, advice_state=state)
        assert f"<strong>{CONCLUSION_UNSURE}</strong>" in html, state
        assert CONCLUSION_ALLOW not in _visible(html), state


def test_card_has_no_banned_words() -> None:
    """规格 P2：全文不出现 放行/倾向/不建议，也不出现内部词。"""
    for phrase in BANNED_PHRASES + BANNED_INTERNAL:
        assert phrase not in _visible(_card()), f"卡片出现了禁用词 {phrase!r}"


def test_card_has_no_english_command_tokens() -> None:
    """规格 P3：除路径外不许出现英文命令 token。"""
    text = _visible(_card(command="rm -rf D:/other/dir"))
    for token in ("rm -rf", "-rf", "--force", "rm -f", "sudo"):
        assert token not in text, f"卡片泄了命令 token {token!r}"


def test_card_scope_detects_a_system_directory() -> None:
    html = _card(command=r"rm -rf C:\Windows\System32")
    assert "这是系统目录" in html


def test_card_scope_says_inside_the_working_directory() -> None:
    html = _card(command="rm -rf D:/workspace/build")
    assert "在它的工作目录之内" in html


def test_card_unknown_command_is_not_guessed() -> None:
    """未命中的命令原样显示 + 明说未识别，不许瞎猜。"""
    html = _card(command="npm run build", conclusion="")
    assert "未识别的命令" in html
    assert "npm run build" in html
    assert "看不出" in html


def test_card_echo_of_a_delete_is_not_a_delete() -> None:
    """规格 §5.2：echo "rm -rf C:\\" 不算删除——误报会训练用户无脑点确认。"""
    html = _card(command='echo "rm -rf C:\\"')
    assert "未识别的命令" in html
    assert "清空整个磁盘" not in html


def test_card_highlights_high_risk_in_red() -> None:
    """规格 §2 硬规则 3：不可恢复动作 → 标红 + 明说不自动放行。"""
    html = _card(action_class=ACTION_CLASS_IRREVERSIBLE)
    assert 'class="risk"' in html
    assert "必须由你亲自点允许或拒绝" in html


def test_card_highlights_critical_even_without_a_class() -> None:
    """极高危档独立标红：万一将来某条规则忘了声明类别，红色也不能漏。"""
    html = _card(risk="critical")
    assert 'class="risk"' in html


def test_card_low_risk_is_not_red() -> None:
    """低风险不红：把每张卡都标成高风险，红色就没有信息量了。"""
    assert 'class="risk"' not in _card(risk="notice")


# ── C5：卡片文案不许泄配置键名与内部入口名 ─────────────────────────────────

@pytest.mark.parametrize("state", ["disabled", "unauthorized", "no_key",
                                   "pending_backend"])
def test_model_state_copy_is_a_plain_statement(state: str) -> None:
    """模型状态那句陈述：只说现在是什么状态，不指挥用户去配什么。

    它现在渲染在「这是什么」卡上（主卡正文是固定四行，放不下它），
    但性质不变：不许出现配置键名、不许出现内部入口名、不许索取。
    """
    text = advice_text(state)
    assert text in ADVICE_TEXT.values()
    for leaked in ("ai_judgement", "auto_decide", "api_key", "approval_authorize",
                   "key"):
        assert leaked not in text, f"{state} 泄了内部名词 {leaked!r}"
    for soliciting in ("填写", "即可", "配置后", "请调", "去确认"):
        assert soliciting not in text, f"{state} 出现了索取式措辞 {soliciting!r}"


@pytest.mark.parametrize("state", ["disabled", "no_key", "pending_backend"])
def test_model_state_copy_never_implies_the_gate_needs_a_key(state: str) -> None:
    """零 key 时也不许暗示闸门不工作——安全兜底与模型无关（规格 §12）。"""
    assert "安全兜底与模型无关" in advice_text(state)


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
    note = "已过期：宿主已自行处理，本插件未做判断。"
    html = build_result_html(note)
    assert "已过期" in html
    assert "自动拒绝" not in html
    assert "拒绝" not in html


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


# ── 显式授权（规格 §2 硬规则 2） ───────────────────────────────────────────

def test_authorization_starts_absent() -> None:
    """默认没签字：L1/L2 都不可用，哪怕配置全开。"""
    gateway = ApprovalGateway(ListLogger(), port=_free_port(), ai_judgement=True,
                              auto_decide=AUTO_LOW_RISK_ONLY, has_api_key=True)
    assert gateway.authorized_scopes() == ()
    snapshot = gateway.authorization_snapshot()
    assert snapshot["granted"] is False
    assert snapshot["scopes"] == []
    # 文案里必须有"可能出错"和"用户担责"两句——那是用户签字前该看到的话
    assert "模型" in snapshot["text"]
    assert AUTHORIZATION_HEADLINE in snapshot["headline"]
    assert gateway.advice_state() == "unauthorized"


def test_grant_writes_an_audit_row_with_scope_and_version(tmp_path: Path) -> None:
    """确认记录写进审计：时间、范围、授权版本（规格 §2 硬规则 2）。"""
    gateway = ApprovalGateway(ListLogger(), port=_free_port(),
                              authorization_path=tmp_path / "authorization.json")
    record = gateway.grant_authorization([AUTH_SCOPE_AUTO, AUTH_SCOPE_ADVICE,
                                          "不是合法范围"])
    assert set(record.scopes) == set(AUTH_SCOPES)     # 非法范围被丢掉，不猜
    assert set(gateway.authorized_scopes()) == set(AUTH_SCOPES)

    rows = gateway.audit_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "authorization"
    assert row["decided_by"] == DECIDED_BY_AUTHORIZATION
    assert row["decision"] is None                    # 授权不是一次审批决定
    assert "ai_judgement" in row["authorization"]
    assert "auto_decide" in row["authorization"]
    assert "v1" in row["authorization"]
    assert "承担后果" in row["reason"]
    assert row["time"]                                # ISO 时间


def test_authorization_survives_a_restart(tmp_path: Path) -> None:
    """重启不吞掉用户已经做过的确认——但要靠配置开关才真正生效。"""
    path = tmp_path / "authorization.json"
    first = ApprovalGateway(ListLogger(), port=_free_port(),
                            authorization_path=path)
    first.grant_authorization(AUTH_SCOPES)

    second = ApprovalGateway(ListLogger(), port=_free_port(),
                             authorization_path=path)
    assert second.authorized_scopes() == AUTH_SCOPES

    # 授权在手，但配置关着 → 依然不动作（恢复授权 ≠ 自动开启）
    closed = ApprovalGateway(ListLogger(), port=_free_port(),
                             authorization_path=path,
                             ai_judgement=False, auto_decide=AUTO_OFF)
    assert closed.advice_state() == "disabled"
    assert closed.policy_snapshot()["auto_decide"] == AUTO_OFF


def test_authorization_file_is_not_a_remote_switch(tmp_path: Path) -> None:
    """读不懂/被篡改的授权文件当没有处理，不猜、不半效。"""
    path = tmp_path / "authorization.json"
    path.write_text("{ 不是 json", encoding="utf-8")
    assert load_authorization(path) is None
    path.write_text('{"scopes": ["nonsense"], "version": 1}', encoding="utf-8")
    record = load_authorization(path)
    # 能解析（scopes 是合法字段），但范围不在白名单里 → 等于没授权
    assert record is not None
    assert not set(record.scopes) & set(AUTH_SCOPES)
    assert load_authorization(None) is None
    assert load_authorization(tmp_path / "missing.json") is None


def test_grant_rejects_an_empty_scope_list() -> None:
    gateway = ApprovalGateway(ListLogger(), port=_free_port())
    with pytest.raises(ValueError):
        gateway.grant_authorization([])
    with pytest.raises(ValueError):
        gateway.grant_authorization(["完全不相干的东西"])
    assert gateway.authorized_scopes() == ()


def test_authorize_card_carries_the_consent_text() -> None:
    """授权卡片的文案是验收项：必须含"可能出错"与"用户担责"。"""
    html = build_authorize_html(scopes=AUTH_SCOPES)
    assert AUTHORIZATION_HEADLINE in html
    assert "决定由你授权并承担后果" in html
    assert "ai_judgement" in html and "auto_decide" in html
    assert f'data-neko-action="{ACTION_AUTH_CONFIRM}"' in html
    assert f'data-neko-action="{ACTION_AUTH_CANCEL}"' in html
    # 两个按钮都指向授权入口，用 args 区分签不签字
    actions = build_authorize_actions(AUTH_SCOPES)
    assert set(actions) == {ACTION_AUTH_CONFIRM, ACTION_AUTH_CANCEL}
    assert actions[ACTION_AUTH_CONFIRM]["entry"] == ENTRY_AUTHORIZE
    assert actions[ACTION_AUTH_CONFIRM]["args"]["confirm"] is True
    assert actions[ACTION_AUTH_CANCEL]["args"]["confirm"] is False


# ── 纪律：没有显式授权就不动作（规格 §2 硬规则 2） ─────────────────────────

def test_policy_is_read_only_at_runtime() -> None:
    """门控开关与授权只能来自用户配置 + 用户签字；运行期没有任何方法能改它们。"""
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
    gateway.advice_state()
    gateway.take_new()
    gateway.take_auto_allowed()

    assert gateway.policy_snapshot() == before
    assert before["ai_judgement"] is False
    assert before["auto_decide"] == AUTO_OFF
    assert before["authorized_scopes"] == []


def test_no_method_enables_the_policy() -> None:
    """反向守护：ApprovalGateway 上不存在能自我授权/自我开门的公开方法。

    ``grant_authorization`` 是唯一能写授权的方法，而它只能由用户在授权卡上点
    按钮触发——没有定时任务、hook、配置变更会调用它（``__init__.py`` 的
    ``on_config_change`` 刻意不碰它，另有测试盯着）。
    """
    forbidden = {"enable_ai", "enable_auto", "set_auto_decide", "set_ai_judgement",
                 "turn_on", "authorize", "auto_authorize", "self_authorize"}
    public = {name for name in dir(ApprovalGateway) if not name.startswith("_")}
    assert not (forbidden & public), f"出现了能自我授权的方法：{forbidden & public}"
    # grant_authorization 必须存在（否则用户无法授权），但名字不许在禁用表里
    assert "grant_authorization" in public


def test_config_change_does_not_touch_the_authorization() -> None:
    """on_config_change 不许恢复/修改授权——否则配置变更就能自我授权。

    这条守着"插件没有运行期自行打开授权的路径"。被测代码在 __init__.py，
    这里用源码断言而不是实例调用：要的是"这个方法里没有授权相关调用"。
    """
    import inspect

    from plugin.plugins import agent_deskmate

    source = inspect.getsource(agent_deskmate.AgentDeskmatePlugin.on_config_change)
    for token in ("grant_authorization", "_authorization", "authorization_path"):
        assert token not in source, f"on_config_change 里出现了 {token}"


# ── 响应体形状 ──────────────────────────────────────────────────────────────

def test_hook_response_shape() -> None:
    assert hook_response(APPROVE_ALLOW) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }


def test_allow_carries_no_rewrite_fields() -> None:
    """规格 P5.2：allow 时不带 updatedInput / updatedPermissions。

    我们没有要改写的参数，那就什么都别带。空对象在某些版本可能被理解成
    "把参数清空"——少给一个字段比多给一个可能被误解的字段安全。
    """
    decision = hook_response(APPROVE_ALLOW)["hookSpecificOutput"]["decision"]
    assert "updatedInput" not in decision
    assert "updatedPermissions" not in decision
    assert "message" not in decision


def test_deny_still_carries_the_full_shape() -> None:
    """deny 仍按官方示例带齐三个字段 + message + interrupt。"""
    decision = hook_response(APPROVE_DENY,
                             message="同桌猫：用户拒绝了")["hookSpecificOutput"]["decision"]
    assert decision["updatedInput"] == {}
    assert decision["updatedPermissions"] == []
    assert decision["interrupt"] is False
    assert "拒绝" in decision["message"]


def test_no_decision_response_has_no_decision() -> None:
    body = no_decision_response(reason="no_decision", timeout_seconds=570)
    assert body["ok"] is False
    assert "hookSpecificOutput" not in body
    assert "宿主已自行处理" in body["message"]
    assert "本插件未做判断" in body["message"]
    assert "拒绝" not in body["message"]


def test_no_decision_response_says_it_is_not_a_fault() -> None:
    """F4：只写"宿主已自行处理"时，CC 把到期理解成"hook 服务挂了"，
    反过来让用户去查端口——白担心一场。所以必须补一句"这是在等你，不是坏了"。
    """
    message = no_decision_response(reason="no_decision",
                                   timeout_seconds=570)["message"]
    assert "不是故障" in message
    assert "没有代替你作答" in message
    # 顺带守住规则 1 的不变式：到期文案里不许出现"拒绝"字样，
    # 一个都不行——出现了就等于把"没决定"写成了"决定拒绝"。
    assert "拒绝" not in message


def test_no_decision_response_has_no_internal_jargon() -> None:
    """给 CC 看的话里不出现内部词：端口号、地址、onFailure、503。

    那些是给我们排查用的。写进给 CC 看的话里，它只会顺着往下猜
    （"是不是端口被占了"），把用户带偏。
    """
    message = no_decision_response(reason="no_decision",
                                   timeout_seconds=570)["message"]
    for jargon in ("端口", "onFailure", "127.0.0.1", "503", "hook 服务"):
        assert jargon not in message, f"到期文案泄了内部词 {jargon!r}"


def test_stats_shape() -> None:
    gateway = ApprovalGateway(ListLogger(), port=_free_port())
    assert gateway.stats() == {"pending": 0, "allow": 0, "deny": 0, "ai": 0,
                               "expired": 0, "host_timeout": 0,
                               "authorizations": 0, "running": 0}


def test_policy_snapshot_reflects_config() -> None:
    gateway = ApprovalGateway(ListLogger(), port=_free_port(), ai_judgement=True,
                              auto_decide=AUTO_LOW_RISK_ONLY, has_api_key=True,
                              auto_decide_max_severity="warn")
    assert gateway.policy_snapshot() == {
        "ai_judgement": True, "auto_decide": AUTO_LOW_RISK_ONLY,
        "auto_decide_max_severity": "warn", "has_api_key": True,
        "judge_connected": False, "authorized_scopes": [],
    }
    # 签字后快照要能反映出来——用户要能从 status 入口看到自己授了什么
    gateway.grant_authorization(AUTH_SCOPES)
    assert gateway.policy_snapshot()["authorized_scopes"] == list(AUTH_SCOPES)


def test_gateway_clamps_the_threshold_at_construction() -> None:
    """配置写成 critical 时，网关建起来就是 warn（规格 §3 + C3）。

    只夹纯函数不够：配置是从文件读进来的，必须确认网关真的用了夹过的值，
    否则 `auto_decide_max_severity = "critical"` 会原样进 policy_snapshot，
    用户在 status 入口看到的是一个"开了却开不动"的假象。
    """
    gateway = ApprovalGateway(ListLogger(), port=_free_port(),
                              auto_decide_max_severity="critical")
    assert gateway.policy_snapshot()["auto_decide_max_severity"] == "warn"


def test_audit_records_the_action_class(port: int) -> None:
    """审计要能看出"当时判定这是不可恢复动作"——不能只剩一个规则名。

    规则名会随规则库改（C1 之后"delete"可能变成"disk_root"），
    事后翻审计时类别才是稳定的那一维。
    """
    gateway = _gateway(port, timeout=0.3)
    try:
        _post_status(port, PAYLOAD)          # rm -rf build/ → 删除类
        row = gateway.audit_rows()[0]
        assert row["action_class"] == ACTION_CLASS_IRREVERSIBLE
        assert row["severity"] == "critical"
        assert "class=irreversible" in gateway.audit_text()

        gateway2 = _gateway(_free_port(), timeout=0.3)
        try:
            _post_status(gateway2.port, dict(PAYLOAD,
                                             tool_input={"command": "ls -la"}))
            row = gateway2.audit_rows()[0]
            assert row["action_class"] == ACTION_CLASS_RECOVERABLE
        finally:
            gateway2.stop()
    finally:
        gateway.stop()


def test_authorization_record_round_trips_through_json() -> None:
    record = AuthorizationRecord(scopes=AUTH_SCOPES, confirmed_by="user",
                                 note="测试")
    restored = AuthorizationRecord.model_validate(json.loads(record.model_dump_json()))
    assert restored.scopes == AUTH_SCOPES
    assert restored.version == 1
    assert "v1" in restored.as_audit_text()
    assert restored.as_dict()["scopes"] == list(AUTH_SCOPES)
    assert restored.as_dict()["confirmed_time"]


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
