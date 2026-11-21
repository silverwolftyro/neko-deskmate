"""scan_now 只读诊断单测。

核心保证有两条，都是真机上会咬人的：
  1. **不发送播报**——诊断入口一次也不该 push；
  2. **不碰常驻轮询的状态**——复用它会吞掉还没轮到的增量，并抑制真播报。

第 2 条没法直接断言"没改状态"，因为 diagnose() 根本拿不到那些状态
（它在模块级，参数只有一个 section）。这里用「不构造 Broadcaster」+
「不消耗适配器」两个角度守住：任何把常驻状态接进 diagnose 的改动，
都得先让它拿到 broadcaster 或 adapter，测试会当场炸。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import plugin.plugins.agent_deskmate as module
from plugin.plugins.agent_deskmate import diagnose
from plugin.plugins.agent_deskmate.adapters.claude_code import ClaudeCodeAdapter
from plugin.plugins.agent_deskmate.adapters.gateway import GatewayAdapter

GATEWAY_SCHEMA = """
CREATE TABLE proxy_request_logs (
    request_id TEXT, app_type TEXT, model TEXT,
    input_tokens INTEGER, output_tokens INTEGER,
    total_cost_usd TEXT, latency_ms INTEGER,
    status_code INTEGER, error_message TEXT,
    session_id TEXT, created_at INTEGER
)
"""


def _tool_line(command: str, session: str = "sess-1") -> str:
    return json.dumps({
        "type": "assistant",
        "sessionId": session,
        "isSidechain": False,
        "timestamp": "2026-11-21T06:00:00.000Z",
        "message": {
            "role": "assistant",
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "t", "name": "Bash",
                         "input": {"command": command}}],
        },
    }, ensure_ascii=False)


def _transcript(tmp_path: Path, commands: list[str]) -> Path:
    target = tmp_path / "projects" / "proj.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(_tool_line(c) for c in commands) + "\n", encoding="utf-8")
    return target


def _gateway_db(tmp_path: Path, statuses: list[int]) -> Path:
    db = tmp_path / "cc-switch.db"
    connection = sqlite3.connect(db)
    try:
        connection.execute(GATEWAY_SCHEMA)
        for index, status in enumerate(statuses):
            connection.execute(
                "INSERT INTO proxy_request_logs (app_type, status_code, created_at) "
                "VALUES ('claude', ?, ?)",
                (status, 1795193997 + index),
            )
        connection.commit()
    finally:
        connection.close()
    return db


def _section(tmp_path: Path, *, transcript: bool = True, gateway: bool = True,
             mine_roots: list[str] | None = None) -> dict:
    """按插件配置的真实层级造 ``[agent_deskmate]`` 整节。

    注意 ``coalesce_seconds`` 在**顶层**、路径在 ``watch`` 子表里——
    旧代码把 coalesce_seconds 也当 watch 的键读，永远读不到（已修）。
    """
    watch: dict[str, str] = {}
    if transcript:
        watch["claude_projects_dir"] = str(tmp_path / "projects")
    if gateway:
        watch["cc_switch_db"] = str(tmp_path / "cc-switch.db")
    config: dict = {"coalesce_seconds": 60, "watch": watch}
    if mine_roots is not None:
        config["mine"] = {"roots": mine_roots}
    return config


# ── 基本统计 ────────────────────────────────────────────────────────────────

def test_counts_events_hits_and_broadcasts(tmp_path: Path) -> None:
    _transcript(tmp_path, ["ls -la", "rm -rf build/", "git status"])
    reply = diagnose(_section(tmp_path))

    assert reply.events_read == 3
    assert reply.hits == 1
    assert reply.broadcasts == 1
    assert reply.top_rules == [{"rule": "delete", "count": 1}]
    assert len(reply.samples) == 1
    assert "rm -rf build/" in reply.samples[0]


def test_reports_both_rules_in_top_rules(tmp_path: Path) -> None:
    _transcript(tmp_path, ["rm -rf a", "rm -rf b", "del x.txt"])
    _gateway_db(tmp_path, [503, 429, 200])
    reply = diagnose(_section(tmp_path))

    # 3 条删除（指纹各不相同，各自放行）+ 网关 503 / 429 两条失败；200 不是失败
    assert reply.hits == 5
    assert reply.broadcasts == 5
    counts = {item["rule"]: item["count"] for item in reply.top_rules}
    assert counts == {"delete": 3, "failure": 2}
    # 分布按条数降序
    assert [item["rule"] for item in reply.top_rules] == ["delete", "failure"]


def test_repeated_identical_commands_are_counted_once(tmp_path: Path) -> None:
    """同一条命令重复出现：命中算 2，预计播报只算 1（和常驻轮询同一套合并规则）。"""
    _transcript(tmp_path, ["rm -rf a", "rm -rf a"])
    reply = diagnose(_section(tmp_path))

    assert reply.hits == 2
    assert reply.broadcasts == 1
    assert len(reply.samples) == 1


def test_samples_are_capped(tmp_path: Path) -> None:
    _transcript(tmp_path, [f"rm -rf dir{i}" for i in range(6)])
    reply = diagnose(_section(tmp_path))

    assert reply.hits == 6
    assert reply.broadcasts == 6
    assert len(reply.samples) == module.SCAN_SAMPLE_LIMIT


def test_missing_sources_are_reported_not_guessed(tmp_path: Path) -> None:
    reply = diagnose({})

    assert reply.events_read == 0
    assert reply.hits == 0
    assert reply.broadcasts == 0
    assert reply.top_rules == []
    assert reply.sources["claude_code_transcript"] == "未配置或目录不存在"
    assert reply.sources["gateway_metadata"] == "未配置或文件不存在"


def test_unset_config_never_falls_back_to_cwd() -> None:
    """缺配置不能静默变成"观测当前工作目录"。

    这曾经是真 bug：``_expand`` 对空值返回 ``Path()``，而 ``Path()`` 就是 ``.``、
    ``Path(".").is_dir()`` 是真的。于是配置读不到时，插件会去扫宿主进程的 cwd，
    还会打出"观测适配器就绪"的假日志。缺配置必须明说"未配置"。
    """
    assert module._expand(None) is None
    assert module._expand("") is None
    assert module._expand("   ") is None

    reply = diagnose({"claude_projects_dir": "", "cc_switch_db": ""})
    assert reply.events_read == 0
    assert reply.hits == 0
    assert reply.sources["claude_code_transcript"] == "未配置或目录不存在"
    assert reply.sources["gateway_metadata"] == "未配置或文件不存在"


def test_only_one_source_available(tmp_path: Path) -> None:
    _transcript(tmp_path, ["rm -rf a"])
    reply = diagnose(_section(tmp_path, gateway=False))

    assert reply.events_read == 1
    assert reply.hits == 1
    assert "gateway_metadata" in reply.sources


def test_per_source_limit_is_honoured(tmp_path: Path) -> None:
    _transcript(tmp_path, [f"rm -rf d{i}" for i in range(10)])
    reply = diagnose(_section(tmp_path), per_source=4)

    assert reply.events_read == 4
    assert reply.hits == 4


def test_clean_history_yields_no_hits(tmp_path: Path) -> None:
    _transcript(tmp_path, ["ls -la", "git status", "npm run build"])
    _gateway_db(tmp_path, [200, 200])
    reply = diagnose(_section(tmp_path))

    assert reply.events_read == 5
    assert reply.hits == 0
    assert reply.broadcasts == 0
    assert reply.top_rules == []


# ── 保证一：一次播报都不发送 ────────────────────────────────────────────────

def test_scan_never_builds_a_broadcaster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """谁要把播报接进诊断，先得过这一关。

    诊断的契约是"只读"。接了播报器，用户在真机上点一下 scan_now
    就会往聊天窗里灌一条——那正好是最不该发生的打扰。
    """
    _transcript(tmp_path, ["rm -rf a"])

    def exploding(*_: object, **__: object) -> None:
        raise AssertionError("scan_now 不该构造 Broadcaster")

    monkeypatch.setattr(module, "Broadcaster", exploding)
    reply = diagnose(_section(tmp_path))
    assert reply.broadcasts == 1  # 统计照样算，只是不发


def test_scan_does_not_disturb_live_adapters(tmp_path: Path) -> None:
    """诊断跑完，常驻适配器的水位必须原地不动。

    复用一个 adapter 会把增量消费掉，3 秒后的 timer 就看不到这些事件——
    播报被诊断入口吞掉，比不诊断严重得多。
    """
    target = _transcript(tmp_path, ["ls -la"])
    db = _gateway_db(tmp_path, [200])
    section = _section(tmp_path)

    live_transcript = ClaudeCodeAdapter(tmp_path / "projects")
    live_gateway = GatewayAdapter(db)
    assert live_transcript.poll() == []
    assert live_gateway.poll() == []

    diagnose(section)  # 只读诊断跑一遍

    # 水位没被推进：追加的内容仍然轮到 timer 去播
    with target.open("a", encoding="utf-8") as handle:
        handle.write(_tool_line("rm -rf leaked") + "\n")
    pending = live_transcript.poll()
    assert len(pending) == 1
    assert pending[0].command == "rm -rf leaked"

    connection = sqlite3.connect(db)
    with connection:
        connection.execute(
            "INSERT INTO proxy_request_logs (app_type, status_code, created_at) "
            "VALUES ('claude', 503, 1795194000)"
        )
    connection.close()
    assert len(live_gateway.poll()) == 1


# ── 接线：入口确实是入口 ────────────────────────────────────────────────────

def test_scan_now_is_a_plugin_entry() -> None:
    import inspect

    meta = getattr(module.AgentDeskmatePlugin.scan_now, "__neko_event_meta__", None)
    assert meta is not None
    assert meta.id == "scan_now"
    assert meta.event_type == "plugin_entry"
    assert inspect.iscoroutinefunction(module.AgentDeskmatePlugin.scan_now)


def test_scan_now_delegates_to_diagnose() -> None:
    """入口只是薄壳：真正的逻辑在可单测的模块级函数里。"""
    import inspect

    source = inspect.getsource(module.AgentDeskmatePlugin.scan_now)
    assert "diagnose" in source
    assert "to_thread" in source            # 读盘不能堵住 entry loop
    assert "broadcaster" not in source      # 不碰播报器
    assert "_poll_once" not in source       # 不碰常驻轮询


# ── 首启引导（B） ───────────────────────────────────────────────────────────
# 只打日志、不播报：这是配置建议，不是观测结果。

from plugin.plugins.agent_deskmate import _mine_roots, _to_bool, _watch_section  # noqa: E402


def test_mine_roots_empty_by_default() -> None:
    assert _mine_roots({}) == []
    assert _mine_roots({"mine": {}}) == []
    assert _mine_roots({"mine": {"roots": None}}) == []
    assert _mine_roots({"mine": {"roots": r"D:\workspace"}}) == []   # 不是列表，忽略


def test_mine_roots_expands_user_and_vars(tmp_path: Path) -> None:
    roots = _mine_roots({"mine": {"roots": [str(tmp_path), "", "  "]}})
    assert roots == [tmp_path]          # 空串被丢掉，不当成"当前目录"


def test_mine_roots_ignores_bad_types() -> None:
    assert _mine_roots({"mine": {"roots": [None, 5]}}) == [Path("5")]  # 5 → "5"，不崩
    assert _mine_roots({"mine": "not a dict"}) == []


def test_watch_section_tolerates_junk() -> None:
    assert _watch_section({}) == {}
    assert _watch_section({"watch": None}) == {}
    assert _watch_section({"watch": "x"}) == {}


def test_to_bool_defaults() -> None:
    assert _to_bool(None, True) is True
    assert _to_bool(None, False) is False
    assert _to_bool("true", False) is True
    assert _to_bool("FALSE", True) is False
    assert _to_bool("1", False) is True
    assert _to_bool("0", True) is False
    assert _to_bool("瞎写", True) is True      # 坏值不猜，退回默认
    assert _to_bool(True, False) is True
