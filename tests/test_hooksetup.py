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
import types
from pathlib import Path

import pytest

from plugin.plugins.agent_deskmate import (
    AgentDeskmatePlugin,
    hooksetup,
)
from plugin.plugins.agent_deskmate.hook_relay import HEALTH_PATH as RELAY_HEALTH_PATH
from plugin.plugins.agent_deskmate.hooksetup import (
    EVENT_NAME,
    HOOK_TIMEOUT_SECONDS,
    RELAY_MARKER,
    install_hook,
    installed_hook,
    installed_port,
    installed_relay_path,
    is_ours,
    parse_relay_command,
    read_settings,
    relay_command,
    relay_matches,
    selfcheck,
    settings_path,
    source_relay_path,
    sync_relay,
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


def test_relay_command_uses_an_absolute_script_path(tmp_path: Path) -> None:
    """hook 触发时的 cwd 是 Claude Code 的工作目录，相对路径一定找不到。"""
    command = relay_command(tmp_path / "hook_relay.py", 48921)
    assert (tmp_path / "hook_relay.py").is_absolute()
    assert str(tmp_path / "hook_relay.py") in command
    assert "48921" in command


def test_relay_command_uses_a_real_marker_flag_not_a_comment() -> None:
    """标记必须是 ``--marker <值>`` 这种真 argv 参数，不能是 ``#`` 注释。

    Windows 的 cmd 不把 ``#`` 当注释，它会作为普通参数传进去——
    白白多两个 argv，而且看起来像我们能控制它，其实不能。
    """
    command = relay_command(Path("D:/data/hook_relay.py"), 48921)
    assert "#" not in command, "命令里还有 # 注释形式的标记"
    assert f"--marker {RELAY_MARKER}" in command
    assert is_ours(command)
    assert not is_ours("python something_else.py")
    assert not is_ours(None)
    assert not is_ours("")


def test_relay_script_ignores_the_marker_argument() -> None:
    """中继脚本要能吃掉 --marker 而不把它当端口。"""
    import json
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, str(source_relay_path(PLUGIN_DIR)), "49999",
         "--marker", RELAY_MARKER],
        input=json.dumps({"tool_name": "Bash"}).encode(),
        capture_output=True, timeout=30)
    # 端口没人听 → 探活失败 → 输出空对象。关键是**不崩、不把 marker 当端口**。
    assert proc.returncode == 0
    assert json.loads(proc.stdout.decode()) == {}


def test_selfcheck_passes_for_the_shipped_relay(tmp_path: Path) -> None:
    """shipped 的中继脚本必须真能跑起来——这条不过就不该去装 hook。"""
    ok, why = sync_relay(source_relay_path(PLUGIN_DIR),
                         installed_relay_path(tmp_path))
    assert ok, why
    ok, why = selfcheck(installed_relay_path(tmp_path))
    assert ok, why


def test_selfcheck_fails_when_the_script_is_missing(tmp_path: Path) -> None:
    ok, why = selfcheck(tmp_path / "nope.py")
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

def test_install_writes_an_explicit_timeout(settings: Path,
                                            backups: Path) -> None:
    """hook 条目必须显式带 timeout=600，**不赌官方默认值**。

    我们的到期是 570s、中继转发超时是 600s。万一 command hook 的默认值是 60s，
    570s 的审批窗口会先被砍断——症状是"卡片还没点，CC 那边已经报超时"，
    极难归因。所以写死。
    """
    install_hook(PLUGIN_DIR, 48921, settings=settings, backup_dir=backups)
    hook = _entries(settings)[0]["hooks"][0]
    assert hook["timeout"] == HOOK_TIMEOUT_SECONDS == 600


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


def test_install_does_not_touch_other_files_by_default(settings: Path,
                                                       backups: Path,
                                                       tmp_path: Path) -> None:
    """默认只写传入的那一份 settings，别处一律不碰。

    用户 2026-11-22 明确要求：项目级 .claude/settings.json 不许动，
    那可能是他按项目自己维护的。要清别处得调用方显式传。
    """
    other = tmp_path / "project-settings.json"
    other.write_text(json.dumps(_legacy_http_settings(), ensure_ascii=False),
                     encoding="utf-8")
    before = other.read_text(encoding="utf-8")

    result = install_hook(PLUGIN_DIR, 48921, settings=settings, backup_dir=backups)

    assert result["cleaned_elsewhere"] == []
    assert other.read_text(encoding="utf-8") == before, "默认动了别处的文件"


