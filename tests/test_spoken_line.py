"""语音线：哪几条通知值得让 Yui 开口，以及念的那一句长什么样。

**这批测试盯两件事**，对应 2026-11-26/27 两次真机事故：

1. **该念的才念**。`_should_speak` 是唯一决定点，按「值不值得打断人」
   分，不按事件多少分。`action` 一条都不许进去——每一步工具调用都念一句
   就是滴答式播报。
2. **念的那一句里没有路径、没有 UUID**。用户原话：
   「他把这句话读出来了，他真就读出来了」「特别是直接读路径，那个更不应该」。
   这条是回归测试：`_who()` 优先给 workspace（那是给眼睛的），谁哪天
   把它直接填进 `spoken`，这里必须红。

第 2 条用**扫全部产出**的方式钉，不逐条写期望值——期望值会漏掉新加的角色，
而"任何一句都不能有路径"这个不变式不会。
"""

from __future__ import annotations

import re

import pytest

from plugin.plugins.agent_deskmate import _should_speak
from plugin.plugins.agent_deskmate.progress import Light, Role
from plugin.plugins.agent_deskmate.tracker import (
    Notice,
    ProgressTracker,
    TrackerConfig,
)

T0 = 1_700_000_000.0
# 会话 UUID 形状 + 路径形状。**逐字符扫**而不是"看起来不像"：
# 一句念白里出现任何一个，就是上面那次事故重演。
_UNSPEAKABLE = re.compile(
    r"[\\/]"                      # 路径分隔符（正反斜杠都算）
    r"|[A-Za-z]:"                 # 盘符
    r"|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}"   # UUID 前两段
    r"|\.tmp\b"                   # 临时文件后缀
)


def _notice(role: str, light: str = Light.IDLE) -> Notice:
    return Notice(
        role=role, light=light, agent="claude-code", session="s1",
        who="claude-code", headline="h", detail="d", at=T0,
        fingerprint="f",
    )


# ── 1. 该念的才念 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("role", sorted(
    {Role.ACTION, Role.OUTCOME, Role.BOUNDARY, Role.WAITING}))
@pytest.mark.parametrize("light", sorted(
    {Light.IDLE, Light.RUNNING, Light.WAITING, Light.ERROR}))
def test_speak_matrix_is_exactly_three_cases(role: str, light: str) -> None:
    """全组合只有三个 True，多一个少一个都红。

    穷举 4×4 而不是挑几条：这张表是**产品口径**（`待处理命题` §2 定稿那张），
    漏一种组合就等于给某个没人讨论过的情形定了性。
    """
    expected = (
        role == Role.BOUNDARY
        or (role == Role.OUTCOME and light == Light.ERROR)
        or role == Role.WAITING
    )
    assert _should_speak(_notice(role, light)) is expected


def test_action_never_speaks() -> None:
    """每一步工具调用都不念。这是滴答式播报那道闸，单独钉一条。"""
    for light in sorted({Light.IDLE, Light.RUNNING, Light.WAITING,
                         Light.ERROR}):
        assert _should_speak(_notice(Role.ACTION, light)) is False


def test_successful_outcome_does_not_speak() -> None:
    """成功的 outcome 不念——哪怕哪天它开始播通知了。

    今天成功的 outcome 根本不产通知（见 ``tracker._on_outcome``），
    所以这条挡的是**将来**：不能因为"outcome 会念"就把成功也带上。
    """
    assert _should_speak(_notice(Role.OUTCOME, Light.RUNNING)) is False


# ── 2. 念的那一句：短、能念、没有路径没有 UUID ──────────────────────────

def _all_spoken(notices: list[Notice]) -> list[str]:
    """把一批通知里**非空**的 spoken 收回来。扫不变式用。"""
    return [n.spoken for n in notices if n.spoken]


def test_no_spoken_line_contains_a_path_or_session_id() -> None:
    """任何角色、任何情形，念的那一句里不许出现路径或会话 id。

    这是 2026-11-26 那次事故的回归测试：她把
    「claude-code · D:\\workspace\\…\\agent_deskmate 这一轮跑完了
      第 1 轮，共 2 步（Bash 2 次） · 会话 xxxxxxxx」
    整条念了出来。`_who()` 至今仍优先给 workspace——那是给眼睛的；
    语音侧另走 `_says()`，谁把两者合回去，这条必须红。
    """
    spoken = _all_spoken(_drive_every_role())
    assert spoken, "一条都没采到——测试自己失效了，不是插件没问题"
    for line in spoken:
        assert line, "spoken 不许为空：空表示退回 headline，而 headline 有路径"
        assert not _UNSPEAKABLE.search(line), f"这句念不出来：{line!r}"
        assert len(line) <= 60, f"太长，念不完：{line!r}"


