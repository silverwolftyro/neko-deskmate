"""「这件事里都允许」/「始终允许」的规则表（0.6.26）。

## 这批测试的重点是**那道墙**

四条按钮里，「只这一次」什么都不记，「这件事里都允许」和「始终允许」
会记规则——而规则意味着**下次不问了**。所以这批测试的大头不是"记不记"，
是"什么不许记"：

1. **irreversible 类永不进表。** `~` / 盘根 / 系统目录 / 工作区外 /
   无界通配符 / `.git`——点十次「始终允许」也只是那一次放行，下一次照旧问。
   这是这个项目已成文的一条：「高风险永远呈现」，以及用户自己的
   「真正的严重警告需要有真实性」。
2. **键里不放原始命令。** 命令可能含密钥；而且每条都不一样，拿它当键
   等于规则永远命不中。
3. **认不出目标就不建规则。** 宁可多问一次，不可悄悄放行一次。

## 为什么窄是安全的

`rm -rf build/` 与 `rm -rf dist/` 是两条规则。偏窄，但窄的代价只是多弹
一次卡；宽的代价是悄悄放行危险操作。这个项目反复付过宽的学费。
"""

from __future__ import annotations

import pytest

from plugin.plugins.agent_deskmate.allowrules import (
    SCOPE_ALWAYS,
    SCOPE_SESSION,
    AllowRules,
    rule_key,
)

RECOVERABLE = "recoverable"
IRREVERSIBLE = "irreversible"


def _key(command, *, action_class=RECOVERABLE, rule="delete"):
    return rule_key(command, action_class=action_class, rule=rule)


# ── 1. 键怎么来 ────────────────────────────────────────────────────────

def test_same_shape_makes_the_same_key() -> None:
    assert _key("rm -rf build/") == _key("rm -rf build/")


def test_different_targets_make_different_keys() -> None:
    """`build/` 与 `dist/` 是两条规则——窄是故意的。"""
    assert _key("rm -rf build/") != _key("rm -rf dist/")


def test_the_key_holds_no_raw_command() -> None:
    """键里不许有原始命令：可能含密钥，而且每条都不一样，当键等于命不中。"""
    key = _key("rm -rf build/")
    assert "rm -rf" not in repr(key)
    assert "build/" in repr(key)          # 目标在，那是它该有的信息


def test_a_key_that_cannot_be_built_is_none() -> None:
    """认不出要动什么 → 建不出规则 → 调用方按"要问"处理。

    宁可多问一次，不可悄悄放行一次。
    """
    assert _key("git status", rule="") is None


# ── 2. 那道墙：irreversible 永不进表 ─────────────────────────────────

@pytest.mark.parametrize("command", [
    "rm -rf ~",
    "rm -rf $HOME",
    "rm -rf /",
    "rm -rf D:/",
    "rm -rf C:/Windows/System32",
    "rm -rf ../*",
    "rm -rf *",
    "rm -rf .git",
    "rm -rf .git/objects",
    "rm -rf foo/.git",
    "rm -rf ~/.ssh",
])
def test_irreversible_class_never_gets_a_key(command: str) -> None:
    """墙在 ``rule_key`` 里，不在调用方——放一处，漏一处也不会放危险动作进去。"""
    assert _key(command, action_class=IRREVERSIBLE) is None


def test_clicking_always_allow_ten_times_still_asks_next_time() -> None:
    """点十次「始终允许」也只是那一次放行。

    这条是端到端的那道墙：grant 十次，下一次照样命中不了。
    """
    rules = AllowRules()
    key = _key("rm -rf ~", action_class=IRREVERSIBLE)
    assert key is None
    for _ in range(10):
        rules.grant(key, SCOPE_ALWAYS, agent="a", session="s") \
            if key is not None else None
    assert rules.hit(None, agent="a", session="s") is None
    assert len(rules) == 0


# ── 3. 两个作用域 ──────────────────────────────────────────────────────

def test_always_scope_survives_a_different_session() -> None:
    rules = AllowRules()
    key = _key("rm -rf build/")
    rules.grant(key, SCOPE_ALWAYS, agent="claude-code", session="s1")
    assert rules.hit(key, agent="claude-code", session="s2") == SCOPE_ALWAYS


