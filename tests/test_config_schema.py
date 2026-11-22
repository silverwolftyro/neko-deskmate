"""config.schema.json 单测。

四条核心契约：

1. **合法 JSON**，且能被当 JSON Schema 用（有 ``$schema`` 与 ``type``）；
2. **每个字段路径都能在 config.example.toml 里找到对应**——
   schema 声明了示例里没有的键 = 死配置键，正是 config.example.toml
   开头那条约定禁止的东西；
3. **enum 取值与代码常量逐一对应**——schema 给一个代码不认的值，
   用户选了下拉就静默失效（代码会退回默认值），那种故障最难查；
4. **schema 不是校验边界**：坏值由代码兜底，文件头必须把这件事写明，
   否则将来有人会以为"表单挡住了"就去掉运行时检查。

第 3 条的判据是**从代码 import 常量**，不是在测试里再抄一份字符串——
抄一份的话，代码改了测试照样绿。
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
SCHEMA_PATH = PLUGIN_DIR / "config.schema.json"
EXAMPLE_PATH = PLUGIN_DIR / "config.example.toml"


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def example() -> dict:
    with EXAMPLE_PATH.open("rb") as stream:
        return tomllib.load(stream)


def _leaf_paths(node: dict, prefix: str = "") -> list[str]:
    """递归取出所有叶子字段的点分路径（不含数组 items 内部）。"""
    found: list[str] = []
    properties = node.get("properties")
    if not isinstance(properties, dict):
        return found
    for name, child in properties.items():
        path = f"{prefix}{name}"
        found.append(path)
        found.extend(_leaf_paths(child, f"{path}."))
    return found


def _leaf_nodes(node: dict, prefix: str = "") -> list[tuple[str, dict]]:
    """``(点分路径, 字段节点)``，只取真正的字段（有 type 且没有 properties）。"""
    found: list[tuple[str, dict]] = []
    properties = node.get("properties")
    if not isinstance(properties, dict):
        return found
    for name, child in properties.items():
        path = f"{prefix}{name}"
        if isinstance(child.get("properties"), dict):
            found.extend(_leaf_nodes(child, f"{path}."))
        else:
            found.append((path, child))
    return found


def _dig_schema(schema: dict, path: str) -> dict:
    """按点分路径取 schema 里的字段节点，逐层穿过 ``properties``。"""
    node: dict = schema
    for part in path.split("."):
        properties = node.get("properties")
        if not isinstance(properties, dict) or part not in properties:
            raise KeyError(path)
        node = properties[part]
    return node


def _dig_example(data: dict, path: str) -> object:
    """按点分路径取示例配置里的值——TOML 解析出来就是普通嵌套 dict。"""
    node: object = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(path)
        node = node[part]
    return node


# ── 1. 合法 JSON + 基本形状 ───────────────────────────────────────────────

def test_schema_is_valid_json() -> None:
    # 读两次：一次确认能 parse，一次确认没有 BOM / 尾随垃圾
    raw = SCHEMA_PATH.read_text(encoding="utf-8")
    assert raw.strip(), "config.schema.json 是空的"
    assert not raw.startswith("﻿"), "文件带 BOM，JSON 解析器可能不接受"
    json.loads(raw)


def test_schema_declares_itself_as_a_json_schema(schema: dict) -> None:
    assert schema["$schema"].startswith("https://json-schema.org/draft/")
    assert schema["type"] == "object"


def test_schema_declares_it_is_not_the_validation_boundary(schema: dict) -> None:
    """文件头必须写明：schema 只管表单，不拦值。

    不写这一句，将来有人会以为"表单已经挡住了"就把运行时检查删掉——
    而表单只在插件管理器里出现，手改配置文件的人根本不过它。
    """
    note = schema.get("$comment", "")
    assert "NOT a validation boundary" in note
    assert "runtime" in note


def test_every_leaf_has_a_type(schema: dict) -> None:
    for path, node in _leaf_nodes(schema):
        assert "type" in node, f"{path} 没有 type，表单不知道怎么渲染"


# ── 2. 字段路径与 config.example.toml 一一对应 ────────────────────────────

def test_every_schema_field_exists_in_the_example(schema: dict,
                                                  example: dict) -> None:
    """schema 声明的每个键，示例配置里必须真有。

    反了方向就是"死配置键"：表单上摆着一项，改了却没有任何代码读它。
    """
    missing = [path for path in _leaf_paths(schema)
               if not _exists(example, path)]
    assert not missing, f"schema 有、示例配置里没有：{missing}"


def test_every_example_field_exists_in_the_schema(schema: dict,
                                                  example: dict) -> None:
    """示例配置里的每个键，schema 里也必须声明。

    漏一个的后果是表单上少一项：用户根本看不到它，只能去手改文件。
    """
    missing = [path for path in _example_paths(example)
               if path not in set(_leaf_paths(schema))]
    assert not missing, f"示例配置有、schema 没声明：{missing}"


def _exists(data: dict, path: str) -> bool:
    try:
        _dig_example(data, path)
    except KeyError:
        return False
    return True


def _example_paths(node: dict, prefix: str = "") -> list[str]:
    """示例配置的点分路径（只走字典，list 当叶子）。"""
    found: list[str] = []
    for name, value in node.items():
        path = f"{prefix}{name}"
        found.append(path)
        if isinstance(value, dict):
            found.extend(_example_paths(value, f"{path}."))
    return found


# ── 3. enum 与代码常量一致 ────────────────────────────────────────────────

def test_auto_decide_enum_matches_the_code(schema: dict) -> None:
    """下拉里的两个值必须正好是代码认的那两个。"""
    from plugin.plugins.agent_deskmate.approval import AUTO_DECIDE_VALUES

    node = _dig_schema(schema, "agent_deskmate.approval.auto_decide")
    assert set(node["enum"]) == set(AUTO_DECIDE_VALUES)
    assert node["default"] in node["enum"]


def test_severity_enum_matches_the_code(schema: dict) -> None:
    """风险四档必须正好是 SEVERITY_ORDER 的键，一个不多一个不少。"""
    from plugin.plugins.agent_deskmate.approval import SEVERITY_ORDER

    node = _dig_schema(schema, "agent_deskmate.approval.auto_decide_max_severity")
    assert set(node["enum"]) == set(SEVERITY_ORDER), (
        "schema 的风险档与代码的 SEVERITY_ORDER 不一致")
    assert node["default"] in node["enum"]


def test_protocol_enum_matches_the_code(schema: dict) -> None:
    """接口类型必须是代码里认的两种；代码认不出的会静默按 openai 发。"""
    from plugin.plugins.agent_deskmate.model_advice import ModelAdvisor

    node = _dig_schema(schema, "agent_deskmate.model.protocol")
    assert set(node["enum"]) == {"openai", "anthropic"}
    # 每个取值都必须真被代码接受，不会静默回退
    for value in node["enum"]:
        advisor = ModelAdvisor(enabled=True, protocol=value, api_key="k",
                              base_url="http://x", model="m")
        assert advisor.protocol == value, f"代码不认 {value}"
    assert node["default"] in node["enum"]


def test_every_enum_default_is_one_of_its_values(schema: dict) -> None:
    """默认值必须是枚举里的一个，否则表单一打开就是非法值。"""
    for path, node in _leaf_nodes(schema):
        values = node.get("enum")
        if not values:
            continue
        assert node.get("default") in values, (
            f"{path} 的默认值 {node.get('default')!r} 不在枚举 {values} 里")


def test_integer_fields_have_bounds(schema: dict) -> None:
    """整数项带 min/max：用户输个 -1 或 999999，表单当场就拦。"""
    for path, node in _leaf_nodes(schema):
        if node.get("type") != "integer":
            continue
        assert "minimum" in node, f"{path} 没有下限"
        assert "maximum" in node, f"{path} 没有上限"
        assert node["minimum"] <= node["default"] <= node["maximum"], (
            f"{path} 的默认值越界")


def test_timeout_default_stays_below_the_hook_timeout(schema: dict) -> None:
    """等待决定的时间必须小于 Claude Code 的 hook 超时（600s）。

    不小于的话我们还没到期宿主先断开，pending 永远等不到标记——
    这类事 schema 拦一道，代码里还有一道（_start_approval_service 会报错）。
    """
    from plugin.plugins.agent_deskmate import HOOK_TIMEOUT_SECONDS

    node = _dig_schema(schema, "agent_deskmate.approval.timeout_mark_expired")
    assert node["maximum"] < HOOK_TIMEOUT_SECONDS
    assert node["default"] < HOOK_TIMEOUT_SECONDS


# ── 4. 密钥永不回显 ───────────────────────────────────────────────────────

def test_api_keys_are_write_only(schema: dict) -> None:
    """两处密钥都必须是 writeOnly——否则回显一次就把 key 泄在屏幕上。"""
    for path in ("agent_deskmate.approval.api_key", "agent_deskmate.model.api_key"):
        node = _dig_schema(schema, path)
        assert node["type"] == "string", f"{path} 不该是 {node['type']}"
        assert node.get("writeOnly") is True, f"{path} 没有标 writeOnly"
        assert node.get("default") == "", f"{path} 的默认值该是空串"


def test_no_other_field_is_write_only(schema: dict) -> None:
    """只有密钥该被藏起来。多藏一个，用户就少看见一个该看见的值。"""
    hidden = [path for path, node in _leaf_nodes(schema)
              if node.get("writeOnly")]
    assert sorted(hidden) == ["agent_deskmate.approval.api_key",
                              "agent_deskmate.auto_review.api_key",
                              "agent_deskmate.model.api_key"], hidden


# ── 5. 给用户看的文字不许泄内部名词 ──────────────────────────────────────

# 这些是配置键名 / 内部术语，只能当键，不能出现在 title 或 description 里。
_FORBIDDEN_IN_PROSE = (
    "ai_judgement",
    "auto_decide",
    "coalesce_seconds",
    "stall_minutes",
    "claude_projects_dir",
    "cc_switch_db",
    "timeout_mark_expired",
    "retention_seconds",
    "api_key",
    "base_url",
    "low_risk_only",
    "onFailure",
    "hookSpecificOutput",
    "L0",
    "L1",
    "L2",
    "PermissionRequest",
)


@pytest.mark.parametrize("path,node", [
    pytest.param(path, node, id=path)
    for path, node in _leaf_nodes(
        json.loads((PLUGIN_DIR / "config.schema.json").read_text(encoding="utf-8")))
])
def test_titles_and_descriptions_hide_internal_names(path: str, node: dict) -> None:
    """title / description 是给用户看的，不许出现配置键名和内部术语。

    这是 4e9c134 修过的同一类泄漏（当时是把 ai_judgement 印在了卡片上），
    配置表单是同一个道理：用户看到 "ai_judgement" 不知道那是什么。
    """
    for key in ("title", "description"):
        text = str(node.get(key) or "")
        for forbidden in _FORBIDDEN_IN_PROSE:
            assert forbidden not in text, f"{path} 的 {key} 里出现了 {forbidden!r}"


def test_titles_and_descriptions_are_localised(schema: dict) -> None:
    """中英文都要有：缺一种语言的用户会看到未翻译的英文。"""
    for path, node in _leaf_nodes(schema):
        for key in ("x-title-i18n", "x-description-i18n"):
            bundle = node.get(key)
            assert isinstance(bundle, dict), f"{path} 缺 {key}"
            assert {"zh-CN", "en"} <= set(bundle), (
                f"{path} 的 {key} 只有 {sorted(bundle)}")
            assert bundle["zh-CN"].strip(), f"{path} 的 {key} 中文是空的"
            assert bundle["en"].strip(), f"{path} 的 {key} 英文是空的"


def test_prose_and_translations_agree(schema: dict) -> None:
    """title/description（英文）应与 x-*-i18n 的 en 一致，别两处说不同的话。"""
    for path, node in _leaf_nodes(schema):
        assert node.get("title") == node["x-title-i18n"]["en"], (
            f"{path} 的 title 与英文翻译不一致")
        assert node.get("description") == node["x-description-i18n"]["en"], (
            f"{path} 的 description 与英文翻译不一致")


def test_chinese_text_is_actually_chinese(schema: dict) -> None:
    """防手滑把英文粘进了中文栏——那种错误肉眼极难发现。"""
    for path, node in _leaf_nodes(schema):
        for key in ("x-title-i18n", "x-description-i18n"):
            text = node[key]["zh-CN"]
            assert any("一" <= char <= "鿿" for char in text), (
                f"{path} 的 {key} 中文栏里没有汉字")