def test_installed_hook_reports_the_legacy_http_shape(settings: Path) -> None:
    """残留的旧 http hook 必须被认出来并报出来，不是当"没装"。

    用户机器上可能留着一份过期的 http hook。只认 command 的话，
    启动核对会报"没有装配审批中继"——既不清它也不提它，用户完全不知情，
    而那个形状在插件没开时会把该目录下所有权限请求卡死。
    """
    settings.write_text(json.dumps(_legacy_http_settings(), ensure_ascii=False),
                        encoding="utf-8")
    found = installed_hook(settings, PLUGIN_DIR)
    assert found is not None
    assert found["kind"] == "http"
    assert found["port"] == 48921


def test_installed_hook_reports_the_command_shape(settings: Path,
                                                  backups: Path,
                                                  tmp_path: Path) -> None:
    data_dir = tmp_path / "agent_deskmate-data"
    install_hook(PLUGIN_DIR, 51234, settings=settings, backup_dir=backups,
                 data_dir=data_dir)
    found = installed_hook(settings, data_dir)
    assert found["kind"] == "command"
    assert found["port"] == 51234
    # 脚本路径也报出来：启动核对要靠它判断"中继文件还在不在"
    assert found["script"] == str(installed_relay_path(data_dir))


def test_installed_hook_ignores_a_foreign_hook(settings: Path) -> None:
    settings.write_text(json.dumps({
        "hooks": {EVENT_NAME: [
            {"matcher": "", "hooks": [{"type": "command", "command": "echo 别人的"}]},
        ]},
    }, ensure_ascii=False), encoding="utf-8")
    assert installed_hook(settings, PLUGIN_DIR) is None


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


# ── P0：中继脚本必须放在插件数据目录（2026-11-22 真机事故） ────────────────

def test_install_points_the_hook_at_the_data_dir_not_the_install_copy(
        settings: Path, backups: Path, tmp_path: Path) -> None:
    r"""hook 必须指向插件数据目录里的副本。

    真机事故：宿主把 hook 指向 ``.neko-plugin-installations\...``，
    而那个安装副本会被宿主清空重建 → hook 指向死链接 →
    ``can't open file ... hook_relay.py: [Errno 2]`` →
    卡片永不出现，用户无感知，只剩一条莫名的"端口不一致"。
    """
    data_dir = tmp_path / "agent_deskmate-data"
    result = install_hook(PLUGIN_DIR, 48921, settings=settings,
                          backup_dir=backups, data_dir=data_dir)

    assert result["ok"] is True
    assert result["relay_file"] == str(installed_relay_path(data_dir))
    # 副本真的落在那儿了
    assert installed_relay_path(data_dir).is_file()
    # hook 指向副本，不是源码目录、也不是宿主的安装副本
    assert str(installed_relay_path(data_dir)) in result["command"]
    assert "neko-plugin-installations" not in result["command"]
    assert str(source_relay_path(PLUGIN_DIR)) not in result["command"]


def test_install_rewrites_a_stale_relay_copy(settings: Path, backups: Path,
                                             tmp_path: Path) -> None:
    """副本内容与源码不一致时要重写——半份脚本跑起来更难查。"""
    data_dir = tmp_path / "agent_deskmate-data"
    copy = installed_relay_path(data_dir)
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text("# 被改坏了的一半脚本\n", encoding="utf-8")

    result = install_hook(PLUGIN_DIR, 48921, settings=settings,
                          backup_dir=backups, data_dir=data_dir)

    assert result["ok"] is True
    assert copy.read_bytes() == source_relay_path(PLUGIN_DIR).read_bytes()


def test_sync_relay_is_a_noop_when_already_current(tmp_path: Path) -> None:
    source = tmp_path / "src.py"
    source.write_text("print('x')\n", encoding="utf-8")
    target = tmp_path / "data" / "src.py"
    assert sync_relay(source, target)[0] is True
    before = target.stat().st_mtime_ns
    assert sync_relay(source, target)[0] is True
    assert target.stat().st_mtime_ns == before, "内容一致时不该重写"


def test_sync_relay_reports_a_missing_source(tmp_path: Path) -> None:
    ok, why = sync_relay(tmp_path / "nope.py", tmp_path / "d" / "x.py")
    assert ok is False
    assert "没有中继脚本" in why


@pytest.mark.parametrize("target_content,expected", [
    ("print('x')\n", False),          # 内容不同
    ("", False),                      # 空文件
])
def test_relay_matches_compares_content(tmp_path: Path, target_content: str,
                                        expected: bool) -> None:
    """逐字节比对——旧副本照样能跑，只是不发新字段（静默失效）。"""
    source = tmp_path / "src.py"
    source.write_text("print('new')\n", encoding="utf-8")
    target = tmp_path / "data" / "src.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(target_content, encoding="utf-8")
    assert relay_matches(source, target) is expected


def test_relay_matches_is_true_for_an_identical_copy(tmp_path: Path) -> None:
    source = tmp_path / "src.py"
    source.write_text("print('x')\n", encoding="utf-8")
    target = tmp_path / "data" / "src.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    assert relay_matches(source, target) is True


