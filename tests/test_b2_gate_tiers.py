"""B2：闸门按**删除目标**分档 + 白名单思维 + 未见默认待定。

老口径只有一条："命令里出现删除 token"就判 critical，其余一律 notice。
后果是删 `node_modules` 和删 `~` 顶同一根红条——用户见到红条就点确认，
闸门就是这么贬值的（真机原话：看多了等于没有）。

改成两步：**先解析目标路径集合，再按目标所在位置分档**。

| 档 | 判定 | severity | 红字 |
|---|---|---|---|
| recoverable | node_modules / .venv / dist / build / __pycache__ / 缓存 | notice | 不计数 |
| normal | 工作区内的其他路径 | warn | 计数 |
| extreme | 工作区外 / 家目录 / 盘根 / 系统目录 / 通配符 / 目标不可解析 | critical | **永不递减** |
| unknown | 解析不出目标、效果说不清 | warn | 计数 |

另外两条硬规则：
- **没见过的默认待定**：解析不出目标 → 至少告警档，不进低风险；
- **L2 只放行只读白名单内的命令**：白名单外一律不进自动。
"""

from __future__ import annotations

import pytest

from plugin.plugins.agent_deskmate.approval import (
    assess_risk,
    assess_tier,
    auto_decide_gate,
    warning_key,
    warning_mode_for_tier,
)
from plugin.plugins.agent_deskmate.commands import (
    TIER_EXTREME,
    TIER_NORMAL,
    TIER_RECOVERABLE,
    TIER_UNKNOWN,
    TIERS,
    delete_targets,
    is_recoverable_target,
    target_tier,
    target_unparseable,
    tier_for_command,
    translate,
)
from plugin.plugins.agent_deskmate.rules import l2_readonly_allowed

WD = r"D:\workspace\proj"


def _risk(command: str, cwd: str | None = WD) -> tuple[str, str, str]:
    return assess_risk("Bash", {"command": command}, cwd)


# ── 分档表 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("command,tier,severity", [
    # 可恢复：删了重建得回来
    ("rm -rf node_modules", TIER_RECOVERABLE, "notice"),
    ("rm -rf D:/p/node_modules", TIER_RECOVERABLE, "notice"),
    ("rm -rf .venv", TIER_RECOVERABLE, "notice"),
    ("rm -rf dist", TIER_RECOVERABLE, "notice"),
    ("rm -rf build", TIER_RECOVERABLE, "notice"),
    ("rm -rf __pycache__", TIER_RECOVERABLE, "notice"),
    ("rm -rf .pytest_cache", TIER_RECOVERABLE, "notice"),
    # 多个目标取**最坏**的一档：一个是可恢复、一个不是 → 按不是的那个算
    ("rm -rf node_modules src", TIER_NORMAL, "warn"),
    # 普通：工作区内的其他路径
    ("rm -rf D:/workspace/proj/src", TIER_NORMAL, "warn"),
    ("rm -rf src", TIER_NORMAL, "warn"),
    ("del x.txt", TIER_NORMAL, "warn"),
    # 极高危：工作区外 / 家目录 / 盘根 / 系统目录 / 不可解析
    ("rm -rf ~", TIER_EXTREME, "critical"),
    ("rm -rf $HOME/.cache", TIER_EXTREME, "critical"),
    ("rm -rf /", TIER_EXTREME, "critical"),
    ("rm -rf C:/", TIER_EXTREME, "critical"),
    ("rm -rf /etc", TIER_EXTREME, "critical"),
    (r"rm -rf C:\Windows\System32", TIER_EXTREME, "critical"),
    ("rm -rf D:/other/x.txt", TIER_EXTREME, "critical"),
    ("rm -rf $(cat pathlist)", TIER_EXTREME, "critical"),
    ("rm -rf *", TIER_EXTREME, "critical"),
    # 目标说不清 → 抬到极高危（删除类）
    ("git clean -fdx", TIER_EXTREME, "critical"),
])
def test_the_tier_table(command: str, tier: str, severity: str) -> None:
    """B2 表逐行落地。"""
    assert assess_tier("Bash", {"command": command}, WD) == tier
    assert _risk(command)[0] == severity


def test_multiple_targets_take_the_worst_tier() -> None:
    """一条命令删两个路径，危险的那个说了算。"""
    assert assess_tier("Bash", {"command": "rm -rf node_modules D:/other/x.txt"}, WD) == (
        TIER_EXTREME)
    assert _risk("rm -rf node_modules D:/other/x.txt")[0] == "critical"