def test_every_spoken_line_ends_with_a_full_stop() -> None:
    """念白以句号收尾——她照念的时候这是断句依据。"""
    for line in _all_spoken(_drive_every_role()):
        assert line.endswith(("。", ".")), f"{line!r}"


def test_the_agent_name_is_separated_by_a_space() -> None:
    """agent 名与后面那句之间**必须有一个空格**。

    `claude-code` 和中文之间没有分词，TTS 会把两者粘成一个词。
    0.6.21 出包前跑了一遍原句预览，打出来是「claude-code出事了」——
    才发现 `_says()` 拼的时候漏了空格，而 `_spoken_line`（跑完那条）
    一直有。两边要对上。
    """
    for line in _all_spoken(_drive_every_role()):
        assert "claude-code " in line, f"主语后面没有空格：{line!r}"
        assert "claude-code出" not in line, f"粘住了：{line!r}"


# ── 3. 三个角色各自真的产出了念白 ───────────────────────────────────────

def test_boundary_speaks_the_round_summary() -> None:
    _, notices = _run(
        _action(action_id="tu_1", kind="Bash"),
        _ok(at=T0 + 1, action_id="tu_1"),
        _boundary(at=T0 + 2),
    )
    boundary = [n for n in notices if n.role == Role.BOUNDARY]
    assert len(boundary) == 1
    assert boundary[0].spoken, "跑完这条必须有专门念白"
    assert "跑完了" in boundary[0].spoken
    assert "Bash" in boundary[0].spoken  # 只报类别，不报命令


def test_failure_speaks_once_it_is_confirmed() -> None:
    """失败**确认之后**才念——和播报同一条时间线，不提前。"""
    tracker = ProgressTracker(TrackerConfig(failure_confirm_seconds=120.0))
    _feed(tracker, _fail(kind="Bash"))
    assert not _spoken_so_far(_tick(tracker, T0 + 60)), \
        "确认窗口内就念了——那正是假警报的来源"

    spoken = _spoken_so_far(_tick(tracker, T0 + 200))
    assert spoken, "过了确认窗口还没念——出错播报是核心痛点，不许静默"
    assert "出事了" in spoken[0]


def test_retry_streak_speaks_the_count() -> None:
    """兜圈子那条要把次数念出来——次数本身就是那个事实。"""
    tracker = ProgressTracker(TrackerConfig(retry_escalate_at=(3, 5)))
    spoken = _spoken_so_far(_feed(
        tracker, *[_fail(at=T0 + i, action_id=f"tu_{i}", kind="Bash")
                   for i in range(3)]))
    assert spoken, "连错 3 次没念"
    assert "兜圈子" in spoken[0]
    assert "3" in spoken[0]


def test_waiting_speaks_but_not_the_reason() -> None:
    """在等你——念，但**不念原因**。

    原因是自由文本（审批那条路会带 judge_reason），长度内容都不可控；
    而念出来的东西必须短。要原因去看聊天里的 detail。
    """
    reason = "D:\\workspace\\某个很长的路径\\还有更多"
    tracker = ProgressTracker()
    notices = _feed(tracker, _wait(at=T0, reason=reason))
    spoken = _all_spoken(notices)
    assert spoken, "停下来等人居然不念——这正是「要你动手」"
    assert not _UNSPEAKABLE.search(spoken[0])
    assert "等你" in spoken[0]


def test_waiting_escalation_speaks_the_elapsed() -> None:
    """等太久还在等，升级那条也要念，并且带上等了多久。"""
    tracker = _speaking(wait_escalate_seconds=180.0)
    _feed(tracker, _wait(at=T0))

    spoken = _spoken_so_far(_tick(tracker, T0 + 400))
    assert spoken, "等超时了没升级播报"
    assert "还在等你" in spoken[0]
    assert "分" in spoken[0]  # _elapsed 的形状


