"""B6：清理与文档对齐。

十条里最值得单独钉的是这三条，因为它们都在**骗人**：

1. ``status`` 的 ``adapters`` 写死 ``"ready"``——假状态比没有状态更糟：
   用户看到 ready 就以为删除播报一定在，而适配器可能压根没建起来。
2. ``stall_minutes`` 在配置与 schema 里，代码零读取——用户调一个不存在的旋钮。
3. ``approval.py`` 顶部写着"hooks.json 必须带 onFailure:block"，而现行设计
   恰恰是**刻意不配**它（配了插件没启动时用户全部权限请求卡死，真机实测过）。

其余是消重（``DEFAULT_MAX_SEVERITY`` 定义两次、``SUGGESTION_TEXT`` 死词表）、
脱敏收窄（别把长 commit message 打成马赛克）、打包测试排除工具产物，
以及 README 按现状重写 + 分级命名统一成「只提醒 / 给建议 / 自动放行」。
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

import plugin.plugins.agent_deskmate as module
from plugin.plugins.agent_deskmate import AgentDeskmatePlugin
from plugin.plugins.agent_deskmate.approval import (
    DEFAULT_MAX_SEVERITY,
    build_card_html,
)
from plugin.plugins.agent_deskmate.broadcast import for_log, redact
from plugin.plugins.agent_deskmate.commands import BANNED_INTERNAL, first_banned

PLUGIN_DIR = Path(__file__).resolve().parents[1]


# ── 1. status 报真实状态，不写死 ready ───────────────────────────────────

class _StatusHost:
    """只借 ``_adapter_status``：它读 ``_source_state``，别的一概不用。"""

    _adapter_status = AgentDeskmatePlugin._adapter_status
    _ADAPTER_SOURCES = AgentDeskmatePlugin._ADAPTER_SOURCES

    def __init__(self, state: dict[str, str]) -> None:
        self._source_state = dict(state)


def test_adapter_status_is_not_hardcoded_ready() -> None:
    """没建起来就不能说 ready。"""
    host = _StatusHost({})
    assert set(host._adapter_status()) == {  # noqa: SLF001
        "claude_code_transcript", "gateway_metadata"}
    assert all(value == "not_started"
               for value in host._adapter_status().values())  # noqa: SLF001


@pytest.mark.parametrize("state,expected", [
    ({"claude_code_transcript": "ready"}, "ready"),
    ({"claude_code_transcript": "unconfigured"}, "unconfigured"),
    ({"claude_code_transcript": "path_missing"}, "path_missing"),
    ({"claude_code_transcript": "init_failed"}, "init_failed"),
])
def test_adapter_status_reports_what_actually_happened(state, expected) -> None:
    host = _StatusHost(state)
    assert host._adapter_status()["claude_code_transcript"] == expected  # noqa: SLF001


def test_the_status_entry_uses_the_real_states() -> None:
    """入口不能再把那两个键写死成 "ready"——源码级盯着，别改回去。"""
    import inspect

    source = inspect.getsource(AgentDeskmatePlugin.status)
    assert '"ready"' not in source, "status 里又出现写死的 ready 了"
    assert "_adapter_status" in source


def test_a_failed_gateway_construction_is_recorded(tmp_path: Path) -> None:
    """B4 + B6 接起来：网关建不起来，状态记 init_failed，转录照样 ready。"""
    from plugin.plugins.agent_deskmate.tests.test_b4_source_isolation import (
        WatcherHost,
        _broken_db,
        _section,
        _transcript,
    )

    _transcript(tmp_path, ["ls -la"])
    _broken_db(tmp_path, name="cc-switch.db")
    host = WatcherHost()
    host._ensure_watchers(_section(tmp_path))
    states = host._adapter_status()  # noqa: SLF001
    assert states["claude_code_transcript"] == "ready"
    assert states["gateway_metadata"] == "init_failed"


# ── 2. 死配置删掉 ────────────────────────────────────────────────────────

def test_stall_minutes_is_gone_everywhere() -> None:
    """``stall_minutes`` 代码零读取 → 从配置、schema、示例里删干净。

    留着它的害处：用户在设置表单里调一个不存在的旋钮，还以为是插件坏了。
    """
    schema = json.loads((PLUGIN_DIR / "config.schema.json").read_text(encoding="utf-8"))
    # 字段在 ``properties.agent_deskmate.properties`` 里，不在顶层——
    # 老测试查错了层级，等于什么都没查（B6 自己踩过）。
    section = schema["properties"]["agent_deskmate"]["properties"]
    assert "stall_minutes" not in section, "schema 里还有 stall_minutes"
    assert "watch" in section, "别把 watch 子表一起删了"
    example = (PLUGIN_DIR / "config.example.toml").read_text(encoding="utf-8")
    assert "stall_minutes" not in example
    # 代码里也不该再读它
    for path in PLUGIN_DIR.glob("*.py"):
        assert "stall_minutes" not in path.read_text(encoding="utf-8"), path.name


def test_the_schema_still_parses_and_matches_the_example() -> None:
    """删字段不能把 schema 删坏——它还要在插件管理器里渲染表单。

    **层级要对上**：schema 的 ``properties`` 顶层是 ``plugin_runtime`` 与
    ``agent_deskmate``，我们加的字段在后者里面。老测试查了顶层
    ``properties``，等于什么都没有查（B6 自己踩过这个坑）。
    """
    schema = json.loads((PLUGIN_DIR / "config.schema.json").read_text(encoding="utf-8"))
    top = schema["properties"]
    section = top["agent_deskmate"]["properties"]
    with (PLUGIN_DIR / "config.example.toml").open("rb") as stream:
        example = tomllib.load(stream)
    for key in example:
        assert key in top, f"示例里有 schema 不认的表：{key}"
    for key in example.get("agent_deskmate", {}):
        assert key in section, f"示例里有 schema 不认的键：{key}"
    for key in example.get("agent_deskmate", {}).get("watch", {}):
        assert key in section["watch"]["properties"], (
            f"示例的 watch 里有 schema 不认的键：{key}")


# ── 3. 常量只定义一次 ────────────────────────────────────────────────────

def test_default_max_severity_is_defined_once() -> None:
    import inspect

    import plugin.plugins.agent_deskmate.approval as approval
    source = inspect.getsource(approval)
    assert len(re.findall(r"^DEFAULT_MAX_SEVERITY\s*=", source, re.M)) == 1
    assert approval.DEFAULT_MAX_SEVERITY == DEFAULT_MAX_SEVERITY


def test_the_relay_constants_are_duplicated_on_purpose() -> None:
    """中继不能 import 插件包，所以那几个常量有两份——这是**有意的**。

    但要有人盯着两边一致，否则"爬到了却不显示"，无声无息。
    """
    from plugin.plugins.agent_deskmate import hook_relay
    from plugin.plugins.agent_deskmate.approval import (
        HEALTH_PATH,
        HEALTH_SERVICE,
        UPSTREAM_FIELD,
    )

    assert hook_relay.HEALTH_PATH == HEALTH_PATH  # noqa: SLF001
    assert hook_relay.HEALTH_SERVICE == HEALTH_SERVICE  # noqa: SLF001
    assert hook_relay.UPSTREAM_FIELD == UPSTREAM_FIELD  # noqa: SLF001


# ── 4. onFailure 的说法改对了 ───────────────────────────────────────────

def test_the_docs_no_longay_claim_we_require_on_failure_block() -> None:
    """B6 第 4 条：老文档说"必须带 onFailure:block"，与现行设计相反。

    现行设计：command hook 的默认失败语义是非阻断，插件没起来时
    Claude Code 回退原生权限框。配上 block 的代价是用户全部请求卡死。
    """
    import inspect

    import plugin.plugins.agent_deskmate.approval as approval
    source = inspect.getsource(approval)
    assert "必须带 ``onFailure" not in source
    assert "刻意不配" in source or "刻意不带" in source


def test_no_decision_response_carries_no_decision() -> None:
    """与文档对齐的行为面：我们只说"我没有决定"，不假装有。"""
    from plugin.plugins.agent_deskmate.approval import no_decision_response

    body = no_decision_response(reason="expired", timeout_seconds=570)
    assert "decision" not in json.dumps(body)


# ── 5. 脱敏收窄：别把长 commit message 打成马赛克 ────────────────────────

def test_a_long_commit_message_is_not_masked() -> None:
    """老规则"40 字以上的 [A-Za-z0-9_-]"会把长说明文字整段打飞，
    日志反而看不出当时跑了什么——那也是一种静默失败。"""
    message = "git commit -m " + "a" * 60
    assert "a" * 60 in redact(message)


def test_a_long_sentence_with_spaces_is_not_masked() -> None:
    text = "git commit -m " + "fix the renderer dropping a frame on the second monitor " * 3
    assert redact(text) == text


def test_a_long_url_is_not_masked() -> None:
    url = "https://example.com/" + "verylongpathsegment/" * 6
    assert redact(url) == url


@pytest.mark.parametrize("blob", [
    "sk-" + "a1" * 30,                                  # 同时含字母与数字
    "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0" + "uv",  # base64 形状
    "d41d8cd98f00b204e9800998ecf8427e" * 2,             # 纯十六进制
])
def test_real_token_shapes_are_still_masked(blob: str) -> None:
    assert blob not in redact(f"export TOKEN={blob}")


def test_masking_does_not_touch_short_identifiers() -> None:
    text = "rm -rf node_modules && npm run build"
    assert redact(text) == text


# ── 6. 死词表删掉 ────────────────────────────────────────────────────────

def test_the_dead_suggestion_table_is_gone() -> None:
    """``SUGGESTION_TEXT`` 全项目零调用点——双份真相比没有真相更糟。"""
    import inspect

    import plugin.plugins.agent_deskmate.model_advice as advice
    assert not hasattr(advice, "SUGGESTION_TEXT")
    assert not hasattr(advice, "suggestion_text")
    source = inspect.getsource(advice)
    assert "倾向放行" not in source
    # 取值常量仍在用，不该被一起删掉
    assert advice.SUGGEST_ALLOW == "allow"
    assert advice.SUGGESTIONS == ("allow", "deny", "unsure")


# ── 7. 分级命名：用户可见处不许出现 L0/L1/L2 ─────────────────────────────

@pytest.mark.parametrize("severity,risk", [
    ("notice", "notice"), ("warn", "warn"), ("critical", "critical"),
])
def test_card_text_never_leaks_internal_tier_names(severity: str,
                                                   risk: str) -> None:
    html = build_card_html(subject="claude-code", tool_summary="Bash: rm -rf x",
                           session_id="s", working_directory=r"D:\workspace\proj",
                           command="rm -rf x", risk=risk,
                           advice_state="ready", conclusion="无法判断，需要你决定")
    for word in BANNED_INTERNAL:
        assert word not in html, f"卡片上出现了内部词 {word!r}"


def test_broadcast_text_never_leaks_internal_tier_names() -> None:
    from plugin.plugins.agent_deskmate.events import UnifiedEvent
    from plugin.plugins.agent_deskmate.rules import evaluate_delete

    source = UnifiedEvent(agent="claude-code", session="s1", event="tool_use",
                          tool="Bash", command="rm -rf ~", ts="t",
                          executor="claude-code", working_directory=r"D:\workspace\proj")
    hit = evaluate_delete("rm -rf ~", agent="claude-code", session="s1",
                          ts="t", source=source)
    assert hit is not None
    assert first_banned(hit.summary) is None
    assert first_banned(for_log("【严重】Claude Code 删除文件：rm -rf ~")) is None


def test_the_readme_names_the_three_user_facing_tiers() -> None:
    """B6 第 6 条：用户可见文案统一成这三个词。"""
    text = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
    for name in ("只提醒", "给建议", "自动放行"):
        assert name in text, f"README 里没有统一用语「{name}」"


# ── 8. README 不把没做的写成已有 ─────────────────────────────────────────

def test_the_readme_marks_unbuilt_things_as_planned() -> None:
    text = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
    # turn_end 只解析不播报
    assert "turn_end" in text
    assert "只解析" in text
    # 停滞未实现
    assert "停滞未实现" in text or "停滞播报未实现" in text
    # L2 后端未接
    assert "未接" in text


def test_the_readme_status_table_is_not_the_old_one() -> None:
    """老表写着 M0-lite-5/6 未开始——那两项早就上线了。"""
    text = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
    assert "真机验收中" not in text
    assert "未开始" not in text.replace("停滞未实现", "").replace("未实现", "")


def test_stage_marker_matches_the_readme() -> None:
    """``status`` 里的 stage 与 README 的阶梯标题对得上。"""
    assert module.STAGE == "M0-lite-6"
    text = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
    assert module.STAGE in text


# ── 9. 打包测试不再被工具产物骗 ──────────────────────────────────────────

def test_uv_lock_is_not_a_shippable_source() -> None:
    from plugin.plugins.agent_deskmate.tests.test_packaging import (
        _EXCLUDED_FILES,
        _matches,
    )

    assert "uv.lock" in _EXCLUDED_FILES
    assert not _matches("uv.lock", ["*.py", "*.json", "*.toml", "*.md"])
