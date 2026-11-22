"""P0 / P1 / P2 / P3 回归：重载后能轮询、审查期不弹卡、通知固定三行。

三个真机症状，三条修法，这份测试把它们钉在一起：

**P0**：重载后每 3 秒一次 ``AttributeError: _source_state``，播报全停。
根因是观测层的可变状态只在 ``on_config_change`` 里创建，而 ``watch_tick``
不等配置变更就可能跑第一轮。修法是把状态集中到 ``_reset_watch_state()``
并让 ``__init__`` 就调。

**P1**：已经自动放行的请求仍然弹出一张带「允许 / 拒绝」的卡片。
建卡 tick 是 1 秒、模型审查要 1-2 秒，卡先弹出来——那一屏按钮等于请用户
点一个已经决定完的动作。修法是审查期间 ``record.reviewing`` 按住建卡，
且自动放行不许用 ``""`` 覆盖已经建出来的真 ``card_id``（否则 settle 直接
return，按钮永远留着）。

**P2 / P3**：自动放行的通知挤进了模型名、授权版本、机制说明和关闭方法，
主信息（到底放了什么）被埋在最里面。修法是固定三行，其余全进审计。
"""

from __future__ import annotations

import ast
import inspect
import json
import socket
from pathlib import Path

import pytest

import plugin.plugins.agent_deskmate as module
import plugin.plugins.agent_deskmate.approval as approval
from plugin.plugins.agent_deskmate import AgentDeskmatePlugin
from plugin.plugins.agent_deskmate.approval import (
    AUTH_SCOPE_REVIEW,
    ApprovalGateway,
    ApprovalRequest,
    PendingApproval,
    auto_allow_notice,
    auto_allow_setting_line,
    build_card_html,
    build_result_html,
)
from plugin.plugins.agent_deskmate.auto_review import (
    DECISION_ALLOW,
    OUTCOME_AUTO_ALLOW,
    RISK_LOW,
    Review,
)
from plugin.plugins.agent_deskmate.commands import BANNED_INTERNAL, first_banned

WD = r"D:\workspace\proj"


# ── 1. P0：构造之后立刻能轮询 ─────────────────────────────────────────────

class _FakeCtx:
    """够用的宿主上下文：只给插件构造时真正碰到的那几样。"""

    def __getattr__(self, name: str):
        raise AttributeError(name)


class _RecordingPush:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs: object) -> dict:
        self.calls.append(kwargs)
        return {"submitted": True}


def _tool_line(command: str, cwd: str = WD) -> str:
    return json.dumps({
        "type": "assistant",
        "sessionId": "sess-1",
        "isSidechain": False,
        "timestamp": "2026-11-23T06:00:00.000Z",
        "cwd": cwd,
        "message": {
            "role": "assistant",
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "t", "name": "Bash",
                         "input": {"command": command}}],
        },
    }, ensure_ascii=False)


def _projects(tmp_path: Path, commands: list[str]) -> Path:
    target = tmp_path / "projects" / "proj.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(_tool_line(c) for c in commands) + "\n",
                      encoding="utf-8")
    return target


def _config(tmp_path: Path) -> dict:
    return {"coalesce_seconds": 60,
            "watch": {"claude_projects_dir": str(tmp_path / "projects"),
                      "cc_switch_db": str(tmp_path / "no-such.db")}}


def _plugin() -> AgentDeskmatePlugin:
    """构造插件但不启动 timer / 审批服务。"""
    plugin = AgentDeskmatePlugin.__new__(AgentDeskmatePlugin)
    AgentDeskmatePlugin.__init__(plugin, _FakeCtx())
    return plugin


def test_a_freshly_constructed_plugin_can_poll() -> None:
    """**P0 的回归本体**：构造完立刻 poll，不许抛 AttributeError。"""
    plugin = _plugin()
    assert plugin._source_state == {}          # noqa: SLF001
    assert plugin._source_error_at == {}       # noqa: SLF001
    # _ensure_watchers 会往这两个 dict 里写——写之前它们必须存在
    plugin._ensure_watchers(_config(Path(".")))  # noqa: SLF001


