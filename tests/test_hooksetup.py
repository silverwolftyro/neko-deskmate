"""hooksetup.py 装配 / 卸载单测。

核心契约：

1. **只动自己的那一条**——别人的 hook 原样留着；
2. **幂等**——重装是替换，不会多出一条；
3. **备份**——写之前一定备份，路径在返回值里；
4. **自检不过就不写**——装一条跑不起来的 command hook 比不装更糟；
5. **settings 解析不了就拒绝**——覆盖一份坏 JSON 比不装严重得多。

全部用 tmp_path 里的假 settings，不碰用户真实的 ``~/.claude/settings.json``。
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from plugin.plugins.agent_deskmate.hook_relay import HEALTH_PATH as RELAY_HEALTH_PATH
from plugin.plugins.agent_deskmate.hooksetup import (
    EVENT_NAME,
    RELAY_MARKER,
    install_hook,
    installed_port,
    is_ours,
    read_settings,
    relay_command,
    relay_script_path,
    selfcheck,
    settings_path,
    uninstall_hook,
)

PLUGIN_DIR = Path(__file__).resolve().parents[1]
RELAY_SCRIPT = PLUGIN_DIR / "hook_relay.py"


@pytest.fixture()
def settings(tmp_path: Path) -> Path:
    return tmp_path / "settings.json"


@pytest.fixture()
def backups(tmp_path: Path) -> Path:
    return tmp_path / "hook-backups"


def _entries(path: Path) -> list:
    data, error = read_settings(path)
    assert error == "", error
    hooks = data.get("hooks") or {}
    return hooks.get(EVENT_NAME) or []


# ── 路径与命令形状 ────────────────────────────────────────────────────────

def test_settings_path_points_at_user_claude_config() -> None:
    """只改 ~/.claude/settings.json，别处不许拼路径。"""
    path = settings_path()
    assert path.name == "settings.json"
    assert path.parent.name == ".claude"


def test_relay_command_uses_an_absolute_script_path() -> None:
    """hook 触发时的 cwd 是 Claude Code 的工作目录，相对路径一定找不到。"""
    command = relay_command(PLUGIN_DIR, 48921)
    assert relay_script_path(PLUGIN_DIR).is_absolute()
    assert str(relay_script_path(PLUGIN_DIR)) in command
    assert "48921" in command


def test_relay_command_absolutises_a_relative_plugin_dir() -> None:
    """宿主万一传相对路径，也要装出绝对路径的命令。

    开发模式下见过传相对路径的情况。装一条相对路径的 hook，症状是
    "每次权限请求都失败"——脚本根本找不到，所以这里兜底。
    """
    command = relay_command("plugin/plugins/agent_deskmate", 48921)
    assert relay_script_path("plugin/plugins/agent_deskmate").is_absolute()
    assert "\\" in command or "/" in command
    assert not command.startswith("python \"plugin")


def test_relay_command_carries_the_marker() -> None:
    """卸载靠标记认自己，不靠端口——端口会变。"""
    command = relay_command(PLUGIN_DIR, 48921)
    assert is_ours(command)
    assert not is_ours("python something_else.py")
    assert not is_ours(None)
    assert not is_ours("")


def test_selfcheck_passes_for_the_shipped_relay() -> None:
    """ shipped 的中继脚本必须真能跑起来——这条不过就不该去装 hook。"""
    ok, why = selfcheck(PLUGIN_DIR)
    assert ok, why


def test_selfcheck_fails_when_the_script_is_missing(tmp_path: Path) -> None:
    ok, why = selfcheck(tmp_path)
    assert ok is False
    assert "中继脚本不在" in why


# ── 读 settings 的容错 ────────────────────────────────────────────────────

def test_missing_settings_is_not_an_error(settings: Path) -> None:
    """没装过 Claude Code 配置是正常状态，不是错误。"""
    data, error = read_settings(settings)
    assert data == {}
    assert error == ""


def test_malformed_settings_is_refused_not_overwritten(settings: Path) -> None:
    """解析不了就拒绝继续：覆盖一份坏 JSON 比不装 hook 严重得多。"""
    settings.write_text("{ 这不是 json", encoding="utf-8")
    _, error = read_settings(settings)
    assert "不是合法 JSON" in error


def test_non_object_settings_is_refused(settings: Path) -> None:
    settings.write_text("[1, 2, 3]", encoding="utf-8")
    _, error = read_settings(settings)
    assert "顶层不是对象" in error


def test_empty_settings_file_is_treated_as_empty(settings: Path) -> None:
    settings.write_text("", encoding="utf-8")
    data, error = read_settings(settings)
    assert data == {}
    assert error == ""


# ── 装配 ──────────────────────────────────────────────────────────────────

def test_install_writes_our_entry_and_reports_the_file(settings: Path,
                                                       backups: Path) -> None:
    result = install_hook(PLUGIN_DIR, 48921, settings=settings, backup_dir=backups)
    assert result["ok"] is True
    assert result["settings_file"] == str(settings)
    assert result["port"] == 48921
    # 没写过 settings → 没有备份可做，这个字段该是 None 而不是假路径
    assert result["backup"] is None
    entries = _entries(settings)
    assert len(entries) == 1
    hook = entries[0]["hooks"][0]
    assert hook["type"] == "command"
    assert RELAY_MARKER in hook["command"]


def test_install_keeps_other_hooks_untouched(settings: Path,
                                             backups: Path) -> None:
    """红线：只碰自己的那一条。别人的 hook 一个字节都不能改。"""
    settings.write_text(json.dumps({
        "hooks": {
            EVENT_NAME: [
                {"matcher": "Bash", "hooks": [
                    {"type": "command", "command": "echo 别人的 hook"}]},
            ],
            "PreToolUse": [
                {"matcher": "", "hooks": [{"type": "command", "command": "echo 另一个事件"}]},
            ],
        },
        "model": "someone-elses-setting",
    }, ensure_ascii=False), encoding="utf-8")

    install_hook(PLUGIN_DIR, 48921, settings=settings, backup_dir=backups)

    data, error = read_settings(settings)
    assert error == ""
    assert data["model"] == "someone-elses-setting"
    pre = data["hooks"]["PreToolUse"]
    assert pre[0]["hooks"][0]["command"] == "echo 另一个事件"
    ours = [entry for entry in data["hooks"][EVENT_NAME]
            if any(is_ours(h.get("command")) for h in entry["hooks"])]
    foreign = [entry for entry in data["hooks"][EVENT_NAME]
               if not any(is_ours(h.get("command")) for h in entry["hooks"])]
    assert len(ours) == 1
    assert foreign[0]["hooks"][0]["command"] == "echo 别人的 hook"


def test_reinstall_replaces_instead_of_appending(settings: Path,
                                                 backups: Path) -> None:
    """幂等：改了端口重装 = 替换，不是追加出第二条。"""
    install_hook(PLUGIN_DIR, 48921, settings=settings, backup_dir=backups)
    install_hook(PLUGIN_DIR, 50000, settings=settings, backup_dir=backups)
    entries = _entries(settings)
    assert len(entries) == 1
    assert installed_port(settings, PLUGIN_DIR) == 50000


def test_install_backs_up_an_existing_file(settings: Path,
                                           backups: Path) -> None:
    settings.write_text('{"model": "keep-me"}', encoding="utf-8")
    result = install_hook(PLUGIN_DIR, 48921, settings=settings, backup_dir=backups)
    assert result["ok"] is True
    backup = Path(result["backup"])
    assert backup.is_file()
    assert json.loads(backup.read_text(encoding="utf-8"))["model"] == "keep-me"
    # 原文件被改坏了也能从备份恢复
    assert json.loads(settings.read_text(encoding="utf-8"))["model"] == "keep-me"


def test_install_refuses_when_selfcheck_fails(settings: Path,
                                              backups: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """自检不过就不写文件——装一条跑不起来的 hook 比不装更糟。"""
    import plugin.plugins.agent_deskmate.hooksetup as hooksetup

    monkeypatch.setattr(hooksetup, "selfcheck",
                        lambda _plugin: (False, "找不到解释器 python"))
    result = install_hook(PLUGIN_DIR, 48921, settings=settings, backup_dir=backups)
    assert result["ok"] is False
    assert "找不到解释器" in result["reason"]
    assert not settings.exists(), "自检没过居然写了 settings"


def test_install_refuses_on_malformed_settings(settings: Path,
                                               backups: Path) -> None:
    settings.write_text("{ 坏", encoding="utf-8")
    result = install_hook(PLUGIN_DIR, 48921, settings=settings, backup_dir=backups)
    assert result["ok"] is False
    assert "不是合法 JSON" in result["reason"]
    # 坏文件原样留着，等用户手工修
    assert settings.read_text(encoding="utf-8") == "{ 坏"


# ── 卸载 ──────────────────────────────────────────────────────────────────

def test_uninstall_removes_only_ours(settings: Path, backups: Path) -> None:
    settings.write_text(json.dumps({
        "hooks": {EVENT_NAME: [
            {"matcher": "Bash", "hooks": [{"type": "command",
                                          "command": "echo 别人的"}]},
        ]},
    }, ensure_ascii=False), encoding="utf-8")
    install_hook(PLUGIN_DIR, 48921, settings=settings, backup_dir=backups)

    result = uninstall_hook(settings=settings, backup_dir=backups)

    assert result["ok"] is True
    assert result["removed"] == 1
    entries = _entries(settings)
    assert len(entries) == 1
    assert entries[0]["hooks"][0]["command"] == "echo 别人的"


def test_uninstall_drops_the_event_key_when_empty(settings: Path,
                                                  backups: Path) -> None:
    """只剩我们一条时，把整个事件键删掉，不留空数组。"""
    install_hook(PLUGIN_DIR, 48921, settings=settings, backup_dir=backups)
    uninstall_hook(settings=settings, backup_dir=backups)
    data, error = read_settings(settings)
    assert error == ""
    assert "hooks" not in data


def test_uninstall_with_nothing_installed_is_a_no_op(settings: Path,
                                                     backups: Path) -> None:
    settings.write_text("{}", encoding="utf-8")
    result = uninstall_hook(settings=settings, backup_dir=backups)
    assert result["ok"] is True
    assert result["removed"] == 0


def test_uninstall_recognises_our_entry_after_the_plugin_moved(
        settings: Path, backups: Path) -> None:
    """按标记认，不按脚本路径认——插件目录搬了家也要认得出自己装的那条。"""
    install_hook(PLUGIN_DIR, 48921, settings=settings, backup_dir=backups)
    data, _ = read_settings(settings)
    # 模拟用户把插件搬到了别处：命令里的路径已经不存在了
    data["hooks"][EVENT_NAME][0]["hooks"][0]["command"] = (
        f'python "D:\\ somewhere \\ else \\ hook_relay.py" 48921  # {RELAY_MARKER}')
    settings.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    result = uninstall_hook(settings=settings, backup_dir=backups)

    assert result["removed"] == 1
    assert _entries(settings) == []


def test_install_then_uninstall_round_trip(settings: Path,
                                           backups: Path) -> None:
    install_hook(PLUGIN_DIR, 48921, settings=settings, backup_dir=backups)
    assert installed_port(settings, PLUGIN_DIR) == 48921
    uninstall_hook(settings=settings, backup_dir=backups)
    assert installed_port(settings, PLUGIN_DIR) is None
    assert _entries(settings) == []


def test_installed_port_returns_none_for_a_foreign_entry(settings: Path) -> None:
    settings.write_text(json.dumps({
        "hooks": {EVENT_NAME: [
            {"matcher": "", "hooks": [{"type": "command", "command": "echo 别人的"}]},
        ]},
    }, ensure_ascii=False), encoding="utf-8")
    assert installed_port(settings, PLUGIN_DIR) is None


def test_installed_port_survives_the_quoted_path(settings: Path,
                                                backups: Path) -> None:
    """端口解析要扛住引号——脚本路径在命令里是带引号的。

    第一版按"第一个 token 是端口"解析，结果切出来的是 ``"``，
    ``int('"')`` 炸掉 → 静默返回 None → 启动时永远报"没装 hook"。
    """
    install_hook(PLUGIN_DIR, 51234, settings=settings, backup_dir=backups)
    assert installed_port(settings, PLUGIN_DIR) == 51234


# ── 已存在的老 http hook：必须替换，不能追加 ──────────────────────────────

def _legacy_http_settings() -> dict:
    """真机验收时用户手写进项目 settings 的那个形状（规格 §18.3）。"""
    return {
        "permissions": {"allow": ["Bash(ls:*)"]},
        "env": {"PYTHONIOENCODING": "utf-8"},
        "hooks": {EVENT_NAME: [
            {"matcher": "", "hooks": [
                {"timeout": 600, "type": "http",
                 "url": "http://127.0.0.1:48921/hook/permission",
                 "onFailure": "block"},
            ]},
        ]},
    }


def test_legacy_http_hook_is_recognised_as_ours() -> None:
    """老的 http + onFailure:block 形状必须认成自己的。

    不认的后果是装配时**追加**第二条：两个 hook 同时跑，而那个带 block 的
    旧 hook 正是插件没开时会把该目录下所有权限请求卡死的形状（实测）。
    """
    from plugin.plugins.agent_deskmate.hooksetup import is_our_hook

    legacy = {"type": "http", "url": "http://127.0.0.1:48921/hook/permission",
              "onFailure": "block"}
    assert is_our_hook(legacy)
    assert is_our_hook({"type": "http", "url": "http://localhost:1/hook/permission"})
    # 别人的 http hook 不能动
    assert not is_our_hook({"type": "http", "url": "http://127.0.0.1:8787/hooks/x"})
    assert not is_our_hook({"type": "command", "command": "echo 别人的"})


def test_install_replaces_the_legacy_http_hook_instead_of_appending(
        settings: Path, backups: Path) -> None:
    """规格 §18.3：已存在的老 hook 要被替换，不是旁边再加一条。"""
    settings.write_text(json.dumps(_legacy_http_settings(), ensure_ascii=False),
                        encoding="utf-8")

    result = install_hook(PLUGIN_DIR, 48921, settings=settings, backup_dir=backups)

    assert result["ok"] is True
    assert result["replaced_here"] == 1
    entries = _entries(settings)
    assert len(entries) == 1, "老 hook 没被替换掉，变成两条同时跑了"
    hook = entries[0]["hooks"][0]
    assert hook["type"] == "command"
    assert RELAY_MARKER in hook["command"]
    # 危险的 onFailure:block 必须消失
    assert "onFailure" not in hook
    # 别人的设置一个字节不动
    data, error = read_settings(settings)
    assert error == ""
    assert data["permissions"]["allow"] == ["Bash(ls:*)"]
    assert data["env"]["PYTHONIOENCODING"] == "utf-8"


def test_install_cleans_our_hook_from_other_locations(settings: Path,
                                                      backups: Path,
                                                      tmp_path: Path) -> None:
    """装在用户级 settings，但要摘掉项目级那份老的——否则两个 hook 同时跑。"""
    project = tmp_path / "project-settings.json"
    project.write_text(json.dumps(_legacy_http_settings(), ensure_ascii=False),
                       encoding="utf-8")

    result = install_hook(PLUGIN_DIR, 48921, settings=settings, backup_dir=backups,
                          extra_locations=[project])

    cleaned = result["cleaned_elsewhere"]
    assert len(cleaned) == 1
    assert cleaned[0]["removed"] == 1
    assert cleaned[0]["error"] == ""
    # 项目 settings 里的 hooks 段被摘干净，其余设置保留
    data, error = read_settings(project)
    assert error == ""
    assert "hooks" not in data
    assert data["env"]["PYTHONIOENCODING"] == "utf-8"


def test_install_reports_nothing_cleaned_when_other_locations_are_clean(
        settings: Path, backups: Path, tmp_path: Path) -> None:
    other = tmp_path / "other.json"
    other.write_text('{"model": "x"}', encoding="utf-8")
    result = install_hook(PLUGIN_DIR, 48921, settings=settings, backup_dir=backups,
                          extra_locations=[other])
    assert result["cleaned_elsewhere"][0]["removed"] == 0

# ── 中继脚本本身：活着转发，死了交还 ──────────────────────────────────────

def _run_relay(args: list[str], stdin: bytes) -> tuple[int, str]:
    proc = subprocess.run([sys.executable, str(RELAY_SCRIPT), *args],
                          input=stdin, capture_output=True, timeout=30)
    return proc.returncode, proc.stdout.decode("utf-8")


def test_relay_and_gateway_agree_on_the_health_path() -> None:
    """中继探的路径必须是网关应答的那一个。

    两边各写一份常量（中继不能 import 插件包），写歪了的症状是
    "插件明明活着却每次都被当成死了"——静默且难查，所以钉一条测试。
    """
    from plugin.plugins.agent_deskmate.approval import HEALTH_PATH as GATEWAY

    assert RELAY_HEALTH_PATH == GATEWAY


def test_relay_selfcheck_emits_ok() -> None:
    code, out = _run_relay(["--selfcheck"], b"")
    assert code == 0
    assert json.loads(out)["ok"] is True


def test_relay_emits_empty_object_when_the_plugin_is_down() -> None:
    """插件没起 → 输出空对象，把决定权交还 Claude Code 原生流程。

    这是整件事的安全底线：command hook 退出码 0 + 无 decision = 非阻断，
    CC 弹自己的权限框。不会像 http + onFailure:"block" 那样把用户卡死。
    """
    code, out = _run_relay(["49999"], json.dumps({"tool_name": "Bash"}).encode())
    assert code == 0
    assert json.loads(out) == {}
    assert "hookSpecificOutput" not in out


def test_relay_emits_empty_object_without_a_port() -> None:
    code, out = _run_relay([], json.dumps({"tool_name": "Bash"}).encode())
    assert code == 0
    assert json.loads(out) == {}


def test_relay_emits_empty_object_on_empty_stdin() -> None:
    code, out = _run_relay(["49999"], b"")
    assert code == 0
    assert json.loads(out) == {}


def test_relay_forwards_when_the_plugin_is_alive() -> None:
    """插件活着 → 原样转发请求，并把插件的应答原样交给 CC。"""
    from plugin.plugins.agent_deskmate.approval import ApprovalGateway

    class _Log:
        def __init__(self):
            self.lines = []

        def _l(self, _lv, _m, *a):
            return

        def info(self, m, *a):
            return

        def warning(self, m, *a):
            return

        def error(self, m, *a):
            return

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    gateway = ApprovalGateway(_Log(), port=port, timeout_seconds=30.0)
    assert gateway.start()
    try:
        # 后台发请求并让它挂起，用户随后点允许
        import threading

        result = {}

        def _post():
            result["response"] = _run_relay(
                [str(port)], json.dumps({
                    "hook_event_name": "PermissionRequest", "session_id": "s",
                    "tool_name": "Bash", "tool_input": {"command": "ls -la"},
                    "cwd": "D:\\"}).encode())

        thread = threading.Thread(target=_post, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and gateway.undecided_count() == 0:
            time.sleep(0.01)
        pending = gateway.take_new()[0]
        gateway.resolve(pending.request.request_id, "allow", decided_by="user")
        thread.join(timeout=20)

        code, out = result["response"]
        assert code == 0
        body = json.loads(out)
        assert body["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    finally:
        gateway.stop()


def test_relay_emits_empty_object_when_the_plugin_has_no_decision() -> None:
    """插件回 503（到期无人决定）→ 中继输出空对象，CC 回原生流程。

    503 不是错误，是插件明确的"我没有决定"——交还原生流程正是规格要的。
    """
    from plugin.plugins.agent_deskmate.approval import ApprovalGateway

    class _Log:
        def info(self, *_a):
            return

        def warning(self, *_a):
            return

        def error(self, *_a):
            return

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    gateway = ApprovalGateway(_Log(), port=port, timeout_seconds=0.3)
    assert gateway.start()
    try:
        code, out = _run_relay(
            [str(port)], json.dumps({
                "hook_event_name": "PermissionRequest", "session_id": "s",
                "tool_name": "Bash", "tool_input": {"command": "ls"},
                "cwd": "D:\\"}).encode())
        assert code == 0
        assert json.loads(out) == {}
    finally:
        gateway.stop()
