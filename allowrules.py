"""「这件事里都允许」/「始终允许」的规则表（0.6.26）。

## 它是什么

用户在 AgentHUD 任务卡上点了这四个按钮之一，插件据此决定"下次还要不要问"：

| 按钮 | 效果 |
|---|---|
| 只这一次 | 放行这一次，什么都不记 |
| 这件事里都允许 | 放行，同 `(agent, session)` 下同形状的请求不再问 |
| 始终允许 | 放行，同形状的请求不再问（跨会话） |

**表在内存里，不落盘。** 重启即丢——"永远不再问"是一个安全决策，
让它跨会话活得走用户签字那套三 scope 授权，而这个按钮没有签字。
重启后重新问一遍，那是安全的失败方向。

## 三条硬规矩

1. **irreversible 类永不进表。** `~` / 盘根 / 系统目录 / 工作区外 /
   无界通配符 / `.git`（见 ``commands.destroys_irreversible_state`` 与
   ``_class_for_tier``）——点十次「始终允许」也只是那一次放行，下一次照旧问。
   这是这个项目已成文的一条：「高风险永远呈现」。
2. **键里不放原始命令。** 命令可能含密钥；而且每条都不一样，拿它当键
   等于规则永远命不中。用 ``translate()`` 出来的 **action + 主目标**。
3. **认不出目标就不建规则。** 宁可多问一次，不可悄悄放行一次。

## 为什么窄是安全的

`rm -rf build/` 与 `rm -rf dist/` 是两条规则，`rm -rf build/` 重复出现
才命中。偏窄，但窄的代价只是多弹一次卡；宽的代价是悄悄放行危险操作。
这个项目反复付过宽的学费，所以默认站窄那边。
"""

from __future__ import annotations

from dataclasses import dataclass

from .commands import ACTION_CLASS_IRREVERSIBLE, translate

#: 两个作用域。值与 AgentHUD 卡片上传回来的 ``scope`` 一一对应。
SCOPE_SESSION = "session"    # 这件事里都允许
SCOPE_ALWAYS = "always"      # 始终允许
SCOPES = (SCOPE_SESSION, SCOPE_ALWAYS)

#: 卡片按钮的 id → 传给 ``approval_decide`` 的 scope。
#: 「只这一次」不在这儿：它什么都不记，走默认路径。
BUTTON_SCOPES = {
    "allow_session": SCOPE_SESSION,
    "allow_always": SCOPE_ALWAYS,
}


@dataclass(frozen=True)
class AllowRule:
    """一条规则的键。**不含原始命令**（见模块 docstring 第 2 条）。"""

    action: str
    target: str
    rule: str
    action_class: str

    def describe(self) -> str:
        """给人看的那一句。进通知与审计，不进键以外的任何地方。"""
        return f"{self.action}：{self.target}" if self.target else self.action


def rule_key(command: str, *, action_class: str,
             rule: str) -> AllowRule | None:
    """命令 → 规则键。**建不出规则就返回 None**（调用方按"要问"处理）。

    ``action_class`` 是 irrecoverable 的一律返回 None——那条墙在这儿，
    不在调用方：放在一处，漏一处也不会把危险动作放进去。
    """
    if action_class == ACTION_CLASS_IRREVERSIBLE:
        return None
    # ``translate`` 返回**单个** CommandFacts（不是列表）；认不出时
    # ``known`` 为 False，那时不给目标——建不出规则的请求按"要问"处理。
    facts = translate(command)
    if not facts.known:
        return None
    target = facts.targets[0] if facts.targets else ""
    if not target:
        # 认不出要动什么：这种请求本来就不该被自动放行。
        return None
    return AllowRule(action=facts.action, target=target, rule=rule,
                     action_class=action_class)


class AllowRules:
    """规则表。**线程安全**（审批服务每个 hook 请求一个线程）。"""

    def __init__(self) -> None:
        self._always: set[AllowRule] = set()
        self._session: dict[tuple[str, str, AllowRule], None] = {}

    def grant(self, key: AllowRule, scope: str, *, agent: str,
              session: str) -> None:
        """记一条规则。``scope`` 只能是 ``SCOPES`` 里的值。"""
        if scope not in SCOPES:
            raise ValueError(f"未知作用域 {scope!r}")
        if scope == SCOPE_ALWAYS:
            self._always.add(key)
            return
        self._session[(agent or "", session or "", key)] = None

    def hit(self, key: AllowRule | None, *, agent: str,
            session: str) -> str | None:
        """命中返回作用域，没命中返回 None。

        **先查 always 再查 session**：两者都中时按更宽的那个算，
        通知里也说"始终允许"——那才是用户当时点的那个。
        """
        if key is None:
            return None
        if key in self._always:
            return SCOPE_ALWAYS
        if (agent or "", session or "", key) in self._session:
            return SCOPE_SESSION
        return None

    def __len__(self) -> int:
        return len(self._always) + len(self._session)
