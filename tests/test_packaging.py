"""打包白名单单测（``[tool.neko.build] include``）。

**为什么需要这个文件**：``include`` 一旦写出来就变成 allow-list——
名单之外的文件会被**静默丢掉**，装好的插件少个文件，而这种故障极难查
（本地跑得好好的，装上去才缺东西）。所以这里盯着一条不变式：

    源码树里每一个该发布的文件，都必须命中 include 至少一条。

新加一个文件却忘了登记 → 测试当场红，而不是等用户在真机上发现。

只检查该发布的文件：packager 自己排除的东西（``__pycache__``、``.git``、
``AGENTS.md``、``CLAUDE.md``、``.agents``、``.claude``、``.pyc`` 等）
不参与——它们的排除规则在 CLI 里，是硬安全默认值，不是我们能选的。
"""

from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]

# packager 的硬排除项（plugin/neko_plugin_cli/core/build_rules.py）。
# 这里抄一份是为了让"该发布"的判据可读；真正生效的是 CLI 里那份。
_EXCLUDED_DIRS = {"__pycache__", ".github", ".vscode", ".idea", ".pytest_cache",
                  ".mypy_cache", ".ruff_cache", ".venv", ".git", ".agents",
                  ".claude", "dist", "build"}
# packager 自己排除的文件，加上**本机工具产物**：``uv run`` 会在插件目录留一个
# ``uv.lock``，而它不在 include 白名单里 → "没有文件会被静默丢掉"这条不变式
# 当场假失败（B6 实测踩过两次）。这些文件不是源码，该发布的判据不该管它们。
_EXCLUDED_FILES = {".DS_Store", "AGENTS.md", "CLAUDE.md", ".agents", ".claude",
                   "uv.lock"}
_EXCLUDED_SUFFIXES = {".pyc", ".pyo"}


@pytest.fixture(scope="module")
def include() -> list[str]:
    with (PLUGIN_DIR / "pyproject.toml").open("rb") as stream:
        data = tomllib.load(stream)
    rules = data.get("tool", {}).get("neko", {}).get("build", {})
    assert "include" in rules, "pyproject.toml 里没有 [tool.neko.build] include"
    return list(rules["include"])


def _shippable() -> list[str]:
    """该进包的文件（相对插件目录的 posix 路径）。"""
    found: list[str] = []
    for path in sorted(PLUGIN_DIR.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(PLUGIN_DIR)
        if any(part in _EXCLUDED_DIRS for part in relative.parts[:-1]):
            continue
        if relative.parts[0] in _EXCLUDED_DIRS:
            continue
        if relative.name in _EXCLUDED_FILES:
            continue
        if relative.suffix in _EXCLUDED_SUFFIXES:
            continue
        found.append(relative.as_posix())
    return found


def _matches(path_str: str, patterns: list[str]) -> bool:
    """复刻 CLI 的 _match_pattern 语义：整路径匹配，或无斜杠时匹配 basename。"""
    for pattern in patterns:
        if fnmatch.fnmatchcase(path_str, pattern):
            return True
        if "/" not in pattern and fnmatch.fnmatchcase(
                Path(path_str).name, pattern):
            return True
    return False


def test_every_shippable_file_is_covered(include: list[str]) -> None:
    """不变式：没有文件会被静默丢掉。"""
    missing = [path for path in _shippable() if not _matches(path, include)]
    assert not missing, (
        "这些文件不在 include 白名单里，打包时会被静默丢掉："
        f"{missing}")


def test_the_config_schema_is_packaged(include: list[str]) -> None:
    """config.schema.json 必须随包发布——用户在插件管理器里就靠它渲染表单。"""
    assert _matches("config.schema.json", include), (
        "config.schema.json 不在打包白名单里，装好的插件没有它，"
        "设置页会拿不到表单定义")


def test_the_include_list_is_not_empty(include: list[str]) -> None:
    """空 include 意味着"全带"，那是另一套语义，不能稀里糊涂地空着。"""
    assert include


def test_include_patterns_are_not_too_broad(include: list[str]) -> None:
    """别图省事写个 "*"——那等于没写白名单。"""
    assert "*" not in include
    assert "**" not in include


def test_tool_artifacts_are_not_treated_as_source(include: list[str]) -> None:
    """B6：``uv run`` 留在插件目录的 ``uv.lock`` 不许让不变式假失败。

    原来它会：``uv.lock`` 不在 include 白名单里，而 ``_shippable`` 把它
    当成"该发布的源码" → ``test_every_shippable_file_is_covered`` 红，
    红的原因跟"漏登记源码"毫无关系。那种假失败训练人忽略这条测试。
    """
    assert "uv.lock" in _EXCLUDED_FILES
    # 反面证明它确实不在白名单里——否则这条测试什么都没有守住
    assert not _matches("uv.lock", include)
    assert "uv.lock" not in _shippable()