@pytest.mark.parametrize("missing", ["source", "target", "both"])
def test_relay_matches_is_false_when_a_file_is_missing(tmp_path: Path,
                                                       missing: str) -> None:
    """任何一边不在都算不一致——启动自检要能把它和"内容过期"区分开。"""
    source = tmp_path / "src.py"
    target = tmp_path / "data" / "src.py"
    if missing == "target":
        source.write_text("x\n", encoding="utf-8")
    elif missing == "source":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")
    # "both"：两边都不写
    assert relay_matches(source, target) is False


def test_installed_relay_lives_in_the_plugin_data_dir() -> None:
    """数据目录归插件自己管，宿主不碰——这是选它的全部理由。"""
    path = installed_relay_path("D:/some/data")
    assert path == Path("D:/some/data/hook_relay.py")


# ── 标记与端口解析 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("command,expected_port", [
    # 装配方生成的标准形状
    ('python "D:/data/hook_relay.py" 48921 --marker neko-deskmate-approval-relay',
     48921),
    # 路径带空格（必须带引号才对）
    ('python "D:/my plugins/agent_deskmate/hook_relay.py" 50000 '
     '--marker neko-deskmate-approval-relay', 50000),
    # 没有引号也认（容错：用户手改过）
    ('python D:/data/hook_relay.py 48921 --marker neko-deskmate-approval-relay',
     48921),
    # 正斜杠路径
    ('python "D:/data/hook_relay.py" 1234 --marker neko-deskmate-approval-relay',
     1234),
    # 后面又跟了带数值的选项：端口仍是紧挨路径的那个，不是 2
    ('python "D:/data/hook_relay.py" 48921 --marker neko-deskmate-approval-relay '
     '--verbose 2', 48921),
    # 路径后第一个参数不是数字时，退回找后面第一个数字
    ('python "D:/data/hook_relay.py" --port 48921 '
     '--marker neko-deskmate-approval-relay', 48921),
])
def test_parse_relay_command_finds_the_port(command: str,
                                            expected_port: int) -> None:
    parsed = parse_relay_command(command)
    assert parsed["port"] == expected_port
    assert parsed["script"] is not None


def test_parse_relay_command_handles_a_path_with_spaces() -> None:
    command = ('python "D:/my plugins/agent_deskmate/hook_relay.py" 48921 '
               '--marker neko-deskmate-approval-relay')
    parsed = parse_relay_command(command)
    assert parsed["script"] == "D:/my plugins/agent_deskmate/hook_relay.py"
    assert parsed["port"] == 48921


@pytest.mark.parametrize("command", [
    None,
    "",
    "   ",
    "python",                                  # 只有解释器
    'python "" --marker neko-deskmate-approval-relay',
    'python "D:/data/hook_relay.py" --marker neko-deskmate-approval-relay',
    'python "D:/data/hook_relay.py" abc --marker neko-deskmate-approval-relay',
    'python "D:/data/hook_relay.py --marker neko-deskmate-approval-relay',  # 引号不配对
])
def test_parse_relay_command_degrades_without_raising(command: object) -> None:
    """解析不出来就返回 None，**不许抛异常**——启动自检还要靠它说话。"""
    parsed = parse_relay_command(command)
    assert set(parsed) == {"script", "port"}
    assert parsed["port"] is None


def test_parse_relay_command_strips_only_our_marker() -> None:
    """别人的 --marker 不该被当成我们的（免得把别人的 hook 端口挖出来）。"""
    parsed = parse_relay_command(
        'python "D:/x.py" 777 --marker someone-else')
    assert parsed["port"] == 777


def test_marker_is_not_a_port() -> None:
    """标记里没有数字，不会被误当成端口。"""
    parsed = parse_relay_command(relay_command("D:/d/hook_relay.py", 48921))
    assert parsed["port"] == 48921


# ── 启动自检：旧副本要重写（规格 P0 + 本轮改了中继协议）──────────────────
#
# 2026-11-22 第三轮：中继加了上游进程链，请求体多了一个字段。
# 用户机器上还躺着一份旧副本——它照样能跑，只是**再也不发新字段**，
# 症状是卡片上永远没有「上游」那一行，且没有任何一条报错。
# 所以启动自检必须比内容，不一致就重写。

class _StubLogger:
    def __init__(self) -> None:
        self.lines: list[tuple[str, tuple]] = []

    def _log(self, level: str, args: tuple) -> None:
        self.lines.append((level, args))

    def info(self, *args: tuple) -> None:
        self._log("info", args)

    def warning(self, *args: tuple) -> None:
        self._log("warning", args)

    def error(self, *args: tuple) -> None:
        self._log("error", args)

    def messages(self, level: str) -> list[str]:
        return [" ".join(str(a) for a in args) for lv, args in self.lines
                if lv == level]


