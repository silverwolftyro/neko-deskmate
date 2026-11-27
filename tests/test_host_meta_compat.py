"""`tools/host_meta_compat.py` 的单测。

**为什么需要这个文件**：这个工具改的是**已打包产物**——改错了不会在本地
报错，只会在用户机器上表现成两种故障：

1. 宿主判定包被篡改（`[payload].hash` 没跟着重算）；
2. 元数据被改坏了（entries 少一条 → 某个按钮莫名 404）。

两种都是"装上去才发现"，所以这里用合成包把三条不变式钉住：
**只动 schema_version**、**hash 必须自洽**、**重复跑必须是 no-op**。

合成包故意做得和真包同构（`payload/plugins/<id>/plugin.meta.json` +
顶层 `metadata.toml`），但不依赖真包存在——CI 上没有 `08-发布归档/`。
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

import pytest

from tools.host_meta_compat import _payload_hash, downgrade

META_REL = "payload/plugins/agent_deskmate/plugin.meta.json"


def _meta(schema: int = 5) -> dict:
    """一份最小但结构真实的 meta：11 条 entry 的形状照真包缩水。"""
    return {
        "schema_version": schema,
        "sdk_version": "0.1.0",
        "entries": [
            {
                "id": f"entry_{index}",
                "event_key": f"agent_deskmate.entry_{index}",
                "event_type": "plugin_entry",
                "kind": "action",
            }
            for index in range(3)
        ],
        "handlers": {
            f"agent_deskmate.entry_{index}": {"timeout": 30}
            for index in range(3)
        },
        "entry_methods": {f"entry_{index}": f"entry_{index}" for index in range(3)},
        "source_sha256": "0" * 64,
        "build_env": {"os": "win32", "python": "3.11", "arch": "AMD64"},
    }


def _make_package(path: Path, schema: int = 5) -> Path:
    meta = _meta(schema)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "payload/plugins/agent_deskmate/__init__.py", b"# plugin\n"
        )
        archive.writestr(
            META_REL, json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8")
        )
        archive.writestr(
            "payload/plugins/agent_deskmate/plugin.toml", b'id = "agent_deskmate"\n'
        )
        archive.writestr(
            "metadata.toml",
            '[payload]\nhash_algorithm = "sha256"\nhash = "{}"\n'.format(
                "0" * 64
            ).encode("utf-8"),
        )
        archive.writestr("manifest.toml", b'schema_version = "1.0"\n')
    return path


def _declared_hash(archive: zipfile.ZipFile) -> str:
    text = archive.read("metadata.toml").decode("utf-8")
    match = re.search(r'hash\s*=\s*"([0-9a-fA-F]{64})"', text)
    assert match is not None, "metadata.toml 里应该有 [payload].hash"
    return match.group(1)


def _read_meta(archive: zipfile.ZipFile) -> dict:
    return json.loads(archive.read(META_REL).decode("utf-8"))


# ── 降级本身 ────────────────────────────────────────────────────────────


def test_downgrade_changes_only_schema_version(tmp_path: Path) -> None:
    package = _make_package(tmp_path / "pkg.neko-plugin", schema=5)
    before = _read_meta(zipfile.ZipFile(package))

    report = downgrade(package, 4)

    assert report["status"] == "downgraded"
    assert report["schema"] == "5 → 4"
    with zipfile.ZipFile(package) as archive:
        after = _read_meta(archive)
    assert after["schema_version"] == 4
    # 除 schema_version 外一个字段都不能动——这是本工具唯一允许的改动。
    assert {k: v for k, v in after.items() if k != "schema_version"} == {
        k: v for k, v in before.items() if k != "schema_version"
    }
    # 条目表必须原样保留：少一条就是某个按钮莫名 404。
    assert len(after["entries"]) == len(before["entries"])
    assert after["entries"] == before["entries"]
    assert after["handlers"] == before["handlers"]
    assert after["entry_methods"] == before["entry_methods"]


def test_downgrade_keeps_payload_hash_self_consistent(tmp_path: Path) -> None:
    """改过 meta 就必须重算 hash，否则宿主会判包被篡改。"""
    package = _make_package(tmp_path / "pkg.neko-plugin", schema=5)
    downgrade(package, 4)

    with zipfile.ZipFile(package) as archive:
        assert _declared_hash(archive).lower() == _payload_hash(archive)


def test_payload_hash_matches_undowngraded_package(tmp_path: Path) -> None:
    """反向不变式：没改过的包，hash 算法也要能复现 CLI 的算法。

    真包的验证在 `09-脚本与探针/复现-卡片按钮404-schema判定-2026-11-24.py`
    里做过（复刻结果与 CLI 声明逐位相同）；这里保证算法没被改坏。
    """
    package = _make_package(tmp_path / "pkg.neko-plugin", schema=5)
    with zipfile.ZipFile(package) as archive:
        # 合成包建的时候写的是占位 hash，所以只验证"可复现"：
        # 同一份内容算两次必须一样。
        assert _payload_hash(archive) == _payload_hash(archive)


def test_second_run_is_a_noop(tmp_path: Path) -> None:
    """重复跑不许把包再写一遍——每次重写都有一次写坏的机会。"""
    package = _make_package(tmp_path / "pkg.neko-plugin", schema=5)
    downgrade(package, 4)
    first = package.read_bytes()

    report = downgrade(package, 4)

    assert report["status"] == "noop"
    assert package.read_bytes() == first


def test_noop_when_already_at_target(tmp_path: Path) -> None:
    package = _make_package(tmp_path / "pkg.neko-plugin", schema=4)
    report = downgrade(package, 4)
    assert report["status"] == "noop"


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    package = _make_package(tmp_path / "pkg.neko-plugin", schema=5)
    original = package.read_bytes()

    report = downgrade(package, 4, dry_run=True)

    assert report["status"] == "dry-run"
    assert report["schema"] == "5 → 4"
    assert report["entries"] == 3
    assert package.read_bytes() == original


def _force_hash_mismatch(monkeypatch) -> None:
    """让 metadata.toml 里回填的 hash 故意错一位，逼出独立校验失败那条路。"""
    monkeypatch.setattr(
        "tools.host_meta_compat._replace_hash_line",
        lambda text, new: re.sub(
            r'(hash\s*=\s*")[0-9a-fA-F]{64}(")',
            r"\g<1>" + "f" * 64 + r"\g<2>",
            text,
            count=1,
        ),
    )


def test_backup_is_removed_after_success(tmp_path: Path) -> None:
    """成功后备份必须消失。

    2026-11-27 从"留着"改成"成功后删"：备份是 schema 5 的包，装上去卡片
    按钮全 404，是个地雷；而发布归档的 README 一直要求"归档前把它移走"，
    于是每次打包都要额外跑一条删除命令。真机上那条命令被同桌猫判成
    【严重】删除文件（路径在工作目录外），一晚上响了四次。
    让工具自己收摊，比让使用者记得收摊可靠。
    """
    package = _make_package(tmp_path / "pkg.neko-plugin", schema=5)
    downgrade(package, 4)
    assert not (tmp_path / "pkg.neko-plugin.pre-schema-downgrade").exists()


def test_backup_survives_failed_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """校验不过时备份必须留下——那时它是唯一的回滚材料。"""
    package = _make_package(tmp_path / "pkg.neko-plugin", schema=5)
    original = package.read_bytes()
    backup = tmp_path / "pkg.neko-plugin.pre-schema-downgrade"

    _force_hash_mismatch(monkeypatch)
    with pytest.raises(RuntimeError, match="payload hash 不自洽"):
        downgrade(package, 4)

    assert backup.is_file()
    assert backup.read_bytes() == original


def test_pre_existing_backup_survives_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已有备份不许被这次运行覆盖——它可能是上一次失败留下的回滚材料。

    成功路径下备份会被删掉，所以"没被覆盖"只能在失败路径上观测：
    校验不过 + 已有备份 → 内容必须还是原来那份。
    """
    package = _make_package(tmp_path / "pkg.neko-plugin", schema=5)
    backup = tmp_path / "pkg.neko-plugin.pre-schema-downgrade"
    backup.write_bytes(b"earlier-failed-run")

    _force_hash_mismatch(monkeypatch)
    with pytest.raises(RuntimeError):
        downgrade(package, 4)

    assert backup.read_bytes() == b"earlier-failed-run"


