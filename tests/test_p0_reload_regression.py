"""P0 回归：重载后观测轮询 AttributeError（播报全停）。

真机症状：每次重载后每 3 秒抛一次
    AttributeError: 'AgentDeskmatePlugin' object has no attribute '_source_state'
播报全停（审批不受影响——它走另一条 timer）。

根因：观测层的可变状态**只在 on_config_change 里创建**，而
``watch_tick`` 是 3 秒 timer，它不等配置变更就可能跑第一轮。
于是 ``_ensure_watchers`` 里 ``self._source_state[...] = ...`` 直接炸。

修法：状态集中到 ``_reset_watch_state()``，``__init__`` 就调。
这条测试钉的是**构造之后立刻能跑轮询**——不靠任何入口先跑一遍。
"""

from __future__ import annotations

import json
from pathlib import Path

from plugin.plugins.agent_deskmate import AgentDeskmatePlugin

WD = r"D:\workspace\proj"


class _FakeCtx:
    """够用的宿主上下文：只给插件构造时真正碰到的那几样。"""

    def __getattr__(self, name: str):
        raise AttributeError(name)


def _tool_line(command: str, cwd: str = WD) -> str:
    return json.dumps({
        "type": "assistant",
        "sessionId": "sess-1",
        "isSidechain": False,
        "timestamp": "2026-11-23T06:00:00.000Z",
        "cwd": cwd,
        "message": {
            "role": "assistant",
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "t", "name": "Bash",
                         "input": {"command": command}}],
        },
    }, ensure_ascii=False)


def _projects(tmp_path: Path, commands: list[str]) -> Path:
    target = tmp_path / "projects" / "proj.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(_tool_line(c) for c in commands) + "\n",
                      encoding="utf-8")
    return target


def _config(tmp_path: Path) -> dict:
    return {"coalesce_seconds": 60,
            "watch": {"claude_projects_dir": str(tmp_path / "projects"),
                      "cc_switch_db": str(tmp_path / "no-such.db")}}


class _RecordingPush:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs: object) -> dict:
        self.calls.append(kwargs)
        return {"submitted": True}


def _plugin() -> AgentDeskmatePlugin:
    """构造插件但不启动 timer / 审批服务。"""
    plugin = AgentDeskmatePlugin.__new__(AgentDeskmatePlugin)
    AgentDeskmatePlugin.__init__(plugin, _FakeCtx())
    return plugin


def test_a_freshly_constructed_plugin_can_poll() -> None:
    """**P0 的回归本体**：构造完立刻 poll，不许抛 AttributeError。"""
    plugin = _plugin()
    # 属性都建起来了
    assert plugin._source_state == {}          # noqa: SLF001
    assert plugin._source_error_at == {}       # noqa: SLF001
    # 而 _ensure_watchers 会往这两个 dict 里写——写之前它们必须存在
    plugin._ensure_watchers(_config(Path(".")))  # noqa: SLF001


def test_the_first_poll_after_startup_broadcasts(tmp_path: Path) -> None:
    """真机场景：启动 → 3 秒后第一轮 poll → 删除播报真的出去。"""
    _projects(tmp_path, ["ls -la"])
    plugin = _plugin()
    plugin.push_message = _RecordingPush()      # type: ignore[assignment]

    plugin._poll_once(_config(tmp_path))         # 第一轮：只记水位
    with (tmp_path / "projects" / "proj.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(_tool_line("rm -rf D:/other/x.txt") + "\n")
    outcomes = plugin._poll_once(_config(tmp_path))

    assert outcomes, "第一轮 poll 之后没有播报"
    assert plugin.push_message.calls            # type: ignore[attr-defined]
    assert "删除" in plugin.push_message.calls[0]["parts"][0]["text"]  # type: ignore[attr-defined]


def test_a_config_reload_keeps_the_state_consistent() -> None:
    """配置变更走的是同一条重置路径——两边不许出现第二套初始化。"""
    plugin = _plugin()
    plugin._ensure_watchers(_config(Path(".")))  # noqa: SLF001
    assert plugin._source_state, "重置后状态不该是空的（刚建过适配器）"
    plugin.on_config_change()
    assert plugin._source_state == {}            # noqa: SLF001
    assert plugin._source_error_at == {}         # noqa: SLF001
    # 重置之后照样能跑
    plugin._ensure_watchers(_config(Path(".")))  # noqa: SLF001


def test_every_watch_field_is_created_by_the_reset() -> None:
    """字段清单钉死：以后往观测层加状态，只改 ``_reset_watch_state``。

    这条测试是给下一个人留的路标——再加一个 ``self._foo`` 却忘了写进
    重置方法，症状又是"每 3 秒一次 AttributeError"，查起来很费时。
    """
    plugin = _plugin()
    expected = {"_watch_paths", "_transcript_adapter", "_gateway_adapter",
                "_broadcaster", "_source_error_at", "_source_state",
                # C 批 §三：进度状态机 + 它的阈值缓存 + 积压推送队列
                "_tracker", "_tracker_cfg", "_pending_pushes"}
    assert expected <= set(vars(plugin))
    # 两个 dict 必须是**空 dict**，不是 None：调用方直接往里写
    assert plugin._source_error_at == {}         # noqa: SLF001
    assert plugin._source_state == {}            # noqa: SLF001


def test_the_status_entry_works_before_any_poll() -> None:
    """``status`` 在第一次轮询之前也要能答话（它读 _source_state）。"""
    plugin = _plugin()
    assert plugin._adapter_status()["claude_code_transcript"] == "not_started"  # noqa: SLF001
