"""红字递减（规格 Q3）+ 启动行的两层判据分开（规格 Q4）。

**Q3 要守住的一件事**：警示可以因为"看腻了"而变小，**安全行为一点都不能变**。
极高危照样永不自动放行、照样必须人点。所以这里除了测"第 n+1 次换成徽标"，
还必须测"换成徽标之后门控依然拒绝"——视觉降级 ≠ 安全降级。

**Q4 要守住的一件事**：L1 建议的可用性与 L2 的判断后端是两件事。
曾经共用一个 readiness（judge 在不在），结果模型建议明明在工作，
启动行一直显示「生效=建议否」。测试盯死两列各自算各自的。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from plugin.plugins.agent_deskmate.approval import (
    ACTION_CLASS_IRREVERSIBLE,
    ACTION_CLASS_RECOVERABLE,
    AUTO_LOW_RISK_ONLY,
    AUTO_OFF,
    DEFAULT_WARNING_FIRST_N,
    DEFAULT_WARNING_MODE,
    WARNING_ALWAYS,
    WARNING_BADGE,
    WARNING_BANNER,
    WARNING_FIRST_N,
    WARNING_MODES,
    WARNING_NEVER,
    ApprovalGateway,
    WarningPolicy,
    auto_decide_gate,
    build_card_html,
    normalize_warning_mode,
    warning_key,
)
from plugin.plugins.agent_deskmate.commands import (
    TIER_EXTREME,
    TIER_NORMAL,
    TIER_RECOVERABLE,
    TIER_UNKNOWN,
    TIERS,
)
from plugin.plugins.agent_deskmate.warncount import WarningCounter, counts_path


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


def _card(**overrides: Any) -> str:
    base: dict[str, Any] = dict(
        subject="claude-code", tool_summary="Bash: rm -rf x",
        session_id="800257f3", working_directory=None,
        command="rm -rf D:/other", action_class=ACTION_CLASS_IRREVERSIBLE,
        risk="critical")
    base.update(overrides)
    return build_card_html(**base)


# ── 档位本身 ─────────────────────────────────────────────────────────────

def test_default_is_first_n_three() -> None:
    assert DEFAULT_WARNING_MODE == WARNING_FIRST_N
    assert DEFAULT_WARNING_FIRST_N == 3
    assert WARNING_MODES == (WARNING_ALWAYS, WARNING_FIRST_N, WARNING_NEVER)


@pytest.mark.parametrize("mode", WARNING_MODES)
def test_unknown_mode_falls_back_to_the_default(mode: str) -> None:
    assert normalize_warning_mode(mode) == mode


@pytest.mark.parametrize("value", ["", None, "  ", "总是", "first-n", 7,
                                   "sometimes", "FIRST-N"])
def test_unrecognised_mode_falls_back(value: object) -> None:
    assert normalize_warning_mode(value) == DEFAULT_WARNING_MODE


@pytest.mark.parametrize("value,expected", [
    ("always", WARNING_ALWAYS), ("ALWAYS", WARNING_ALWAYS),
    (" Always ", WARNING_ALWAYS), ("first_n", WARNING_FIRST_N),
    ("FIRST_N", WARNING_FIRST_N), (" never ", WARNING_NEVER),
])
def test_mode_names_are_case_and_space_insensitive(value: str,
                                                   expected: str) -> None:
    """大小写和前后空格都容错——手改配置的人不会注意这些。"""
    assert normalize_warning_mode(value) == expected


@pytest.mark.parametrize("seen,kind", [(1, "banner"), (2, "banner"), (3, "banner"),
                                       (4, "badge"), (99, "badge")])
def test_first_n_switches_after_n_hits(seen: int, kind: str) -> None:
    """规格 Q3 的验收口径：前 n 次红字，第 n+1 次起徽标。"""
    assert WarningPolicy(mode=WARNING_FIRST_N, first_n=3, seen=seen).kind() == kind


def test_always_and_never_ignore_the_count() -> None:
    assert WarningPolicy(mode=WARNING_ALWAYS, first_n=3, seen=99).kind() == "banner"
    assert WarningPolicy(mode=WARNING_NEVER, first_n=3, seen=1).kind() == "badge"


def test_first_n_of_zero_or_negative_means_always_badge() -> None:
    """配 0 或负数：不能因此把红字永久打开（那是拿配置绕过递减）。"""
    assert WarningPolicy(mode=WARNING_FIRST_N, first_n=0, seen=1).kind() == "badge"
    assert WarningPolicy(mode=WARNING_FIRST_N, first_n=-5, seen=1).kind() == "badge"


# ── 卡片：红字 ↔ 徽标 ────────────────────────────────────────────────────

def test_banner_before_the_threshold() -> None:
    html = _card(warning=WarningPolicy(mode=WARNING_FIRST_N, first_n=3, seen=1))
    assert WARNING_BANNER in html
    assert WARNING_BADGE not in html


def test_badge_after_the_threshold() -> None:
    """规格 Q3 验收：第 4 次卡片上不再有整行红字，但有徽标。"""
    html = _card(warning=WarningPolicy(mode=WARNING_FIRST_N, first_n=3, seen=4))
    assert WARNING_BANNER not in html
    assert WARNING_BADGE in html


def test_low_risk_never_gets_a_warning_at_all() -> None:
    """徽标不是"每条都挂一个"——低风险请求上什么警示都没有。"""
    html = _card(action_class=ACTION_CLASS_RECOVERABLE, risk="notice",
                 warning=WarningPolicy(mode=WARNING_ALWAYS, seen=1))
    assert WARNING_BANNER not in html
    assert WARNING_BADGE not in html


def test_high_risk_still_needs_a_human_click_when_it_is_only_a_badge() -> None:
    """**Q3 的核心不变式**：视觉降级绝不改变行为。

    徽标态的卡片上，三个按钮一个不能少，而且不许出现任何"已自动放行"的字样。
    """
    html = _card(warning=WarningPolicy(mode=WARNING_NEVER, first_n=3, seen=99))
    for action in ("deskmate_approve", "deskmate_deny", "deskmate_explain"):
        assert f'data-neko-action="{action}"' in html
    assert "已放行" not in html and "自动允许" not in html


def test_gate_still_refuses_a_badged_high_risk_action() -> None:
    """徽标态下 L2 门控照样拒绝——这是"视觉与安全解耦"的直接证据。

    把阈值开到顶（warn 上限）、授权全签、有 key、auto_decide 开着，
    只要动作类别是不可恢复，就一律不过。
    """
    allowed, reason = auto_decide_gate(
        severity="critical", ai_judgement=True, has_key=True,
        auto_decide=AUTO_LOW_RISK_ONLY, max_severity="warn",
        authorized_scopes=("ai_judgement", "auto_decide"),
        action_class=ACTION_CLASS_IRREVERSIBLE)
    assert allowed is False
    assert "永不进入 L2" in reason


# ── 归类键 ───────────────────────────────────────────────────────────────

def test_keys_are_per_target_tier() -> None:
    """B2 之后按**目标档位**计，不按 action_class 计。

    老口径按 action_class，坑在：可恢复但极高危的请求会和所有低风险请求共用
    ``recoverable`` 这个键，点几次低风险之后那条极高危的红字就被递减掉了。
    四档各一个键，互不相撞。
    """
    assert warning_key(TIER_EXTREME) == "tier:extreme"
    assert warning_key(TIER_NORMAL) == "tier:normal"
    assert warning_key(TIER_RECOVERABLE) == "tier:recoverable"
    assert warning_key(TIER_UNKNOWN) == "tier:unknown"
    assert len({warning_key(t) for t in TIERS}) == len(TIERS)


def test_empty_tier_falls_back_to_unknown() -> None:
    """拿不到档位时按 unknown 计——不猜，也不和低风险共用一个键。"""
    assert warning_key("") == "tier:unknown"
    assert warning_key(None) == "tier:unknown"


# ── 计数：落盘、重启不丢、坏了不影响卡片 ─────────────────────────────────

def test_counts_survive_a_restart(tmp_path: Path) -> None:
    """重启不丢——否则每次重启都把用户打回"前三次"，递减白做。"""
    path = counts_path(tmp_path)
    assert WarningCounter(path).bump("irreversible") == 1
    assert WarningCounter(path).bump("irreversible") == 2
    # 全新实例 = 插件重启
    assert WarningCounter(path).seen("irreversible") == 2
    assert WarningCounter(path).bump("irreversible") == 3


def test_counts_are_per_key(tmp_path: Path) -> None:
    counter = WarningCounter(counts_path(tmp_path))
    for _ in range(4):
        counter.bump("irreversible")
    assert counter.bump("risk:notice") == 1
    assert counter.seen("irreversible") == 4


@pytest.mark.parametrize("content", ["", "{", "not json", "[]", "null",
                                     '{"a": "b"}', '{"a": null}',
                                     '{"a": true}', '{"a": [1,2]}'])
def test_a_corrupt_counts_file_means_start_over(tmp_path: Path,
                                                content: str) -> None:
    """计数文件坏了只当"从没计数过"——绝不因为一个 JSON 坏了崩掉。"""
    path = counts_path(tmp_path)
    path.write_text(content, encoding="utf-8")
    counter = WarningCounter(path)
    assert counter.seen("irreversible") == 0
    assert counter.bump("irreversible") == 1


def test_counts_file_lands_in_the_data_dir(tmp_path: Path) -> None:
    """计数跟着插件数据目录走，不写进源码目录（会被打包/被清空）。"""
    assert counts_path(tmp_path) == tmp_path / "warning_counts.json"


def test_counter_without_a_path_still_counts(tmp_path: Path) -> None:
    """没给路径（测试/异常装配）时只在内存里数，不写盘也不报错。"""
    counter = WarningCounter(None)
    assert counter.bump("x") == 1
    assert counter.bump("x") == 2


def test_writing_a_broken_counts_file_does_not_raise(tmp_path: Path) -> None:
    """目录被占成文件之类的情况下，落盘失败只影响"重启后重新递减"。"""
    blocker = tmp_path / "warning_counts.json"
    blocker.write_text("x", encoding="utf-8")
    counter = WarningCounter(blocker)
    assert counter.bump("irreversible") == 1


# ── 网关：note_warning 兜底 ──────────────────────────────────────────────

def test_gateway_note_warning_counts(tmp_path: Path) -> None:
    gateway = ApprovalGateway(ListLogger(), port=_free_port(),
                              warning_counts_path=counts_path(tmp_path))
    assert gateway.note_warning("irreversible") == 1
    assert gateway.note_warning("irreversible") == 2


def test_gateway_note_warning_survives_a_broken_path(tmp_path: Path) -> None:
    """计数挂了返回 1（当成第一次）——多显示一次红字远比少显示一次安全。"""
    blocker = tmp_path / "warning_counts.json"
    blocker.write_text("x", encoding="utf-8")
    gateway = ApprovalGateway(ListLogger(), port=_free_port(),
                              warning_counts_path=blocker)
    assert gateway.note_warning("irreversible") == 1


# ── Q4：两层 readiness 分开 ──────────────────────────────────────────────

@pytest.mark.parametrize("ai_judgement,has_key,authorized,backend,expected", [
    (True, True, True, True, True),      # 全中 → 生效
    (True, True, True, False, False),    # 建议客户端构造不出来
    (True, True, False, True, False),    # 没签字
    (True, False, True, True, False),    # 没 key
    (False, True, True, True, False),    # 配置没开
])
def test_l1_readiness_follows_the_advice_layer(ai_judgement: bool,
                                               has_key: bool,
                                               authorized: bool,
                                               backend: bool,
                                               expected: bool) -> None:
    """规格 Q4：`生效=建议` 的判据是 ai_judgement + key + 授权 + 客户端可构造。

    **不含 L2 的 judge**——judge 恒为 None 就是那次口径 bug 的成因：
    模型建议明明在工作，启动行一直显示「生效=建议否」。
    """
    gateway = ApprovalGateway(ListLogger(), port=_free_port(),
                              ai_judgement=ai_judgement, has_api_key=has_key,
                              advice_backend_ready=backend)
    if authorized:
        gateway.grant_authorization(("ai_judgement",))
    assert gateway._l1_ready() is expected  # noqa: SLF001


def test_l2_readiness_still_needs_a_judge() -> None:
    """`生效=自动` 仍然只看判断后端——两层不许再合回去。"""
    gateway = ApprovalGateway(ListLogger(), port=_free_port(),
                              ai_judgement=True, has_api_key=True,
                              auto_decide=AUTO_LOW_RISK_ONLY,
                              advice_backend_ready=True)
    gateway.grant_authorization(("ai_judgement", "auto_decide"))
    # 建议层说"生效"，自动层说"不生效"——两列必须能同时成立
    assert gateway._l1_ready() is True  # noqa: SLF001
    assert gateway._auto_decide_live() is False  # noqa: SLF001


def test_the_two_columns_do_not_share_a_judgement() -> None:
    """如果哪天有人把两列又合回一套判据，这条会红。"""
    gateway = ApprovalGateway(ListLogger(), port=_free_port(),
                              ai_judgement=True, has_api_key=True,
                              auto_decide=AUTO_OFF, advice_backend_ready=True)
    gateway.grant_authorization(("ai_judgement",))
    assert gateway._l1_ready() != gateway._auto_decide_live()  # noqa: SLF001


def test_startup_line_reports_the_advice_layer_as_live(tmp_path: Path) -> None:
    """真机反馈的那一句：卡片 3 秒建出且带理由，启动行却说「生效=建议否」。

    现在 advice_backend_ready=True 时，状态行必须是「生效=建议是」。
    """
    logger = ListLogger()
    gateway = ApprovalGateway(logger, port=_free_port(),
                              ai_judgement=True, has_api_key=True,
                              advice_backend_ready=True,
                              warning_counts_path=counts_path(tmp_path))
    gateway.grant_authorization(("ai_judgement",))
    assert gateway.start()
    try:
        line = next(text for level, text in logger.lines
                    if level == "info" and "审批服务就绪" in text)
        assert "生效=建议是/自动否" in line
        # 最后一栏叫「自动判断后端」，不叫「后端」——建议客户端也是后端，
        # 两个词混在一起就是这次口径 bug 的由来。
        assert "自动判断后端=未接入" in line
    finally:
        gateway.stop()