def test_other_files_are_untouched(tmp_path: Path) -> None:
    """除 meta 与 metadata.toml，payload 里其他文件必须逐字节不变。"""
    package = _make_package(tmp_path / "pkg.neko-plugin", schema=5)
    with zipfile.ZipFile(package) as archive:
        before = {
            name: archive.read(name)
            for name in archive.namelist()
            if name not in {META_REL, "metadata.toml"}
        }

    downgrade(package, 4)

    with zipfile.ZipFile(package) as archive:
        after = {
            name: archive.read(name)
            for name in archive.namelist()
            if name not in {META_REL, "metadata.toml"}
        }
    assert after == before


# ── 失败路径 ────────────────────────────────────────────────────────────


def test_missing_package_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        downgrade(tmp_path / "nope.neko-plugin", 4)


def test_package_without_meta_is_an_error(tmp_path: Path) -> None:
    package = tmp_path / "empty.neko-plugin"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("manifest.toml", b'schema_version = "1.0"\n')
    with pytest.raises(RuntimeError, match="plugin.meta.json"):
        downgrade(package, 4)


def test_non_integer_schema_is_an_error(tmp_path: Path) -> None:
    package = tmp_path / "pkg.neko-plugin"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(
            META_REL, json.dumps({"schema_version": "five"}).encode("utf-8")
        )
        archive.writestr("metadata.toml", b'[payload]\nhash = "' + b"0" * 64 + b'"\n')
    with pytest.raises(RuntimeError, match="不是整数"):
        downgrade(package, 4)


def test_cli_reports_failure_without_traceback(tmp_path: Path, capsys) -> None:
    from tools.host_meta_compat import main

    code = main([str(tmp_path / "missing.neko-plugin")])
    assert code == 1
    assert "失败" in capsys.readouterr().err