def test_recoverable_and_extreme_are_not_the_same_thing() -> None:
    """删 node_modules 与删 ~ 不能再顶同一根红条——这是 B2 的出发点。"""
    assert _risk("rm -rf node_modules")[0] != _risk("rm -rf ~")[0]
    assert _risk("rm -rf node_modules")[2] != _risk("rm -rf ~")[2]


def test_unknown_tier_is_the_default_for_unparsable_effects() -> None:
    """没见过的默认待定：至少告警档，绝不进低风险。"""
    assert target_tier((), working_directory=WD) == TIER_UNKNOWN
    assert target_tier(("if", "then"), working_directory=WD) == TIER_UNKNOWN
    # 词典认得的不可恢复动作（没有路径目标）→ 待定 → warn，不是 notice
    assert assess_tier("Bash", {"command": "git reset --hard"}, WD) == TIER_UNKNOWN
    assert _risk("git reset --hard")[0] == "warn"


def test_unparseable_targets_are_never_below_warn() -> None:
    """任务书 B2 测试第三条：目标解析失败 → 不低于 warn。"""
    for command in ("rm -rf $(cat pathlist)", "rm -rf `cat pathlist`",
                    "rm -rf ${DIR}", "rm -rf *.txt"):
        assert target_unparseable(command) is True
        assert _risk(command)[0] in ("warn", "critical")


def test_missing_working_directory_means_worst_case() -> None:
    """不知道工作目录 → 说不清在不在范围内 → 按最坏的猜。"""
    assert _risk("rm -rf x", None)[0] == "critical"
    assert assess_tier("Bash", {"command": "rm -rf x"}, None) == TIER_EXTREME


# ── 词典与闸门共用一套判据 ────────────────────────────────────────────────

def test_gate_and_dictionary_agree_on_git_reset_hard() -> None:
    """B2 现状第一条：同一条 `git reset --hard`，词典认、闸门不认——已修。

    闸门现在也读词典：词典认得且标了"不可恢复"的动作，闸门给 warn 而不是
    默认的 notice。两侧共用 ``commands.tier_for_command`` 解析目标。
    """
    facts = translate("git reset --hard")
    assert facts.known and facts.irreversible
    severity, rule, _ = _risk("git reset --hard")
    assert severity == "warn"
    assert rule == "discard"


def test_dictionary_readonly_commands_stay_low_risk() -> None:
    """反过来也成立：词典不认的只读命令仍然是 notice。"""
    assert _risk("git status") == ("notice", "", "recoverable")
    assert _risk("ls -la") == ("notice", "", "recoverable")
    assert _risk('echo "rm -rf x"') == ("notice", "", "recoverable")


def test_targets_keep_their_original_case() -> None:
    """目标路径要显示给用户看，不能被归一化搅乱。"""
    assert delete_targets("rm -rf D:/Other/X.TXT") == ("D:/Other/X.TXT",)


@pytest.mark.parametrize("target", [
    "node_modules", "D:/p/node_modules", ".venv", "dist", "build",
    "__pycache__", ".pytest_cache", ".ruff_cache", ".tox", ".cache",
    "target", ".next",
])
def test_recoverable_markers(target: str) -> None:
    assert is_recoverable_target(target) is True


@pytest.mark.parametrize("target", ["src", "D:/p/src", "main.py", "~", "/etc"])
def test_non_recoverable_targets(target: str) -> None:
    assert is_recoverable_target(target) is False


def test_subcommands_are_not_targets() -> None:
    """`git clean` 的 `clean` 是子命令不是路径——不跳掉它会把
    "整个仓库的未追踪文件"误判成"工作区内的一个目录"（降档）。"""
    assert delete_targets("git clean -fdx") == ()
    assert delete_targets("git reset --hard") == ()
    assert delete_targets("npm install") == ()
    assert delete_targets("git rm --cached secret.txt") == ("secret.txt",)


def test_navigation_commands_do_not_contribute_targets() -> None:
    """`cd D:/w && rm -rf x`：`D:/w` 不是要删的路径。"""
    assert delete_targets("cd D:/w && rm -rf x") == ("x",)


# ── 红字计数：按目标档位，极高危永不递减 ─────────────────────────────────

def test_warning_keys_are_one_per_tier() -> None:
    """计数键按**目标档位**开，不按 action_class。

    老口径按 action_class：可恢复但极高危的请求会和所有低风险请求共用
    `recoverable` 这个键，点几次低风险之后那条极高危的红字就被递减掉了。
    """
    assert len({warning_key(t) for t in TIERS}) == len(TIERS)
    assert warning_key(TIER_EXTREME) != warning_key(TIER_NORMAL)
    assert warning_key(TIER_NORMAL) != warning_key(TIER_RECOVERABLE)