# ── helpers ─────────────────────────────────────────────────────────────

def _action(*, action_id: str = "tu_1", kind: str = "Bash", at: float = T0):
    from plugin.plugins.agent_deskmate.progress import Signal
    return Signal(role=Role.ACTION, agent="claude-code", session="s1",
                  workspace="D:\\workspace\\proj", at=at,
                  action_id=action_id, kind=kind)


def _ok(*, action_id: str = "tu_1", kind: str = "Bash", at: float = T0):
    from plugin.plugins.agent_deskmate.progress import Signal
    return Signal(role=Role.OUTCOME, agent="claude-code", session="s1",
                  workspace="D:\\workspace\\proj", at=at,
                  action_id=action_id, ok=True, kind=kind)


def _fail(*, action_id: str = "tu_1", kind: str = "Bash", at: float = T0):
    from plugin.plugins.agent_deskmate.progress import Signal
    return Signal(role=Role.OUTCOME, agent="claude-code", session="s1",
                  workspace="D:\\workspace\\proj", at=at,
                  action_id=action_id, ok=False, kind=kind)


def _boundary(at: float = T0):
    from plugin.plugins.agent_deskmate.progress import Signal
    return Signal(role=Role.BOUNDARY, agent="claude-code", session="s1",
                  workspace="D:\\workspace\\proj", at=at)


def _wait(at: float = T0, reason: str = ""):
    from plugin.plugins.agent_deskmate.progress import Signal
    # ``reason`` 空着就整个不带：Signal 的字段校验拒收空串
    # （progress.py 的 ``_require_text``），而"没有原因"和"原因是空的"
    # 在语义上不是一回事。
    extra = {"reason": reason} if reason else {}
    return Signal(role=Role.WAITING, agent="claude-code", session="s1",
                  workspace="D:\\workspace\\proj", at=at, **extra)


def _speaking(**cfg) -> ProgressTracker:
    """一个按**产品默认**开的 tracker：boundary 要说。

    契约层默认关（``progress.NOTIFY_DEFAULTS``：知会类且最吵），
    产品默认由 ``__init__._tracker_config`` 打开——这里照产品默认建，
    否则 boundary 一条都采不到。
    """
    return ProgressTracker(TrackerConfig(
        extra_notify_roles=frozenset({Role.BOUNDARY}), **cfg))


def _feed(tracker: ProgressTracker, *signals) -> list[Notice]:
    out: list[Notice] = []
    for signal in signals:
        out.extend(tracker.feed(signal))
    return out


def _tick(tracker: ProgressTracker, at: float) -> list[Notice]:
    return tracker.tick(at)


def _run(*signals) -> tuple[ProgressTracker, list[Notice]]:
    tracker = _speaking()
    return tracker, _feed(tracker, *signals)


def _drive_every_role() -> list[Notice]:
    """把四个角色各逼出至少一条通知，连同灯一起。

    返回**全部产出**（含 spoken 为空的老通知），让不变式测试自己扫。
    """
    tracker = _speaking(
        failure_confirm_seconds=120.0,
        retry_escalate_at=(2,),
        wait_escalate_seconds=180.0,
    )
    out: list[Notice] = []
    # action：契约默认不播，所以采不到也正常——这条测的是"万一播了别念"。
    out.extend(_feed(tracker, _action(action_id="tu_a", kind="Bash")))
    # outcome 失败 → 兜圈子（阈值 2）
    out.extend(_feed(tracker,
                     _fail(at=T0 + 1, action_id="tu_1", kind="Bash"),
                     _fail(at=T0 + 2, action_id="tu_2", kind="Bash")))
    # outcome 失败 → 确认后播「出事了」
    out.extend(_feed(tracker, _fail(at=T0 + 3, action_id="tu_3",
                                    kind="Write")))
    out.extend(_tick(tracker, T0 + 300))
    # boundary
    out.extend(_feed(tracker, _boundary(at=T0 + 400)))
    # waiting + 升级
    out.extend(_feed(tracker, _wait(at=T0 + 500, reason="D:\\长\\路径")))
    out.extend(_tick(tracker, T0 + 900))
    return out


def _spoken_so_far(notices: list[Notice]) -> list[str]:
    """只取这一步新产出的念白。"""
    return _all_spoken(notices)
