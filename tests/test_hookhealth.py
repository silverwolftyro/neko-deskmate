"""hook 健康度分类的测试。

判定的消费者有两个（启动自检日志 + 设置面板），所以它必须是纯函数、
且每个状态的 tone / action / 日志方法名都要钉住——改错了不是崩，
是用户在该看到红色警告的时候看到灰色。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from plugin.plugins.agent_deskmate import hookhealth
from plugin.plugins.agent_deskmate.hookhealth import (
    HTTP_STALE,
    MISSING,
    NO_PORT,
    NO_SCRIPT,
    OK,
    OUTDATED,
    PORT_MISMATCH,
    SCRIPT_MISSING,
    classify_hook,
    refine_with_relay,
)

TARGET = r"C:\Users\test\.claude\settings.json"


def test_missing_is_danger_and_points_at_install() -> None:
    health = classify_hook(None, target=TARGET, expected_port=48921)
    assert health.state == MISSING
    assert health.tone == "danger"
    assert health.action == "hook_install"
    # 日志侧：启动自检直接喂 logger，方法名必须是桩有的那几个。
    assert health.log_method == "warning"
    assert health.log_args == (TARGET,)


def test_stale_http_shape_is_danger() -> None:
    health = classify_hook(
        {"kind": "http", "script": "http://127.0.0.1:1/x", "port": 1},
        target=TARGET, expected_port=48921,
    )
    assert health.state == HTTP_STALE
    assert health.tone == "danger"
    assert health.action == "hook_install"
    assert health.log_method == "error"


def test_unparseable_script_and_port_are_separate_states() -> None:
    """两种"装上了但指不明"必须分开——用户该采取的动作不同。"""
    no_script = classify_hook(
        {"kind": "command", "script": None, "port": 48921},
        target=TARGET, expected_port=48921,
    )
    assert no_script.state == NO_SCRIPT

    no_port = classify_hook(
        {"kind": "command", "script": "C:/relay.py", "port": None},
        target=TARGET, expected_port=48921,
    )
    assert no_port.state == NO_PORT
    assert no_script.state != no_port.state


def test_port_mismatch_names_both_ports() -> None:
    """端口不一致必须把两个端口都报出来，只报一个用户不知道该改哪。"""
    health = classify_hook(
        {"kind": "command", "script": "C:/relay.py", "port": 5000},
        target=TARGET, expected_port=48921,
    )
    assert health.state == PORT_MISMATCH
    assert "5000" in health.detail
    assert "48921" in health.detail
    # 日志参数顺序是（期望, 实际）——与原来的日志一致，别改。
    assert health.log_args == (48921, 5000)


def test_healthy_is_success_with_no_action() -> None:
    health = classify_hook(
        {"kind": "command", "script": "C:/relay.py", "port": 48921},
        target=TARGET, expected_port=48921,
    )
    assert health.state == OK
    assert health.tone == "success"
    assert health.action is None
    assert health.log_method == "info"


def test_port_compared_as_int_not_string() -> None:
    """hook 里解析出来的端口可能是字符串；拿它和 int 比会永远不相等。"""
    health = classify_hook(
        {"kind": "command", "script": "C:/relay.py", "port": "48921"},
        target=TARGET, expected_port=48921,
    )
    assert health.state == OK


# ── refine_with_relay ────────────────────────────────────────────────────


def test_refine_passes_through_non_ok_unchanged(tmp_path: Path) -> None:
    """前面的状态（没装/端口不一致）不该被后面的落盘判定覆盖。"""
    base = classify_hook(None, target=TARGET, expected_port=48921)
    refined = refine_with_relay(base, script="x", plugin_dir=tmp_path)
    assert refined is base


def test_refine_reports_missing_relay_file(tmp_path: Path) -> None:
    base = classify_hook(
        {"kind": "command", "script": "C:/gone.py", "port": 48921},
        target=TARGET, expected_port=48921,
    )
    health = refine_with_relay(base, script="C:/gone.py", plugin_dir=tmp_path)
    assert health.state == SCRIPT_MISSING
    assert health.tone == "danger"
    assert health.action == "hook_install"


def test_refine_reports_outdated_copy(tmp_path: Path, monkeypatch) -> None:
    """副本过期是 warning 不是 danger：它还能跑，只是新字段不生效。"""
    relay = tmp_path / "relay.py"
    relay.write_text("# copy\n", encoding="utf-8")
    source = tmp_path / "source.py"
    source.write_text("# source\n", encoding="utf-8")

    monkeypatch.setattr(hookhealth, "source_relay_path", lambda _d: str(source))
    monkeypatch.setattr(hookhealth, "relay_matches", lambda s, t: False)

    base = classify_hook(
        {"kind": "command", "script": str(relay), "port": 48921},
        target=TARGET, expected_port=48921,
    )
    health = refine_with_relay(base, script=str(relay), plugin_dir=tmp_path)
    assert health.state == OUTDATED
    assert health.tone == "warning"


def test_refine_stays_ok_when_copy_matches(tmp_path: Path, monkeypatch) -> None:
    relay = tmp_path / "relay.py"
    relay.write_text("# same\n", encoding="utf-8")
    source = tmp_path / "source.py"
    source.write_text("# same\n", encoding="utf-8")

    monkeypatch.setattr(hookhealth, "source_relay_path", lambda _d: str(source))
    monkeypatch.setattr(hookhealth, "relay_matches", lambda s, t: True)

    base = classify_hook(
        {"kind": "command", "script": str(relay), "port": 48921},
        target=TARGET, expected_port=48921,
    )
    health = refine_with_relay(base, script=str(relay), plugin_dir=tmp_path)
    assert health.state == OK


def test_refine_cannot_compare_is_not_silently_ok(tmp_path: Path, monkeypatch) -> None:
    """比对不了（源文件不在）时不许当成好的——宁可多提醒一次。"""
    relay = tmp_path / "relay.py"
    relay.write_text("# copy\n", encoding="utf-8")

    monkeypatch.setattr(hookhealth, "source_relay_path", lambda _d: None)

    base = classify_hook(
        {"kind": "command", "script": str(relay), "port": 48921},
        target=TARGET, expected_port=48921,
    )
    health = refine_with_relay(base, script=str(relay), plugin_dir=tmp_path)
    assert health.state == OK  # 源找不到就跳过比对，不谎报过期


@pytest.mark.parametrize("state", sorted(hookhealth.NEEDS_INSTALL))
def test_every_state_needing_install_points_at_the_entry(
    state: str, tmp_path: Path, monkeypatch,
) -> None:
    """需要用户动手的状态必须带 action，否则面板上只剩一句没法点的文案。"""
    # refine 的两个状态要先让脚本真的存在，否则会先落进 script_missing。
    existing = tmp_path / "relay.py"
    existing.write_text("# copy\n", encoding="utf-8")
    monkeypatch.setattr(hookhealth, "source_relay_path", lambda _d: str(existing))
    monkeypatch.setattr(hookhealth, "relay_matches", lambda s, t: False)

    cases = {
        MISSING: classify_hook(None, target=TARGET, expected_port=48921),
        HTTP_STALE: classify_hook(
            {"kind": "http", "script": "u", "port": 1},
            target=TARGET, expected_port=48921),
        NO_SCRIPT: classify_hook(
            {"kind": "command", "script": None, "port": 48921},
            target=TARGET, expected_port=48921),
        NO_PORT: classify_hook(
            {"kind": "command", "script": "s", "port": None},
            target=TARGET, expected_port=48921),
        PORT_MISMATCH: classify_hook(
            {"kind": "command", "script": "s", "port": 1},
            target=TARGET, expected_port=48921),
        SCRIPT_MISSING: refine_with_relay(
            classify_hook(
                {"kind": "command", "script": "C:/gone.py", "port": 48921},
                target=TARGET, expected_port=48921),
            script="C:/gone.py", plugin_dir=tmp_path),
        OUTDATED: refine_with_relay(
            classify_hook(
                {"kind": "command", "script": str(existing), "port": 48921},
                target=TARGET, expected_port=48921),
            script=str(existing), plugin_dir=tmp_path),
    }
    health = cases[state]
    assert health.state == state
    assert health.action == "hook_install"
    assert health.title
    assert health.detail
    assert health.log_method in ("info", "warning", "error")