def test_extreme_tier_never_decrements() -> None:
    """任务书 B2 测试第二条：连点 3 次删 node_modules 之后，
    第 4 次删 `~` 仍然整行红字。"""
    # 用户把递减配置开到最狠（一次红字都不要）也不影响极高危
    assert warning_mode_for_tier(TIER_EXTREME, "never") == "always"
    assert warning_mode_for_tier(TIER_EXTREME, "first_n") == "always"
    # 其余档位照用户配置走
    assert warning_mode_for_tier(TIER_NORMAL, "never") == "never"
    assert warning_mode_for_tier(TIER_RECOVERABLE, "first_n") == "first_n"
    assert warning_mode_for_tier("", "first_n") == "first_n"


def test_recoverable_and_normal_count_separately() -> None:
    """删 3 次 node_modules 不会把一个 `rm -rf src` 的红字递减掉。"""
    assert warning_key(TIER_RECOVERABLE) != warning_key(TIER_NORMAL)


# ── L2 只读白名单 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "ls -la", "git status", "git log --oneline", "cat x.txt", "pwd",
    "grep -rn foo .", "npm ls", "pip list",
])
def test_readonly_commands_are_whitelisted(command: str) -> None:
    assert l2_readonly_allowed(command) is True


@pytest.mark.parametrize("command", [
    "rm -rf build/",           # 可恢复档 + 阈值之内，也不自动放行
    "rm -rf ~",
    "rm -rf node_modules",
    "ls && rm -rf x",          # 有一段不在，整条不批
    "find . -delete",          # find 能删，刻意不收
    "VAR=x",                   # 看不懂
    "",                        # 空
    "git commit -m x",         # git 只收明确的只读子命令
    "curl https://x",          # 发起网络请求，不收
])
def test_non_readonly_commands_are_not_whitelisted(command: str) -> None:
    assert l2_readonly_allowed(command) is False


def test_recoverable_delete_is_not_auto_allowed_even_with_the_gate_wide_open() -> None:
    """B2 补充规则：白名单外一律不进自动。

    `rm -rf build/` 按目标分档落在可恢复档、severity=notice、阈值之内——
    没有只读白名单这道墙，它就会被 L2 自动放行。这是 B2 自己引入的风险，
    必须由 B2 自己的白名单规则堵住。
    """
    kwargs = dict(ai_judgement=True, has_key=True,
                  auto_decide="low_risk_only", max_severity="notice",
                  authorized_scopes=("ai_judgement", "auto_decide"),
                  action_class="recoverable")
    eligible, why = auto_decide_gate(severity="notice",
                                     readonly=l2_readonly_allowed("rm -rf build/"),
                                     **kwargs)
    assert eligible is False
    assert "只读白名单" in why

    eligible, why = auto_decide_gate(severity="notice",
                                     readonly=l2_readonly_allowed("ls -la"),
                                     **kwargs)
    assert eligible is True


def test_gate_refuses_when_readonly_is_not_supplied() -> None:
    """``readonly`` 默认不批：调用点忘了算这个事实，L2 静默不生效。"""
    eligible, why = auto_decide_gate(severity="notice", ai_judgement=True,
                                     has_key=True, auto_decide="low_risk_only",
                                     max_severity="notice",
                                     authorized_scopes=("ai_judgement", "auto_decide"),
                                     action_class="recoverable")
    assert eligible is False
    assert "只读白名单" in why


# ── 档位与判定的一致性 ───────────────────────────────────────────────────

def test_extreme_always_means_irreversible() -> None:
    for command in ("rm -rf ~", "rm -rf /etc", "rm -rf D:/other/x.txt",
                    "rm -rf *", "git clean -fdx"):
        assert _risk(command)[2] == "irreversible"
        assert _risk(command)[0] == "critical"


def test_recoverable_and_normal_are_not_irreversible() -> None:
    for command in ("rm -rf node_modules", "rm -rf src", "rm -rf build"):
        assert _risk(command)[2] == "recoverable"
        assert _risk(command)[0] in ("notice", "warn")


def test_tier_for_command_is_the_single_entry_point() -> None:
    """闸门、播报、词典都从这一个函数取档位（禁止两套判据）。"""
    assert tier_for_command("rm -rf ~", WD) == TIER_EXTREME
    assert tier_for_command("rm -rf node_modules", WD) == TIER_RECOVERABLE
    assert tier_for_command("rm -rf src", WD) == TIER_NORMAL