def test_session_scope_does_not_leak_to_another_session() -> None:
    rules = AllowRules()
    key = _key("rm -rf build/")
    rules.grant(key, SCOPE_SESSION, agent="claude-code", session="s1")
    assert rules.hit(key, agent="claude-code", session="s1") == SCOPE_SESSION
    assert rules.hit(key, agent="claude-code", session="s2") is None


def test_session_scope_does_not_leak_to_another_agent() -> None:
    """两个 Agent 可能撞同一个 session id（桶键必须带 agent 的理由同源）。"""
    rules = AllowRules()
    key = _key("rm -rf build/")
    rules.grant(key, SCOPE_SESSION, agent="claude-code", session="s1")
    assert rules.hit(key, agent="codex", session="s1") is None


def test_always_wins_when_both_match() -> None:
    """两者都中时按更宽的那个算——那才是用户当时点的那个。"""
    rules = AllowRules()
    key = _key("rm -rf build/")
    rules.grant(key, SCOPE_SESSION, agent="a", session="s")
    rules.grant(key, SCOPE_ALWAYS, agent="a", session="s")
    assert rules.hit(key, agent="a", session="s") == SCOPE_ALWAYS


def test_no_key_never_hits() -> None:
    rules = AllowRules()
    assert rules.hit(None, agent="a", session="s") is None


def test_an_unknown_scope_is_refused() -> None:
    rules = AllowRules()
    with pytest.raises(ValueError):
        rules.grant(_key("rm -rf build/"), "forever", agent="a", session="s")


def test_the_table_is_empty_on_a_fresh_instance() -> None:
    """重启即丢——"永远不再问"是安全决策，不该跨会话活着而没签字。

    第一版（对象）就是这条的性质说明：新实例 = 空表。
    """
    assert len(AllowRules()) == 0


def test_len_counts_both_scopes() -> None:
    rules = AllowRules()
    rules.grant(_key("rm -rf build/"), SCOPE_ALWAYS, agent="a", session="s")
    rules.grant(_key("rm -rf dist/"), SCOPE_SESSION, agent="a", session="s")
    assert len(rules) == 2


# ── 4. 网关侧：命中之后发生什么 ────────────────────────────────────────
#
# 上面测的是纯函数层（键与表）。这里测接进 ``ApprovalGateway`` 之后那条路，
# 盯的是一条不变式：**规则命中可以不弹卡，但绝不能不出声**。
# 看不见的放行等于后门——这是这个项目 0.6.x 早期就定下的规矩。

import threading                                              # noqa: E402
from types import SimpleNamespace                              # noqa: E402

from plugin.plugins.agent_deskmate.approval import (          # noqa: E402
    APPROVE_ALLOW,
    DECIDED_BY_EXPIRED,
    DECIDED_BY_RULE,
    ApprovalGateway,
)


def _record(command="rm -rf build/", *, action_class=RECOVERABLE,
            rule="delete", agent="claude-code", session="s1"):
    request = SimpleNamespace(
        request_id="r1", agent=agent, session_id=session,
        full_command=command, working_directory=r"D:\work\proj",
        tool_name="Bash", tool_summary=f"Bash: {command}",
    )
    return SimpleNamespace(
        request=request, decision=None, decided_by=None, auto=False,
        judge_reason="", judge_model="", severity="warn", rule=rule,
        action_class=action_class, event=__import__("threading").Event(),
    )


def _gateway():
    """只借方法、不建真服务——这条路不需要端口。

    ``_lock`` 与 ``_records`` 必须给：``_auto_allow_by_rule`` 与
    ``grant_rule`` 都在锁内改记录。少一个就是 AttributeError，
    而不是一个有意义的失败。
    """
    gateway = object.__new__(ApprovalGateway)
    gateway._allow_rules = AllowRules()
    gateway._logger = SimpleNamespace(info=lambda *a, **k: None)
    gateway._lock = threading.Lock()
    gateway._records = {}
    return gateway


