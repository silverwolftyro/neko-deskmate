"""同桌猫冒烟测试。

init 生成的版本只断言 plugin.toml 里存在两行字符串，挡不住 entry 拼写错误、
装饰器没生效、i18n 缺键。这里补上真正会炸的那几类。
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]

_TR_KEY_RE = re.compile(r'\btr\(\s*"([^"]+)"')


def _manifest() -> dict:
    with (PLUGIN_DIR / "plugin.toml").open("rb") as stream:
        return tomllib.load(stream)


def _locales() -> dict[str, dict]:
    locales_dir = PLUGIN_DIR / "i18n"
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(locales_dir.glob("*.json"))
    }


def test_plugin_manifest_exists() -> None:
    manifest = PLUGIN_DIR / "plugin.toml"
    assert manifest.is_file()
    text = manifest.read_text(encoding="utf-8")
    assert 'id = "agent_deskmate"' in text
    assert 'entry = "plugin.plugins.agent_deskmate:AgentDeskmatePlugin"' in text


def test_entry_class_importable() -> None:
    """entry 必须真的能 import 到那个类（提前暴露拼写错误）。"""
    from plugin.sdk.plugin import NekoPluginBase

    entry = _manifest()["plugin"]["entry"]
    module_path, _, class_name = entry.partition(":")
    module = __import__(module_path, fromlist=["*"])
    cls = getattr(module, class_name)
    assert issubclass(cls, NekoPluginBase)


def test_events_registered() -> None:
    """装饰器必须真的把 startup/shutdown/status 注册进宿主事件表。

    只断言"方法存在"挡不住装饰器没生效——宿主是按 __neko_event_meta__
    发现入口的，没有这个属性，入口在插件管理器里根本不出现。
    """
    module = __import__("plugin.plugins.agent_deskmate", fromlist=["*"])
    cls = module.AgentDeskmatePlugin

    registered = {}
    for name in dir(cls):
        obj = getattr(cls, name, None)
        meta = getattr(obj, "__neko_event_meta__", None) if obj is not None else None
        if meta is not None:
            registered[meta.id] = meta

    assert set(registered) == {
        "startup", "shutdown", "config_change", "status", "dump_events", "scan_now",
        "watch", "approval", "approval_decide", "approval_explain",
        "pending_list", "audit_list",
    }, sorted(registered)
    assert registered["startup"].event_type == "lifecycle"
    assert registered["shutdown"].event_type == "lifecycle"
    assert registered["config_change"].event_type == "lifecycle"
    assert registered["status"].event_type == "plugin_entry"
    assert registered["dump_events"].event_type == "plugin_entry"
    assert registered["scan_now"].event_type == "plugin_entry"
    # M0-lite-5：审批卡片按钮的落点，必须是 plugin_entry（@ui.action 叠加在它上面）
    assert registered["approval_decide"].event_type == "plugin_entry"
    assert registered["approval_explain"].event_type == "plugin_entry"
    # 5a：审批待办与审计两个查询入口
    assert registered["pending_list"].event_type == "plugin_entry"
    assert registered["audit_list"].event_type == "plugin_entry"
    # 常驻观测轮询必须是 timer，不能是 entry——entry 只在被调用时跑
    assert registered["watch"].event_type == "timer"
    # 审批卡片也走 timer：create_card 是 async SDK 调用，hook handler 在纯线程里
    # 没有事件循环可 await，只能由 timer 代劳。
    assert registered["approval"].event_type == "timer"


def test_observation_lives_in_the_timer_not_in_an_entry() -> None:
    """观测必须在 timer 里，不能挪进 status / dump_events / scan_now。

    entry 只在被调用时跑：把观测搬进入口，等于"没人点就完全不观测"。
    scan_now 是刻意的例外——它是**只读诊断**，见它自己的文档字符串。
    """
    import inspect

    module = __import__("plugin.plugins.agent_deskmate", fromlist=["*"])
    cls = module.AgentDeskmatePlugin

    timers = [
        meta.id for meta in (
            getattr(getattr(cls, name), "__neko_event_meta__", None)
            for name in dir(cls)
        )
        if meta is not None and meta.event_type == "timer"
    ]
    # 两个 timer：watch 做观测，approval 把挂起的审批请求变成卡片。
    # 审批必须是 timer 而不是塞进 watch：create_card 是 async SDK 调用，
    # 而 hook 的 handler 跑在纯线程里，没有事件循环可 await。
    assert sorted(timers) == ["approval", "watch"]

    for entry_name in ("status", "dump_events", "scan_now"):
        source = inspect.getsource(getattr(cls, entry_name))
        assert "_poll_once" not in source, f"{entry_name} 里出现了常驻轮询调用"


def test_source_constant_is_plugin_id() -> None:
    """红线 1：播报 source 必须显式，且必须等于 plugin_id。"""
    module = __import__("plugin.plugins.agent_deskmate", fromlist=["*"])
    assert module.SOURCE == "agent_deskmate"


def test_i18n_covers_required_locales() -> None:
    """第三方插件必须带 zh-CN 与 en 两套语言（官方契约）。"""
    locales = _locales()
    assert {"zh-CN", "en"} <= set(locales), sorted(locales)


def test_i18n_keys_match_across_locales() -> None:
    """两套语言必须是同一组键；缺键会让某个语言的用户看到未翻译文本。"""
    locales = _locales()
    zh_keys = set(locales["zh-CN"])
    en_keys = set(locales["en"])
    assert zh_keys == en_keys, f"only zh-CN: {sorted(zh_keys - en_keys)}; only en: {sorted(en_keys - zh_keys)}"


def test_every_tr_key_has_translations() -> None:
    """代码里每个 tr("key") 都必须在两套语言里有翻译。"""
    locales = _locales()
    used: set[str] = set()
    for path in sorted(PLUGIN_DIR.glob("*.py")):
        used |= set(_TR_KEY_RE.findall(path.read_text(encoding="utf-8")))

    assert used, "没有找到任何 tr() 调用——用户可见文本可能硬编码了"
    for key in sorted(used):
        for locale in ("zh-CN", "en"):
            assert key in locales[locale], f"{locale} 缺少键 {key!r}"
