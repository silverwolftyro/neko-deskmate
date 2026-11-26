"""L0 作用域判定：通配符不再毒化整条命令，``..`` 逃逸不再被放过。

2026-11-26 真机实测的根因修复。一次量了 35 个转录 / 3265 次 Bash /
120 条删除命中，发现两个方向的错，都出在"目标"这一步：

| 症状 | 命令 | 改前 | 改后 |
|---|---|---|---|
| 误报 | ``rm -rf build --exclude="*.log"`` | critical | notice |
| 误报 | ``rm -rf dist && echo "*"`` | critical | notice |
| 误报 | ``rm -rf build/*`` | critical | notice |
| 漏判 | ``rm -rf ../../..`` | warn | critical |
| 漏判 | ``rm -rf ../etc`` | warn | critical |

**误报那三条是同一个 bug**：``target_unparseable`` 用一条正则搜**整条命令串**，
所以 ``*`` 出现在开关值里、出现在另一条 ``&&`` 段里、出现在有字面前缀的目标里，
全都算"目标被藏起来了"。实测它把 47 条抬到极高危，其中 24 条只由通配符触发。

**漏判那两条是另一个 bug**：``_tier_of_one`` 里有一句
``if not _is_absolute(target): return TIER_NORMAL``，本意是"相对路径相对于
会话工作目录，算在内"——但 ``../..`` 同样是相对路径。一句话同时放过
``src``（该放过）和 ``../../..``（不该放过），因为根本没去解算。

两个 bug 合起来的结果不是"红字不准"，而是**一个字符废掉三条路**：
档位被顶成 extreme 之后，播报顶红字、``auto_decide_gate`` 第二道墙挡住、
``auto_review_gate`` 第三道墙挡住——DSH 那套"只让低风险通过"的自动审查
**连问都不问**。所以修档位不是在改文案，是在把模型的眼睛还给它。

铁律没放：``rm -rf *`` / ``$HOME`` / ``$(cat list)`` / ``./*`` / ``../*``
照旧极高危。放过的只是"作用域被字面目录圈住"的通配。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugin.plugins.agent_deskmate.approval import (
    assess_risk,
    assess_tier,
    auto_decide_gate,
    auto_review_gate,
    local_facts_for,
)
from plugin.plugins.agent_deskmate.commands import (
    TIER_EXTREME,
    TIER_NORMAL,
    TIER_RECOVERABLE,
    TIER_SCOPE_UNKNOWN,
    delete_targets,
    path_is_under,
    target_unparseable,
    tier_for_command,
)
from plugin.plugins.agent_deskmate.rules import evaluate_delete

WD = r"D:\workspace\proj"


def _tier(command: str, cwd: str | None = WD) -> str:
    return tier_for_command(command, cwd)


def _source(cwd: str | None) -> SimpleNamespace:
    """搭一个 ``evaluate_delete`` 认的 source：它只读工作目录 + 归因字段。"""
    return SimpleNamespace(requester="", requester_evidence="", executor="",
                           working_directory=cwd or "", session="s", tool="Bash")


def _severity(command: str, cwd: str | None = WD) -> str:
    hit = evaluate_delete(command, source=_source(cwd))
    assert hit is not None, f"{command!r} 没命中删除规则，测试写错了"
    return hit.severity


def _record(command: str, cwd: str = WD) -> SimpleNamespace:
    """搭一个 ``local_facts_for`` 认的 record（它读 request.full_command）。"""
    return SimpleNamespace(request=SimpleNamespace(
        full_command=command, working_directory=cwd))


# ── 误报：通配符不再毒化整条命令 ────────────────────────────────────────


@pytest.mark.parametrize("command", [
    "rm -rf build/*",
    "rm -rf build/**",
    "rm -rf D:/p/dist/*",
    "rm -rf node_modules/.cache/*",
])
def test_glob_with_a_literal_directory_prefix_is_bounded(command: str) -> None:
    """通配符前面有字面目录 → 作用域被那个目录圈住，按它判档。

    ``build`` 本来就在可恢复词表上：``rm -rf build/*`` 与 ``rm -rf build``
    删的是同一批东西，没有理由一个 notice 一个 critical。
    """
    assert target_unparseable(command) is False
    assert _tier(command) == TIER_RECOVERABLE
    assert _severity(command) == "notice"


def test_glob_inside_a_flag_value_does_not_poison_the_target() -> None:
    """``--exclude="*.log"`` 里的 ``*`` 是开关值，不是目标。"""
    command = 'rm -rf build --exclude="*.log"'
    assert delete_targets(command) == ("build",)
    assert target_unparseable(command) is False
    assert _tier(command) == TIER_RECOVERABLE
    assert _severity(command) == "notice"


def test_glob_in_another_segment_is_not_a_delete_target() -> None:
    """``echo "*"`` 里的 ``*`` 一个文件都不会删，不许算进删除目标。

    改前 ``delete_targets`` 对**每一段**都取目标，于是这条的目标是
    ``('dist', '*')``——那个 ``*`` 把整条顶成【严重】。
    """
    command = 'rm -rf dist && echo "*"'
    assert delete_targets(command) == ("dist",)
    assert _tier(command) == TIER_RECOVERABLE


def test_date_format_specifiers_still_do_not_escalate() -> None:
    """``%`` 那条（同一天第四条误报）不许回归。"""
    assert target_unparseable("date +%H:%M:%S") is False
    assert target_unparseable("rm -f x && date +%H:%M:%S") is False


# ── 铁律：这些照旧极高危，一个字不放 ────────────────────────────────────


@pytest.mark.parametrize("command", [
    "rm -rf *",             # 光板通配：作用域说不清
    "rm -rf *.log",         # 通配符在文件名里，前面没有目录
    "rm -rf a?b",
    "rm -rf f[0-9].txt",
    "rm -rf ./*",           # 与 rm -rf * 等价
    "rm -rf ../*",          # 与 rm -rf .. 等价
    "rm -rf $HOME",         # 变量替换
    "rm -rf $DIR",
    "rm -rf `cat list`",    # 命令替换
    "rm -rf $(cat list)",
    "rm -rf ${f%%.txt}",
])
def test_these_still_hide_their_scope(command: str) -> None:
    """**这一条是这次改动的安全边界**：放过的只是"有字面前缀"的通配。

    光板通配（``*`` / ``*.log`` / ``a?b`` / ``f[0-9].txt``）前面没有目录，
    ``.`` / ``..`` 前缀的通配语义上等于不带前缀——四种全都按最坏的猜。
    """
    assert target_unparseable(command) is True
    assert _tier(command) == TIER_EXTREME
    assert _severity(command) == "critical"


def test_dot_slash_star_is_not_a_loophole() -> None:
    """``./*`` 不许被当成"有字面前缀"——``.`` 不是目录。"""
    assert target_unparseable("rm -rf ./*") is True
    assert target_unparseable("rm -rf ./build/*") is False


# ── 漏判：``..`` 逃逸 ───────────────────────────────────────────────────


@pytest.mark.parametrize("command", [
    "rm -rf ../../..",
    "rm -rf ../etc",
    "rm -rf ..",
    "rm -rf ../",
])
def test_parent_traversal_escapes_the_working_directory(command: str) -> None:
    """相对路径也要解算：``../..`` 相对于工作目录的解算结果在**工作区外**。

    改前 ``_tier_of_one`` 用一句 ``not _is_absolute → NORMAL`` 把相对路径
    整个放过，于是删到工作区外三级只判 warn，比删 ``D:/other/x``（critical）
    还轻。这句是这次改动里**唯一抬档**的地方。
    """
    assert _tier(command) == TIER_EXTREME
    assert _severity(command) == "critical"


@pytest.mark.parametrize("command,expected", [
    ("rm -rf src", TIER_NORMAL),              # 工作区内，照旧 warn
    ("rm -rf ./src", TIER_NORMAL),
    ("rm -rf a/b/c", TIER_NORMAL),
    ("rm -rf build", TIER_RECOVERABLE),       # 可恢复词表，照旧 notice
    ("rm -rf .venv", TIER_RECOVERABLE),
])
def test_ordinary_relative_targets_are_unchanged(command: str, expected: str) -> None:
    """**这次改动不许把正常的相对路径抬档**——那是反方向的误报。"""
    assert _tier(command) == expected


def test_missing_working_directory_is_still_scope_unknown() -> None:
    """B8 不许回归：拿不到 cwd 是「范围未知」，不是最坏。"""
    assert assess_tier("Bash", {"command": "rm -rf x"}, None) == TIER_SCOPE_UNKNOWN
    assert _severity("rm -rf x", None) == "warn"
    # 家目录与 cwd 无关，照样极高危
    assert _tier("rm -rf ~", None) == TIER_EXTREME


# ── 闸门与审批卡片共用一套 containment ──────────────────────────────────


@pytest.mark.parametrize("path,root,expected", [
    ("src", r"D:\w\proj", True),
    (r"D:\w\proj\src", r"D:\w\proj", True),
    (r"D:\w\proj", r"D:\w\proj", True),          # 等于根算在内
    ("./src", r"D:\w\proj", True),
    ("../other", r"D:\w\proj", False),           # 折叠 .. 之后在根外
    (r"D:\w\other", r"D:\w\proj", False),
    ("/tmp/x", r"D:\w\proj", False),
    ("x", "", False),                            # 没有根 → 说不清，按不在
])
def test_path_is_under_folds_parent_traversal(path: str, root: str,
                                              expected: bool) -> None:
    """``..`` 必须被折叠，否则 ``../x`` 永远比不中前缀。"""
    assert path_is_under(path, root) is expected


def test_card_and_gate_agree_on_the_same_command(tmp_path) -> None:
    """同一条命令，卡片说的"在不在工作目录内"必须和闸门的档位一致。

    改前这里是两份判据：卡片 ``Path.resolve()``（会折叠 ``..``），
    闸门 ``commands._under``（纯字符串前缀，且相对路径根本不查）。
    于是 ``rm -rf ../x`` 卡片说"外"、闸门说 normal——同一份事实两个答案，
    用户只能不信其中一边。

    用 ``tmp_path`` 真目录：卡片那行要 ``stat`` 才说得出"在内/在外"。
    """
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    cwd = str(root)

    cases = (("rm -rf src", False), ("rm -rf ../other", True))
    for command, outside in cases:
        facts = local_facts_for(_record(command, cwd))
        assert ("在工作目录外" in facts) is outside, facts
        assert (tier_for_command(command, cwd) == TIER_EXTREME) is outside, command


# ── 修复的收益：模型审查摸得到这些命令了 ────────────────────────────────


@pytest.mark.parametrize("command", [
    "rm -rf build/*",
    'rm -rf build --exclude="*.log"',
    "rm -rf dist && echo \"*\"",
])
def test_bounded_globs_no_longer_veto_the_model_review(command: str) -> None:
    """档位不再是极高危 → ``auto_review_gate`` 第三道墙不再一票否决。

    这条是这次改动**真正的收益**，也是它值得做的理由：改前一个 ``*``
    同时废掉播报、L2 自动放行、B9 模型审查三条路，DSH 那套
    "只让 low+allow 通过"的设计根本摸不到这些命令。
    """
    severity, _, _ = assess_risk("Bash", {"command": command}, WD)
    tier = assess_tier("Bash", {"command": command}, WD)
    assert tier != TIER_EXTREME
    ok, why = auto_review_gate(severity=severity, rule="delete", risk_tier=tier,
                               enabled=True,
                               authorized_scopes=("auto_review",))
    assert ok is True, f"模型还是问都不问：{why}"


@pytest.mark.parametrize("command", ["rm -rf *", "rm -rf $HOME", "rm -rf ../.."])
def test_genuinely_extreme_still_vetoes_everything(command: str) -> None:
    """真极高危不许被这次改动放进任何自动路径。"""
    severity, _, _ = assess_risk("Bash", {"command": command}, WD)
    tier = assess_tier("Bash", {"command": command}, WD)
    assert tier == TIER_EXTREME

    ok, why = auto_review_gate(severity=severity, rule="delete", risk_tier=tier,
                               enabled=True, authorized_scopes=("auto_review",))
    assert ok is False, f"{command} 居然过了 auto_review_gate：{why}"

    ok, why = auto_decide_gate(severity=severity, ai_judgement=True, has_key=True,
                               auto_decide="low_risk_only", max_severity="critical",
                               authorized_scopes=("ai_judgement", "auto_decide"),
                               action_class="recoverable", readonly=True)
    assert ok is False, f"{command} 居然过了 auto_decide_gate：{why}"