def test_a_rule_hit_allows_without_a_card() -> None:
    gateway = _gateway()
    key = _key("rm -rf build/")
    gateway._allow_rules.grant(key, SCOPE_ALWAYS, agent="claude-code",
                               session="s1")
    record = _record()
    assert gateway._maybe_allow_by_rule(record) is True
    assert record.decision == APPROVE_ALLOW
    assert record.decided_by == DECIDED_BY_RULE
    # auto=True → 走 take_auto_allowed 那条通知路，不弹卡片
    assert record.auto is True


def test_a_rule_hit_leaves_a_reason_for_the_notice() -> None:
    """通知必须说清"凭你之前点的什么"——不然用户只看到"它自己跑了"。"""
    gateway = _gateway()
    gateway._allow_rules.grant(_key("rm -rf build/"), SCOPE_ALWAYS,
                               agent="claude-code", session="s1")
    record = _record()
    gateway._maybe_allow_by_rule(record)
    assert "始终允许" in record.judge_reason


def test_a_rule_hit_says_session_when_that_is_what_was_clicked() -> None:
    gateway = _gateway()
    gateway._allow_rules.grant(_key("rm -rf build/"), SCOPE_SESSION,
                               agent="claude-code", session="s1")
    record = _record()
    gateway._maybe_allow_by_rule(record)
    assert "这件事里都允许" in record.judge_reason


def test_a_rule_miss_changes_nothing() -> None:
    gateway = _gateway()
    record = _record()
    assert gateway._maybe_allow_by_rule(record) is False
    assert record.decided_by is None
    assert record.auto is False


def test_an_already_decided_record_is_not_touched() -> None:
    """L2 已经放行过的话，规则这条路不能再撞上去。"""
    gateway = _gateway()
    gateway._allow_rules.grant(_key("rm -rf build/"), SCOPE_ALWAYS,
                               agent="claude-code", session="s1")
    record = _record()
    record.decided_by = "ai"
    assert gateway._maybe_allow_by_rule(record) is False
    assert record.decided_by == "ai"


def test_grant_requires_the_request_to_be_resolved_first() -> None:
    """卡片还没决定就记规则 = 替用户决定。必须先 resolve。"""
    gateway = _gateway()
    assert gateway.grant_rule("r1", SCOPE_ALWAYS) == "unknown"
    assert len(gateway._allow_rules) == 0


def test_grant_refuses_an_expired_record() -> None:
    gateway = _gateway()
    record = _record()
    record.decided_by = DECIDED_BY_EXPIRED
    gateway._records = {"r1": record}
    assert gateway.grant_rule("r1", SCOPE_ALWAYS) == "expired"
    assert len(gateway._allow_rules) == 0


def test_grant_on_an_irreversible_action_allows_but_records_nothing() -> None:
    """点「始终允许」删 `~`：**这一次照旧放行，但什么都不记。**

    这是那道墙的端到端版本——纯函数层挡了建键，网关层挡的是
    "调了 grant 却静默记上了"。两边都要有人挡。
    """
    gateway = _gateway()
    record = _record("rm -rf ~", action_class=IRREVERSIBLE)
    record.decided_by = "user"
    gateway._records = {"r1": record}
    assert gateway.grant_rule("r1", SCOPE_ALWAYS) == "resolved"
    assert len(gateway._allow_rules) == 0, "不可恢复类被记进规则表了"


def test_grant_then_the_next_request_is_allowed() -> None:
    """完整一轮：点「始终允许」→ 记规则 → 下一条同形状的直接放行。"""
    gateway = _gateway()
    first = _record()
    first.decided_by = "user"
    gateway._records = {"r1": first}
    assert gateway.grant_rule("r1", SCOPE_ALWAYS) == "resolved"
    assert len(gateway._allow_rules) == 1

    second = _record()
    assert gateway._maybe_allow_by_rule(second) is True
    assert second.decided_by == DECIDED_BY_RULE


def test_an_unknown_scope_is_refused_by_grant() -> None:
    gateway = _gateway()
    record = _record()
    record.decided_by = "user"
    gateway._records = {"r1": record}
    assert gateway.grant_rule("r1", "forever") == "unknown"
    assert len(gateway._allow_rules) == 0