class _PluginStub:
    """只借 ``_check_installed_hook_port`` 这一个方法，不实例化真插件。

    刻意不 ``object.__new__(AgentDeskmatePlugin)``：``plugin_dir`` /
    ``data_path`` 在基类上是只读 property，实例上塞不进去，而这里要测的
    只是启动自检那几行分支。
    """

    _check_installed_hook_port = AgentDeskmatePlugin._check_installed_hook_port

    def __init__(self, data_dir: Path, plugin_dir: Path, port: int,
                 logger: _StubLogger) -> None:
        self.logger = logger
        self.plugin_dir = plugin_dir
        self.data_path = lambda name="": data_dir / name
        self._approval = types.SimpleNamespace(port=port)


def _plugin_stub(data_dir: Path, plugin_dir: Path, port: int,
                 logger: _StubLogger) -> _PluginStub:
    return _PluginStub(data_dir, plugin_dir, port, logger)


def _patch_hooksetup(monkeypatch, **replacements) -> None:
    """同一个函数名在两个模块里各有一份引用，两处都要打补丁。

    ``__init__.py`` 是 ``from .hooksetup import settings_path``，所以
    ``monkeypatch.setattr(hooksetup, "settings_path", ...)`` 改了定义处，
    调用处那份还指着原来的函数——改了没反应的坑，踩过一次就别再踩。
    """
    import plugin.plugins.agent_deskmate as package

    for name, value in replacements.items():
        monkeypatch.setattr(hooksetup, name, value)
        monkeypatch.setattr(package, name, value)


def test_startup_rewrites_a_stale_relay_copy(settings: Path, backups: Path,
                                             tmp_path: Path,
                                             monkeypatch) -> None:
    """启动时发现副本过期 → 自动重写 + 记一条 warning（不是错误）。"""
    data_dir = tmp_path / "agent_deskmate-data"
    install_hook(PLUGIN_DIR, 48921, settings=settings, backup_dir=backups,
                 data_dir=data_dir)
    copy = installed_relay_path(data_dir)
    copy.write_text("# 上一版的旧副本\n", encoding="utf-8")

    logger = _StubLogger()
    _patch_hooksetup(monkeypatch, settings_path=lambda: settings)
    _plugin_stub(data_dir, PLUGIN_DIR, 48921, logger
                 )._check_installed_hook_port(48921)  # noqa: SLF001

    assert copy.read_bytes() == source_relay_path(PLUGIN_DIR).read_bytes()
    warnings = logger.messages("warning")
    assert warnings, "重写了却没记账"
    assert "不一致" in warnings[0]
    assert not logger.messages("error")


def test_startup_stays_quiet_when_the_copy_is_current(settings: Path,
                                                      backups: Path,
                                                      tmp_path: Path,
                                                      monkeypatch) -> None:
    """副本是最新的就别吭声——warning 会变成噪音，真告警就被淹了。"""
    data_dir = tmp_path / "agent_deskmate-data"
    install_hook(PLUGIN_DIR, 48921, settings=settings, backup_dir=backups,
                 data_dir=data_dir)

    logger = _StubLogger()
    _patch_hooksetup(monkeypatch, settings_path=lambda: settings)
    _plugin_stub(data_dir, PLUGIN_DIR, 48921, logger
                 )._check_installed_hook_port(48921)  # noqa: SLF001

    assert not logger.messages("warning")
    assert not logger.messages("error")


def test_startup_reports_a_copy_it_cannot_rewrite(settings: Path,
                                                  backups: Path,
                                                  tmp_path: Path,
                                                  monkeypatch) -> None:
    """重写失败（磁盘满 / 权限）要报错，并说清去调 hook_install。

    这里直接把 sync_relay 换成必然失败的版本：真去造一个写不进去的
    目录在 Windows 上要么做不到、要么留下副作用。
    """
    data_dir = tmp_path / "agent_deskmate-data"
    install_hook(PLUGIN_DIR, 48921, settings=settings, backup_dir=backups,
                 data_dir=data_dir)
    installed_relay_path(data_dir).write_text("旧副本\n", encoding="utf-8")

    logger = _StubLogger()
    _patch_hooksetup(
        monkeypatch,
        settings_path=lambda: settings,
        sync_relay=lambda _src, _dst: (False, "写不进中继副本（模拟）"),
    )
    _plugin_stub(data_dir, PLUGIN_DIR, 48921, logger
                 )._check_installed_hook_port(48921)  # noqa: SLF001

    errors = logger.messages("error")
    assert errors, "重写失败却没报错"
    assert "hook_install" in errors[0]
    assert not logger.messages("warning")
