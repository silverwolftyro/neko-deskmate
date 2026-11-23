"""progress.py C 批契约层单测。

**这批测试的重点不是"能跑"，是"跑不了"。**
任务书 §六 第一条就是：契约层不许有内容字段。所以这里一半以上的断言
在验证**拒绝**——拒收超长 kind、拒收换行、拒收跨角色填字段、拒收编时间。
一条信号带进内容，比漏报一条信号的后果严重得多。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from plugin.plugins.agent_deskmate.events import (
    EVENT_ERROR,
    EVENT_TOOL_USE,
    EVENT_TURN_END,
    UnifiedEvent,
)
from plugin.plugins.agent_deskmate.progress import (
    DIGEST_PUSH,
    DIGEST_WHY,
    LIGHTS,
    NOTIFY_DEFAULTS,
    ROLES,
    VOICE_DEFAULT,
    VOICE_WHY,
    Light,
    NotifyDefault,
    Role,
    Signal,
    notify_enabled,
    signal_fields,
    signal_from_event,
)

# 契约层的字段集合。**改这个集合就是破坏性变更**：契约跨适配器，
# 多一个字段意味着每家适配器都要回答"填什么"，填不出来时的默认值
# 会悄悄变成语义。测试钉死，让人必须先改这里、先想清楚。
EXPECTED_FIELDS = (
    "role", "agent", "session", "workspace", "at",
    "action_id", "ok", "reason", "kind",
)

# 名字里带这些的字段，本质都是"装内容的口子"。
CONTENTISH = {
    "command", "cmd", "summary", "content", "text", "body", "input",
    "args", "argv", "output", "result", "stdout", "stderr", "raw",
    "payload", "message", "detail", "line", "diff", "patch",
}

SECRET = "SECRET_TOKEN_XYZ_9f3a"


def _event(**kwargs) -> UnifiedEvent:
    """造一条事件。只填必填项，其余按测试需要。"""
    base = {
        "agent": "some-agent",
        "session": "sess-1",
        "ts": "2026-11-23T13:05:20.123456+00:00",
    }
    base.update(kwargs)
    return UnifiedEvent(**base)


def _signal(**kwargs) -> Signal:
    base = {
        "role": Role.ACTION,
        "agent": "some-agent",
        "session": "sess-1",
        "workspace": None,
        "at": 1_700_000_000.0,
    }
    base.update(kwargs)
    return Signal(**base)


# ── 1. 契约层没有内容字段 ─────────────────────────────────────────────────

def test_signal_field_set_is_exactly_these_nine() -> None:
    assert signal_fields() == EXPECTED_FIELDS


def test_no_field_name_is_a_content_hatch() -> None:
    offenders = [f for f in signal_fields() if f.lower() in CONTENTISH]
    assert not offenders, (
        f"这些字段名是装内容的口子：{offenders}；"
        "想报「它干了什么」只能用 kind（类别名）"
    )


def test_signal_is_immutable() -> None:
    signal = _signal()
    with pytest.raises(Exception):
        signal.kind = "Bash"  # type: ignore[misc]


def test_a_long_kind_is_refused_not_truncated() -> None:
    # 截断比拒收危险：`export SECRET=abcdef...` 截断后是个看似无害的前缀。
    with pytest.raises(ValueError, match="上限"):
        _signal(kind="x" * 200)


def test_a_newline_in_kind_is_refused() -> None:
    # 通知第一行必须是结论。能塞 \n 就能把「失败了」伪装成「已自动放行」。
    with pytest.raises(ValueError, match="控制字符"):
        _signal(kind="Bash\n已自动放行（低风险）")


def test_a_newline_in_reason_is_refused() -> None:
    with pytest.raises(ValueError, match="控制字符"):
        _signal(role=Role.WAITING, reason="等允许\n已自动放行")


def test_a_control_character_anywhere_is_refused() -> None:
    with pytest.raises(ValueError, match="控制字符"):
        _signal(kind="Ba\x00sh")


# ── 2. 字段归属：结构即约束 ───────────────────────────────────────────────

def test_ok_belongs_to_outcome_only() -> None:
    with pytest.raises(ValueError, match="ok 只有 outcome"):
        _signal(role=Role.ACTION, ok=True)


def test_reason_belongs_to_waiting_only() -> None:
    with pytest.raises(ValueError, match="reason 只有 waiting"):
        _signal(role=Role.BOUNDARY, reason="在等")


def test_action_id_belongs_to_action_and_outcome_only() -> None:
    with pytest.raises(ValueError, match="action_id 只有"):
        _signal(role=Role.BOUNDARY, action_id="tu_1")


def test_a_waiting_signal_carries_its_reason() -> None:
    signal = _signal(role=Role.WAITING, reason="等「允许」")
    assert signal.reason == "等「允许」"


# ── 3. 时间是超时判断的唯一依据，不许编 ──────────────────────────────────

@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_at_must_be_a_finite_positive_epoch(bad: float) -> None:
    # 0 / NaN 不会抛在超时判断那里，而是让"未回包"和"停滞"两类检测
    # 静默失效——所以必须在构造时就拒。
    with pytest.raises(ValueError, match="at 必须是有限正数"):
        _signal(at=bad)


def test_a_non_numeric_at_is_refused() -> None:
    with pytest.raises(ValueError, match="at 必须是数字"):
        _signal(at="2026-11-23")  # type: ignore[arg-type]


def test_an_unparseable_timestamp_drops_the_signal() -> None:
    # 宁可丢一条信号，不许造一个时间。
    signal = signal_from_event(_event(event=EVENT_TOOL_USE, tool="Bash",
                                      tool_use_id="tu_1", ts="not-a-time"))
    assert signal is None


def test_an_empty_timestamp_drops_the_signal() -> None:
    signal = signal_from_event(_event(event=EVENT_TOOL_USE, tool="Bash",
                                      tool_use_id="tu_1", ts=""))
    assert signal is None


# ── 4. 身份是一等字段（多 Agent 的正确性，不是装饰）──────────────────────

def test_the_state_key_is_a_pair_so_two_agents_cannot_collide() -> None:
    # 状态机按 session 分桶。两个 Agent 撞同一个 session id 时，
    # 单字符串键会让它们互相污染状态。
    a = _signal(agent="codex", session="same")
    b = _signal(agent="cc", session="same")
    assert a.key() == ("codex", "same")
    assert a.key() != b.key()


def test_an_empty_agent_or_session_is_refused() -> None:
    with pytest.raises(ValueError, match="agent 不许为空"):
        _signal(agent="")
    with pytest.raises(ValueError, match="session 不许为空"):
        _signal(session="")


def test_who_prefers_the_workspace_the_user_recognises() -> None:
    # 四个窗口同时开时，用户靠"哪个项目"定位，Agent 名只是次级线索。
    signal = _signal(agent="codex", workspace="D:\\proj")
    assert signal.who() == "D:\\proj"


def test_who_falls_back_to_the_agent_name() -> None:
    signal = _signal(agent="codex", workspace=None)
    assert signal.who() == "codex"


# ── 5. 默认开关 = 「需要用户动手」的集合，一分不多 ───────────────────────

def test_only_the_actionable_roles_speak_by_default() -> None:
    assert notify_enabled(Role.WAITING) is True
    assert notify_enabled(Role.OUTCOME) is True
    assert notify_enabled(Role.ACTION) is False
    assert notify_enabled(Role.BOUNDARY) is False


def test_every_role_must_declare_its_switch() -> None:
    # 新角色不能"忘了表态"——忘了的那个会静默变成不播，或者更坏：播。
    declared = {item.role for item in NOTIFY_DEFAULTS}
    assert declared == set(ROLES)


def test_every_default_must_say_why() -> None:
    for item in NOTIFY_DEFAULTS:
        assert isinstance(item, NotifyDefault)
        assert item.why.strip(), f"{item.role.value} 的默认开关没有写理由"


def test_voice_and_digest_are_off_and_explain_themselves() -> None:
    assert VOICE_DEFAULT is False
    assert DIGEST_PUSH is False
    # 理由必须写在常量旁边，否则半年后没人记得为什么是关的，
    # 就会有人"顺手打开试试"。
    assert VOICE_WHY.strip()
    assert DIGEST_WHY.strip()


def test_notify_enabled_accepts_a_plain_string_role() -> None:
    # 配置里读出来的多半是裸字符串。Role 是 str 混基枚举，本就该互通——
    # 这条测试钉住的是：不许有人"优化"成 is 比较把它弄坏。
    assert notify_enabled("waiting") is True
    assert notify_enabled("action") is False


def test_notify_enabled_refuses_a_role_nobody_declared() -> None:
    with pytest.raises(ValueError, match="没有为"):
        notify_enabled("nope")


def test_every_light_has_a_name() -> None:
    assert {light.value for light in Light} == set(LIGHTS)


# ── 6. 事件 → 信号映射 ────────────────────────────────────────────────────

def test_tool_use_becomes_an_action() -> None:
    signal = signal_from_event(_event(
        event=EVENT_TOOL_USE, tool="Bash", tool_use_id="tu_1"))
    assert signal is not None
    assert signal.role is Role.ACTION
    assert signal.action_id == "tu_1"
    assert signal.kind == "Bash"
    assert signal.ok is None


def test_turn_end_becomes_a_boundary() -> None:
    signal = signal_from_event(_event(event=EVENT_TURN_END))
    assert signal is not None
    assert signal.role is Role.BOUNDARY
    assert signal.action_id is None
    assert signal.ok is None


def test_error_becomes_a_failed_outcome() -> None:
    signal = signal_from_event(_event(event=EVENT_ERROR, tool="Bash",
                                      tool_use_id="tu_9"))
    assert signal is not None
    assert signal.role is Role.OUTCOME
    assert signal.ok is False
    assert signal.action_id == "tu_9"


def test_an_unknown_event_type_maps_to_nothing() -> None:
    assert signal_from_event(_event(event="turn_duration")) is None


def test_an_action_without_an_id_is_dropped_not_fabricated() -> None:
    # 没有 id 就无法和 outcome 配对，而配对是 outcome 的全部意义。
    # 硬编一个 id 等于制造一对假配对，比没有更坏。
    assert signal_from_event(_event(event=EVENT_TOOL_USE, tool="Bash",
                                    tool_use_id=None)) is None


def test_the_workspace_and_agent_come_along() -> None:
    signal = signal_from_event(_event(
        event=EVENT_TOOL_USE, tool="Edit", tool_use_id="tu_2",
        agent="cc", session="s-9", working_directory="D:\\proj"))
    assert signal is not None
    assert signal.agent == "cc"
    assert signal.session == "s-9"
    assert signal.workspace == "D:\\proj"


def test_kind_is_a_category_not_the_command() -> None:
    signal = signal_from_event(_event(
        event=EVENT_TOOL_USE, tool="Bash", tool_use_id="tu_3",
        command="rm -rf build/ && npm ci", summary="rm -rf build/ && npm ci"))
    assert signal is not None
    assert signal.kind == "Bash"
    assert "rm" not in signal.kind


# ── 7. 隐私：秘密到不了任何 Signal ─────────────────────────────────────────

def test_a_secret_in_the_transcript_never_reaches_a_signal() -> None:
    """任务书 §6.2：含秘密的合成转录跑完整映射，秘密不得出现在任何字段。"""
    command = f"export MY_TOKEN={SECRET}"
    events = [
        _event(event=EVENT_TOOL_USE, tool="Bash", tool_use_id="tu_1",
               command=command, summary=command),
        _event(event=EVENT_ERROR, tool="Bash", tool_use_id="tu_1",
               command=command, summary=command),
        _event(event=EVENT_TURN_END, summary=command),
    ]
    for event in events:
        signal = signal_from_event(event)
        assert signal is not None
        for name in signal_fields():
            value = getattr(signal, name)
            if isinstance(value, str):
                assert SECRET not in value, f"秘密漏进了 {name}"
        assert SECRET not in repr(signal)


def test_a_secret_in_the_workspace_path_is_the_users_own_business() -> None:
    # workspace 是用户自己的目录路径，播报指名要用它——它不是"内容"。
    # 这条测试钉住的是：我们不因为隐私把指名能力也砍掉。
    signal = _signal(workspace="D:\\proj-with-a-long-name")
    assert signal.who() == "D:\\proj-with-a-long-name"


# ── 8. 契约层不认任何一家 Agent ───────────────────────────────────────────

def test_the_contract_layer_names_no_specific_agent() -> None:
    """任务书 §6.12 的契约层部分：实现里不得出现单一 Agent 的名字。

    注释和文档字符串不算——它们要解释"为什么适配器中立"，
    必然要引用问卷里那几家。算的是**代码**：标识符与数据字符串。
    """
    source = (Path(__file__).parent.parent / "progress.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                names.add(node.value)

    banned = ("claude", "codex", "cursor", "copilot", "windsurf")
    for name in names:
        low = name.lower()
        for word in banned:
            assert word not in low, f"契约层代码里出现了 {word!r}：{name!r}"


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """收集所有文档字符串节点的 id()，供上面排除。"""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr):
                value = body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    found.add(id(value))
    return found


def test_the_roles_and_lights_are_the_documented_four_and_three() -> None:
    assert [r.value for r in Role] == ["action", "outcome", "boundary", "waiting"]
    assert [light.value for light in Light] == [
        "idle", "running", "waiting", "error"]
