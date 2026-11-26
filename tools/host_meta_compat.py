"""把打好的 `.neko-plugin` 里的 `plugin.meta.json` 降到宿主认得的老 schema。

## 为什么需要它（2026-11-24 实测，别删这段注释）

`neko-plugin build`（源码树 11-20 版，CLI 0.2.0）产出的 meta 是
`schema_version = 5`，而本机宿主是 **Steam 2026-10-31 版，只认 4**。
宿主每次加载都打这条 warning：

    packaged plugin metadata schema mismatch, falling back to manifest:
      path=...\\agent_deskmate\\plugin.meta.json, found=5, expected=4

**要命的不是这条 warning，是它后面的连锁**：宿主退回去读 manifest
（`plugin.toml`），而 `plugin.toml` 没有任何 entry 声明，于是
`registry_service._packaged_entries_preview()` 拿到空表 → `state.plugins`
里没有 entries → 卡片动作走 `ui_query_service._entry_ids_from_meta()`
返回空集 → **所有卡片按钮 404**：

    UI action 'approval_decide' is not a plugin entry   (reason=no_plugin_entries)

离线复现脚本（不需要 GUI）：
`N.E.K.O-agent_help\\09-脚本与探针\\复现-卡片按钮404-schema判定-2026-11-24.py`
它把宿主的期望 schema 临时改成 4，跑宿主自己的 `read_packaged_metadata`：
schema 5 → 被拒、0 个 entry；只把 schema_version 改成 4 → 接受、11 个 entry。

## 为什么降到 4 对本品是无损的

schema 5 相对 4 的唯一新增作用是「让 `handlers` 也覆盖 `__routers__` 条目」
（见 `plugin/core/packaged_metadata.py` 的注释与官方文档
`core-plugin-contract.md` 的 Router Contract）。**本品没有 router**——
`handlers` 的键全是 `agent_deskmate.<entry>` 与
`agent_deskmate:plugin_entry:<entry>` 形式，没有 router 派生的条目。
所以 4 与 5 对本品是同一份信息。

⚠️ **如果哪天本品加了 `__routers__`，这个降级必须重新评估**：
那时 schema 4 会让宿主在首次启动时重扫并改写 meta（多一次启动开销），
而更重要的是"该不该为一个旧宿主放弃新契约"要重新拍板。

## 什么时候该撤掉它

宿主升级到认 schema 5 的版本之后。判据很干脆：**宿主日志不再出现
`found=5, expected=4`**。那时把 `--target-schema` 改成 5（或者干脆不用本脚本），
否则会反过来把新宿主不认识的旧结构塞给它。

## 它改了什么，以及为什么不会破坏包

`metadata.toml` 的 `[payload].hash` 覆盖 payload 下所有文件，
而 `plugin.meta.json` 是 payload 内容之一（`build.py` 的注释明说
"plugin.meta.json is package content and must participate in"）。
所以本脚本改完 meta **必须重算 payload hash**，否则宿主会判定包被篡改。
算法与 `neko_plugin_cli.core.archive_utils.compute_archive_payload_hash` 一致：
按 NFC 规范化的 posix 相对路径排序，每条目贡献
`path + NUL + content + NUL`。

只改 `schema_version` 一个整数，其余字节原样保留（正则替换，不重新序列化），
所以 entries / handlers / entry_methods / source_sha256 全部不动。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
import zipfile
from pathlib import Path
from typing import Any

PAYLOAD_PREFIX = "payload"
METADATA_ENTRY = "metadata.toml"
META_SUFFIX = "plugin.meta.json"
_SCHEMA_RE = re.compile(r'("schema_version"\s*:\s*)(\d+)')


def _canonical(name: str) -> str:
    """与 normalize_archive_key 一致：NFC + 正斜杠。"""
    return unicodedata.normalize("NFC", name.replace("\\", "/"))


def _payload_hash(archive: zipfile.ZipFile) -> str:
    """复刻 compute_archive_payload_hash：只吃 payload/ 下的文件条目。"""
    import hashlib

    digest = hashlib.sha256()
    entries: list[tuple[str, str]] = []
    for name in archive.namelist():
        parts = name.split("/")
        if len(parts) < 2 or parts[0] != PAYLOAD_PREFIX:
            continue
        if name.endswith("/"):
            continue
        entries.append((name, _canonical("/".join(parts[1:]))))
    for archive_name, canonical in sorted(entries, key=lambda item: item[1]):
        digest.update(canonical.encode("utf-8"))
        digest.update(b"\0")
        digest.update(archive.read(archive_name))
        digest.update(b"\0")
    return digest.hexdigest()


def _find_meta(archive: zipfile.ZipFile) -> str | None:
    for name in archive.namelist():
        if name.endswith(META_SUFFIX) and name.split("/")[0] == PAYLOAD_PREFIX:
            return name
    return None


def _replace_hash_line(text: str, new_hash: str) -> str:
    pattern = re.compile(r'(hash\s*=\s*")([0-9a-fA-F]{64})(")')
    updated, count = pattern.subn(r"\g<1>" + new_hash + r"\g<3>", text, count=1)
    if count != 1:
        raise RuntimeError("metadata.toml 里找不到可替换的 [payload].hash")
    return updated


def downgrade(package: Path, target: int, *, dry_run: bool = False) -> dict[str, Any]:
    """把包内 meta 的 schema_version 降到 *target*，并重算 payload hash。

    返回一份报告；`dry_run=True` 时只读不写。
    """
    if not package.is_file():
        raise FileNotFoundError(f"找不到包：{package}")

    with zipfile.ZipFile(package) as archive:
        meta_name = _find_meta(archive)
        if meta_name is None:
            raise RuntimeError(f"包里没有 {PAYLOAD_PREFIX}/**/{META_SUFFIX}")
        raw_meta = archive.read(meta_name).decode("utf-8")
        before = json.loads(raw_meta)
        old_schema = before.get("schema_version")
        if not isinstance(old_schema, int):
            raise RuntimeError(f"meta 的 schema_version 不是整数：{old_schema!r}")
        if old_schema == target:
            return {"status": "noop", "meta": meta_name, "schema": old_schema}

        updated_meta, count = _SCHEMA_RE.subn(
            lambda m: m.group(1) + str(target), raw_meta, count=1
        )
        if count != 1:
            raise RuntimeError("meta 里找不到 schema_version 字段")
        after = json.loads(updated_meta)

        # 不变式：除了 schema_version，一个字节都不能变。
        drifted = {
            key: (before.get(key), after.get(key))
            for key in set(before) | set(after)
            if key != "schema_version" and before.get(key) != after.get(key)
        }
        if drifted:
            raise RuntimeError(f"降级会改动 schema_version 以外的字段：{sorted(drifted)}")

        entries = len(after.get("entries") or [])
        handlers = len(after.get("handlers") or {})
        methods = len(after.get("entry_methods") or {})

        # 重建 zip：保持条目顺序与压缩方式，只换 meta 与 metadata.toml。
        infos = archive.infolist()
        payload: list[tuple[zipfile.ZipInfo, bytes]] = []
        metadata_name: str | None = None
        for info in infos:
            data = archive.read(info.filename)
            if info.filename == meta_name:
                data = updated_meta.encode("utf-8")
            elif info.filename == METADATA_ENTRY:
                metadata_name = info.filename
            payload.append((info, data))

        new_hash = None
        if metadata_name is not None:
            # 先用新内容算一遍 hash，再回填 metadata.toml。
            import io

            staging_buffer = io.BytesIO()
            with zipfile.ZipFile(staging_buffer, "w") as staging:
                for info, data in payload:
                    staging.writestr(info, data)
            with zipfile.ZipFile(staging_buffer, "r") as staging:
                new_hash = _payload_hash(staging)

            payload = [
                (
                    info,
                    _replace_hash_line(
                        data.decode("utf-8"), new_hash
                    ).encode("utf-8")
                    if info.filename == metadata_name
                    else data,
                )
                for info, data in payload
            ]

        if dry_run:
            return {
                "status": "dry-run",
                "meta": meta_name,
                "schema": f"{old_schema} → {target}",
                "entries": entries,
                "handlers": handlers,
                "entry_methods": methods,
                "new_payload_hash": new_hash,
            }

    # ── 到这里 archive 已关闭。Windows 上打不开的文件没法替换，
    # 所以写盘必须放在 with 块外面。 ──
    backup = package.with_name(package.name + ".pre-schema-downgrade")
    if not backup.exists():
        backup.write_bytes(package.read_bytes())

    tmp = package.with_name(package.name + ".tmp")
    with zipfile.ZipFile(tmp, "w") as out:
        for info, data in payload:
            out.writestr(info, data)
    tmp.replace(package)

    # 独立校验：重开包，确认 hash 自洽、meta 仍是那个 meta。
    with zipfile.ZipFile(package) as check:
        final_meta = json.loads(check.read(meta_name).decode("utf-8"))
        actual = _payload_hash(check)
        declared = None
        if metadata_name is not None:
            text = check.read(metadata_name).decode("utf-8")
            match = re.search(r'hash\s*=\s*"([0-9a-fA-F]{64})"', text)
            declared = match.group(1) if match else None
        if declared is not None and declared.lower() != actual:
            raise RuntimeError(
                f"payload hash 不自洽：metadata.toml={declared[:16]} 实际={actual[:16]}"
            )
        if final_meta.get("schema_version") != target:
            raise RuntimeError("降级后 schema_version 不对")

    # ── 成功后删掉备份 ──
    #
    # 2026-11-27：备份从"留着"改成"成功后删"。它是 schema 5 的包，
    # 装上去卡片按钮全 404，是个地雷；而发布归档的 README 一直要求
    # "归档前把它移走或删掉"——于是每次打包都要额外跑一条删除命令。
    #
    # 真机上那条命令被同桌猫判成【严重】删除文件（路径在工作目录外），
    # 一晚上响了四次，用户原话：「又来，一点小事，搞得这么严重」。
    # 让工具自己收摊，比让使用者记得收摊可靠。
    #
    # **只在独立校验全过之后删**：校验失败会抛异常，备份因此留下，
    # 那时候它是唯一的回滚材料。
    try:
        backup.unlink()
    except OSError:
        pass

    return {
        "status": "downgraded",
        "package": str(package),
        "meta": meta_name,
        "schema": f"{old_schema} → {target}",
        "entries": entries,
        "handlers": handlers,
        "entry_methods": methods,
        "payload_hash": new_hash,
        "backup": str(backup),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("package", type=Path, help=".neko-plugin 包路径")
    parser.add_argument(
        "--target-schema",
        type=int,
        default=4,
        help="目标 schema 版本，默认 4（本机 2026-10-31 宿主的实测期望）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写文件")
    args = parser.parse_args(argv)

    try:
        report = downgrade(args.package, args.target_schema, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001
        print(f"失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    for key, value in report.items():
        print(f"{key}: {value}")
    if report["status"] == "noop":
        print("（已经是目标 schema，没有改动）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