def test_the_first_poll_after_startup_broadcasts(tmp_path: Path) -> None:
    """真机场景：启动 → 3 秒后第一轮 poll → 删除播报真的出去。"""
    _projects(tmp_path, ["ls -la"])
    plugin = _plugin()
    plugin.push_message = _RecordingPush()      # type: ignore[assignment]

    plugin._poll_once(_config(tmp_path))         # 第一轮：只记水位
    with (tmp_path / "projects" / "proj.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(_tool_line("rm -rf D:/other/x.txt") + "\n")
    outcomes = plugin._poll_once(_config(tmp_path))

    assert outcomes, "第一轮 poll 之后没有播报"
    assert plugin.push_message.calls            # type: ignore[attr-defined]
    assert "删除" in plugin.push_message.calls[0]["parts"][0]["text"]  # type: ignore[attr-defined]


def test_a_config_reload_keeps_the_state_consistent() -> None:
    """配置变更走的是同一条重置路径——两边不许出现第二套初始化。"""
    plugin = _plugin()
    plugin._ensure_watchers(_config(Path(".")))  # noqa: SLF001
    assert plugin._source_state, "重置后状态不该是空的（刚建过适配器）"
    plugin.on_config_change()
    assert plugin._source_state == {}            # noqa: SLF001
    assert plugin._source_error_at == {}         # noqa: SLF001
    # 重置之后照样能跑
    plugin._ensure_watchers(_config(Path(".")))  # noqa: SLF001


def test_every_watch_field_is_created_by_the_reset() -> None:
    """字段清单钉死：以后往观测层加状态，只改 ``_reset_watch_state``。"""
    plugin = _plugin()
    expected = {"_watch_paths", "_transcript_adapter", "_gateway_adapter",
                "_broadcaster", "_source_error_at", "_source_state"}
    assert expected <= set(vars(plugin))
    # 两个 dict 必须是**空 dict**，不是 None：调用方直接往里写
    assert plugin._source_error_at == {}         # noqa: SLF001
    assert plugin._source_state == {}            # noqa: SLF001


# ── 2. P1：审查期间不建卡；已建的卡不许被空串覆盖 ──────────────────────────

def _record(command: str, cwd: str = WD, **overrides) -> PendingApproval:
    request = ApprovalRequest(session_id="sess-1", request_id="r1",
                              tool_name="Bash", tool_summary=f"Bash: {command}",
                              full_command=command, working_directory=cwd)
    record = PendingApproval(request)
    record.severity = "warn"
    record.rule = ""
    record.action_class = "recoverable"
    record.risk_tier = "normal"
    for key, value in overrides.items():
        if hasattr(record, key):
            setattr(record, key, value)
    return record


def test_a_reviewing_record_gets_no_card() -> None:
    """P1 本体：等审查结果的那 1-2 秒里，不许弹出带按钮的卡。"""
    record = _record("npm run build")
    record.reviewing = True
    assert record.needs_card() is False
    # 审查结束（无论放行还是转人工）都要放开——转人工时下一轮 tick 就该建卡
    record.reviewing = False
    assert record.needs_card() is True


def test_an_auto_allowed_record_with_a_real_card_keeps_its_id(
        tmp_path: Path) -> None:
    """P1 另一半：竞态下卡片先建出来了，自动放行不许把真 id 冲成 ``""``。

    ``_settle_approval_card`` 开头就是 ``if not pending.card_id: return``——
    id 被冲掉的那张卡会永远留着「允许 / 拒绝」两个按钮，点下去得到的是
    一个已经执行完的动作。所以这里必须留着真 id，让 settle 把它改成
    一张没有按钮的结果卡（``needs_announce`` 为真才会走那条路）。
    """
    record = _record("npm run build")
    record.card_id = "card-123"
    record.auto = True
    reviewer = _FakeReviewer(Review(OUTCOME_AUTO_ALLOW, risk=RISK_LOW,
                                    decision=DECISION_ALLOW,
                                    reason="只读命令，无副作用", model="m1"))
    gateway = _gateway(reviewer, tmp_path)
    try:
        assert gateway._auto_allow_by_review(record, "门控通过") is True  # noqa: SLF001
        assert record.auto is True
        assert record.card_id == "card-123"
        assert record.needs_announce() is True
    finally:
        gateway.stop()


def test_the_settle_card_for_an_auto_allow_has_no_buttons() -> None:
    """结果卡只留三行话，按钮一个都不许有（红线 2 的镜像：没决定就不给按钮）。"""
    html = build_result_html(auto_allow_notice(
        tool_name="Bash", command="rm -rf build/", reason="构建产物，可重建",
        rule="delete"))
    assert "它要删除：build/" in html
    assert "data-neko-action" not in html
    assert "允许" not in html
    assert "拒绝" not in html


def _first_internal(text: str) -> str | None:
    """第一个撞到的内部词；没撞到返回 None。

    ``first_banned`` 拆开用：``BANNED_PHRASES`` 管的是主卡上的诱导词，
    通知与设置行要的是另一半（``BANNED_INTERNAL``）。
    """
    for phrase in BANNED_INTERNAL:
        if phrase in text:
            return phrase
    return None


# ── 3. P2：通知固定三行，只留人做判断需要的 ────────────────────────────────

@pytest.mark.parametrize("command,rule", [
    ("rm -rf build/", "delete"),
    ("npm run build", ""),
    ("git status", ""),
    ("python -c \"print(1)\"", ""),
])
def test_the_notice_is_exactly_three_lines(command: str, rule: str) -> None:
    """三行封顶：多一行都是把最该被看见的那行往下推。"""
    notice = auto_allow_notice(tool_name="Bash", command=command,
                               reason="只读命令，无副作用", rule=rule)
    assert notice.count("\n") == 2
    assert len(notice.splitlines()) == 3
    assert notice.splitlines()[0] == "已自动放行（低风险）"
    assert notice.splitlines()[2].startswith("理由：")


@pytest.mark.parametrize("command,rule,expected", [
    ("rm -rf build/", "delete", "它要删除：build/"),
    ('python -c "print(1)"', "", '它要执行：python -c "print(1)"'),
    # 目标解析不出来也要说"要删"——不说就等于把删除说成一件中性的事
    ("git clean -fdx", "delete", "它要删除："),
])
def test_the_notice_names_the_actual_action(command: str, rule: str,
                                            expected: str) -> None:
    """第二行说的是"它到底要干什么"，不是命令原文的复读。"""
    notice = auto_allow_notice(tool_name="Bash", command=command,
                               reason="可重建", rule=rule)
    assert expected in notice


def test_the_notice_never_claims_a_delete_it_cannot_name() -> None:
    """删不掉目标时退化成整条命令，但不许退化成「它要执行」。"""
    notice = auto_allow_notice(tool_name="Bash", command="git clean -fdx",
                               reason="缓存目录", rule="delete")
    assert "它要执行" not in notice
    assert "它要删除：git clean -fdx" in notice


# 通知里不许出现的东西：模型名、授权版本、机制说明、关闭方法、内部词。
# 前四样只进审计（audit_list 看得见），最后一样用户根本不知道那是什么。
_FORBIDDEN = ("模型建议", "让模型先审一次", "可关闭", "授权", "v1",
              "deepseek-chat", "claude-sonnet-4-5",
              "L2", "auto_review", "auto_decide", "ai_judgement",
              "deepseek", "gpt-4", "api_key", "base_url")


@pytest.mark.parametrize("command,rule", [
    ("rm -rf build/", "delete"),
    ("npm run build", ""),
    ("git status", ""),
    ("ls -la", ""),
])
def test_the_notice_carries_no_mechanism_or_model_noise(command: str,
                                                        rule: str) -> None:
    """P2 本体：通知只留三行判断材料，机制与溯源全在审计里。"""
    notice = auto_allow_notice(tool_name="Bash", command=command,
                               reason="只读命令，无副作用", rule=rule)
    for phrase in _FORBIDDEN:
        assert phrase not in notice, (phrase, notice)
    # 词典自检：只查 BANNED_INTERNAL。BANNED_PHRASES 管的是审批主卡
    # （"放行""同意"那种诱导词），而这句话本身就叫「已自动放行」——
    # 同一条规则在主卡上用完整的 first_banned 自检，见下。
    assert _first_internal(notice) is None, (_first_internal(notice), notice)


def test_the_main_card_still_passes_the_full_banned_dictionary() -> None:
    """主卡不写「放行」这三个字，所以官方词典（含 BANNED_PHRASES）整个能过。

    通知用不了 ``first_banned``：它禁的"放行"正是通知的第一行。
    规则本身没变，只是两个出口各查各自那一半。
    """
    card = build_card_html(subject="claude-code", tool_summary="Bash: rm -rf build/",
                           session_id="800257f3", working_directory=WD,
                           command="rm -rf build/")
    assert first_banned(card) is None, first_banned(card)


def test_the_notice_has_no_way_to_receive_a_model_name() -> None:
    """结构性保证：``auto_allow_notice`` 压根没有模型参数可传。

    只靠"没传"是不够的——签名里没有这个参数，将来也没法顺手加一个。
    模型名只进 ``record.judge_model``（审计字段）。
    """
    parameters = inspect.signature(auto_allow_notice).parameters
    assert "model" not in parameters
    assert set(parameters) == {"tool_name", "command", "reason", "rule"}


def test_no_user_visible_string_mentions_internal_tokens() -> None:
    """P3：会上屏的那句话里不许有内部词（``L2`` / ``auto_review`` / ``auto_decide``）。

    按 AST 收**可能上屏的函数里的字面量**，而不是对整份源码 grep：
    注释、docstring、以及 ``logger.*`` 的审计行都允许出现 ``L2``——
    审计要精确到配置键名，用户屏幕上不行。"会上屏"= 函数名 ``build_*``
    （拼卡片/通知的纯函数）或函数体里出现了 ``push_message`` /
    ``build_result_html`` 这类出口调用。
    """
    sources = {
        "approval": inspect.getsource(approval),
        "__init__": inspect.getsource(module),
    }
    offenders: list[tuple[str, str, str]] = []
    for name, source in sources.items():
        for function, literal in _user_facing_literals(source):
            for token in ("L2", "auto_review", "auto_decide"):
                if token in literal:
                    offenders.append((name, function, literal[:40]))
    assert not offenders, offenders


# 出口：这些话一旦说出去，用户就直接看见。
_USER_FACING_SINKS = frozenset({
    "push_message", "build_result_html", "build_card_html", "create_card",
    "auto_allow_notice", "auto_allow_setting_line",
})
# 这些调用进的是审计与日志，不是屏幕。
_AUDIT_METHODS = frozenset({"info", "warning", "error", "debug", "exception"})


def _user_facing_literals(source: str) -> list[tuple[str, str]]:
    """``(函数名, 字面量)``：可能上屏的函数里的字符串字面量。

    排除 docstring 与 ``logger.*`` 参数——那两处该精确就精确。
    """
    tree = ast.parse(source)
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node

    def ancestors(node: ast.AST):
        current = parents.get(id(node))
        while current is not None:
            yield current
            current = parents.get(id(current))

    def is_docstring(node: ast.AST) -> bool:
        for parent in ancestors(node):
            body = getattr(parent, "body", None)
            if (isinstance(body, list) and body
                    and isinstance(body[0], ast.Expr) and body[0].value is node):
                return True
        return False

    def in_audit_call(node: ast.AST) -> bool:
        return any(
            isinstance(parent, ast.Call)
            and getattr(parent.func, "attr", getattr(parent.func, "id", ""))
            in _AUDIT_METHODS
            for parent in ancestors(node)
        )

    found: list[tuple[str, str]] = []
    functions = [node for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for function in functions:
        calls = {getattr(call.func, "attr", getattr(call.func, "id", ""))
                 for call in ast.walk(function)
                 if isinstance(call, ast.Call)}
        if not (function.name.startswith("build_") or (calls & _USER_FACING_SINKS)):
            continue
        for node in ast.walk(function):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if is_docstring(node) or in_audit_call(node):
                    continue
                found.append((function.name, node.value))
    return found


# ── 4. P3：设置页那一行，以及"没接审查器就没有自动放行" ────────────────────

def test_the_setting_line_states_how_to_turn_it_off() -> None:
    """「怎么关」只在设置页说一次：通知里每件事都提醒一遍，用户就学会无视。"""
    assert auto_allow_setting_line(enabled=True) == "自动放行：已开启（可关闭）"
    assert auto_allow_setting_line(enabled=False) == "自动放行：未开启"
    assert _first_internal(auto_allow_setting_line(enabled=True)) is None


def test_no_reviewer_means_no_auto_allow_to_talk_about() -> None:
    """没接审查器时根本没有自动放行，提示"可关闭"是让用户找一个不存在的开关。"""
    gateway = ApprovalGateway(_ListLogger(), port=_free_port(),
                              authorization_path=Path("no-such-authorization.json"))
    try:
        assert gateway.reviewer_connected() is False
    finally:
        gateway.stop()


# ── 辅助：从 test_b9_auto_review 照抄的装配件 ──────────────────────────────

class _FakeReviewer:
    """只实现 review()：测的是装配，不是 HTTP。"""

    def __init__(self, review) -> None:
        self._review = review
        self.enabled = True
        self.calls: list[dict] = []

    def review(self, **kwargs):
        self.calls.append(kwargs)
        return self._review


class _ListLogger:
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


def _gateway(reviewer, tmp_path: Path) -> ApprovalGateway:
    """带 review 签字范围的 gateway：B9 这条路要用户确认过 auto_review。"""
    gateway = ApprovalGateway(
        _ListLogger(), port=_free_port(),
        authorization_path=tmp_path / "authorization.json",
        reviewer=reviewer,
    )
    gateway.grant_authorization((AUTH_SCOPE_REVIEW,))
    return gateway
