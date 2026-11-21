"""审批 hook 的装配 / 卸载：把 command hook 写进 ``~/.claude/settings.json``。

**这一节存在的原因**（2026-11-21 真机实测）：``hooks.json`` 一直只是躺在插件
目录里，没有任何东西会把它装进 Claude Code。用户装完插件，卡片永远不来——
不是插件坏了，是 hook 从来没接上。

所以装配必须是插件自己的一个入口，而且必须满足三条：

1. **改哪个文件说清楚**：只动 ``~/.claude/settings.json``，先备份再写，
   备份路径在返回值里给全。
2. **只碰自己的那一条**：别人的 hook 原样留着；我们自己的条目按标记识别，
   重复装配是幂等的（改端口后重装 = 替换，不是追加）。
3. **装不上就别装**：装配前先用 ``--selfcheck`` 真跑一遍中继脚本，
   解释器不在 PATH / 脚本跑不起来 → 直接报错，不写 settings。
   装一条跑不起来的 command hook，等于让用户每次权限请求都看一次报错。

**安全形状**：中继是 command hook，插件没开时输出空对象 → Claude Code 回退
原生权限弹窗。刻意不用 http hook + ``onFailure:"block"``——那会在插件没启动时
把用户所有权限请求卡死（详见 ``hook_relay.py`` 的模块文档）。

本模块**不 import 任何 SDK**：纯标准库，才能在没宿主的环境里单测。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

# Claude Code 的 PermissionRequest 事件名。
EVENT_NAME = "PermissionRequest"
# 我们那条 hook 的标记。卸载与重复装配都靠它识别"这是我的"。
RELAY_MARKER = "neko-deskmate-approval-relay"
RELAY_SCRIPT_NAME = "hook_relay.py"
# 老形状（5a 之前）的 http hook：直接挂回环地址 + onFailure:"block"。
# 真机实测证明那个形状在插件没开时会把该目录下所有权限请求卡死，
# 所以装配时必须把它认出来并替换掉，不能留着一个危险的旧 hook 再加一条新的。
# 判据 = 路径是 /hook/permission **且** 主机是回环——别人的 hook
# （哪怕也在 127.0.0.1 上）路径不同，不会被误认。
LEGACY_URL_PATH = "/hook/permission"
LEGACY_URL_HOSTS = ("127.0.0.1", "localhost")
# 自检用的解释器。刻意用 PATH 上的 python 而不是 sys.executable——
# 宿主进程可能是 Nuitka/mypyc 冻结的，那个"解释器"跑不了独立脚本。
RELAY_INTERPRETER = "python"
SELFCHECK_TIMEOUT = 20.0

HOOK_TYPE = "command"
# 装进 settings 的 hook 超时。**显式写出来，不赌官方默认值**：
# 我们的到期是 570s（hook 600s 前留 30s），中继的转发超时也是 600s。
# 万一 command hook 的默认值是 60s，570s 的审批窗口会先被砍断，
# 症状是"卡片还没点，Claude Code 那边已经报超时了"——那种故障极难归因。
HOOK_TIMEOUT_SECONDS = 600


def settings_path() -> Path:
    """Claude Code 的用户级 settings。只在这里算，别处不许拼路径。"""
    return Path.home() / ".claude" / "settings.json"


def relay_script_path(plugin_dir: Any) -> Path:
    """中继脚本的绝对路径。

    刻意**总是绝对路径**：hook 触发时的 cwd 是 Claude Code 的工作目录，
    不是插件目录。万一宿主传进来的 plugin_dir 是相对路径（真机实测见过
    开发模式下传相对路径的情况），相对路径装进去就是一个永远找不到脚本的 hook，
    而症状是"每次权限请求都失败"——所以这里兜底解析成绝对路径。
    """
    root = Path(plugin_dir).expanduser()
    if not root.is_absolute():
        root = (Path.cwd() / root).resolve()
    return root / RELAY_SCRIPT_NAME


def relay_command(plugin_dir: Any, port: int) -> str:
    """装配进 settings 的那条 command。

    脚本路径用绝对路径：hook 触发时的 cwd 是 Claude Code 的工作目录，
    不是插件目录，相对路径一定找不到。

    末尾的标记注释是**识别用的唯一凭据**：卸载与重复装配都靠它认"这是我的"，
    不按端口认（端口会变），也不按脚本路径认（插件目录可能被搬走）。
    中继脚本会忽略这个多余的参数。
    """
    return (f'{RELAY_INTERPRETER} "{relay_script_path(plugin_dir)}" {int(port)}'
            f"  # {RELAY_MARKER}")


def is_ours(command: Any) -> bool:
    """这条 command hook 是不是我们装的。按标记认，不按端口认——端口会变。"""
    return isinstance(command, str) and RELAY_MARKER in command


def is_our_hook(hook: Any) -> bool:
    """这一个 hook 条目是不是我们的——**两种形状都要认**。

    * command hook：命令里带标记；
    * 老的 http hook：URL 指向本机 + ``/hook/permission``。

    第二种必须认：2026-11-21 真机验收时用户手写进项目 settings 的就是它，
    只认标记的话装配会**追加**第二条，结果两个 hook 同时跑，
    而那个带 ``onFailure:"block"`` 的旧 hook 正是会在插件没开时卡死一切的形状。
    """
    if not isinstance(hook, dict):
        return False
    if is_ours(hook.get("command")):
        return True
    url = hook.get("url")
    if hook.get("type") == "http" and isinstance(url, str):
        return (LEGACY_URL_PATH in url
                and any(host in url for host in LEGACY_URL_HOSTS))
    return False


def read_settings(path: Any) -> tuple[dict[str, Any], str]:
    """读 settings。返回 ``(数据, 错误原因)``；出错时数据是空 dict。

    读不动就**拒绝继续**：把一份解析不了的 JSON 覆盖掉，比不装 hook 严重得多。
    """
    target = Path(path)
    if not target.is_file():
        return {}, ""
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        return {}, f"读不了 {target}（{type(exc).__name__}）：{exc}"
    except UnicodeDecodeError as exc:
        return {}, f"{target} 不是 UTF-8 文本（{exc}），不敢改"
    if not raw.strip():
        return {}, ""
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return {}, f"{target} 不是合法 JSON（{exc}）。请先手工修好它，再装 hook"
    if not isinstance(data, dict):
        return {}, f"{target} 顶层不是对象而是 {type(data).__name__}，不敢改"
    return data, ""


def _entries_of(data: dict[str, Any]) -> list[Any]:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return []
    entries = hooks.get(EVENT_NAME)
    return list(entries) if isinstance(entries, list) else []


def installed_hook(path: Any, plugin_dir: Any) -> dict[str, Any] | None:
    """已装配的那条 hook 的详情；没装过返回 None。

    **两种形状都认**（``is_our_hook``）：command 带标记 / 老的 http 指向本机。
    老形状必须认：用户机器上可能残留一份过期的 http hook，只认 command 的话
    启动核对会报"没有装配审批中继"——既不清它也不提它，用户完全不知情。

    返回 ``{"kind": "command"|"http", "port": int|None}``。
    """
    data, error = read_settings(path)
    if error:
        return None
    script = str(relay_script_path(plugin_dir))
    for entry in _entries_of(data):
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks") or []:
            if not isinstance(hook, dict) or not is_our_hook(hook):
                continue
            kind = "http" if hook.get("type") == "http" else "command"
            port = _port_of_hook(hook, script)
            return {"kind": kind, "port": port}
    return None


def _port_of_hook(hook: dict[str, Any], script: str) -> int | None:
    """从一条 hook 里取出端口。command 从命令参数取，http 从 URL 取。"""
    if hook.get("type") == "http":
        url = hook.get("url")
        if not isinstance(url, str):
            return None
        # http://127.0.0.1:48921/hook/permission → 冒号后、斜杠前那段
        tail = url.split("://", 1)[-1]
        host_port = tail.split("/", 1)[0]
        _, _, port_text = host_port.rpartition(":")
        try:
            return int(port_text)
        except ValueError:
            return None
    command = hook.get("command")
    if not isinstance(command, str) or script not in command:
        return None
    # 脚本路径在命令里是带引号的，所以切完之后第一个 token 可能是 "。
    # 引号一律当空白处理，再取第一个纯数字 token；
    # 标记注释里的 token 不是数字，自然被跳过。
    for token in command.rsplit(script, 1)[-1].replace('"', " ").split():
        if token.isdigit():
            return int(token)
    return None


def installed_port(path: Any, plugin_dir: Any) -> int | None:
    """已装配的那条 hook 用的端口；没装过返回 None。"""
    found = installed_hook(path, plugin_dir)
    return found["port"] if found else None


def selfcheck(plugin_dir: Any) -> tuple[bool, str]:
    """真跑一次中继脚本，确认这条 command 在本机能跑起来。

    不碰网络（``--selfcheck`` 只打印 ok）。解释器不在 PATH、脚本有语法错，
    都会在这里暴露——装配前拦住，比装完让用户每次权限请求都看报错好。
    """
    script = relay_script_path(plugin_dir)
    if not script.is_file():
        return False, f"中继脚本不在：{script}"
    try:
        proc = subprocess.run(
            [RELAY_INTERPRETER, str(script), "--selfcheck"],
            capture_output=True, text=True, timeout=SELFCHECK_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        return False, (f"找不到解释器 {RELAY_INTERPRETER!r}（PATH 里没有 python）。"
                       "Claude Code 的 hook 会在一个干净的环境里跑，"
                       "请先让 python 在 PATH 上可用")
    except subprocess.TimeoutExpired:
        return False, f"中继脚本自检超过 {SELFCHECK_TIMEOUT:g}s 没返回"
    except OSError as exc:
        return False, f"自检跑不起来（{type(exc).__name__}）：{exc}"
    if proc.returncode != 0:
        return False, (f"中继脚本自检退出码 {proc.returncode}"
                       f"（stderr: {proc.stderr.strip()[:200]}）")
    try:
        payload = json.loads(proc.stdout.strip() or "{}")
    except ValueError:
        return False, f"自检输出不是 JSON：{proc.stdout.strip()[:200]}"
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return False, f"自检输出缺少 ok=true：{proc.stdout.strip()[:200]}"
    return True, ""


def _backup(path: Path, backup_dir: Any) -> Path | None:
    """写之前备份。返回备份路径；没东西可备份返回 None。"""
    if not path.is_file():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = Path(backup_dir) / f"settings.json.{stamp}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    except OSError:
        return None
    return target


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    """先写临时文件再替换。半份 settings.json 会让 Claude Code 起不来。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".neko-tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    os.replace(temp, path)


def _strip_ours(path: Path, backup_dir: Any) -> tuple[int, Path | None, str]:
    """从一份 settings 里摘掉我们自己的 hook。返回 ``(摘掉几条, 备份, 错误)``。"""
    data, error = read_settings(path)
    if error:
        return 0, None, error
    hooks = data.get("hooks")
    if not isinstance(hooks, dict) or EVENT_NAME not in hooks:
        return 0, None, ""
    kept: list[Any] = []
    removed = 0
    for entry in _entries_of(data):
        if isinstance(entry, dict) and _is_our_entry(entry):
            removed += 1
            continue
        kept.append(entry)
    if removed == 0:
        return 0, None, ""
    backup = _backup(path, backup_dir) if backup_dir is not None else None
    if kept:
        hooks[EVENT_NAME] = kept
    else:
        del hooks[EVENT_NAME]
        if not hooks:
            data.pop("hooks", None)
    try:
        _write_atomic(path, data)
    except OSError as exc:
        return 0, backup, f"写不进 {path}（{type(exc).__name__}）：{exc}"
    return removed, backup, ""


def install_hook(plugin_dir: Any, port: int, *, settings: Any = None,
                 backup_dir: Any = None,
                 extra_locations: list[Any] | None = None) -> dict[str, Any]:
    """把中继 hook 装进 Claude Code 的 settings.json。

    幂等：已装过就**替换**（端口改了照样正确），不会追加出第二条。

    会顺带清掉别处的旧 hook：真机验收时用户手写进项目 settings 的是
    ``http + onFailure:"block"`` 形状，那个形状在插件没开时会把该目录下所有
    权限请求卡死。只认标记的话这里会追加第二条、两个 hook 同时跑——
    所以老形状也要认（``is_our_hook``），认出来就摘掉。

    自检不过就不写任何文件——返回值里 ``ok`` 为 False 并带原因。
    """
    path = Path(settings) if settings is not None else settings_path()
    plugin = Path(plugin_dir)
    ok, why = selfcheck(plugin)
    if not ok:
        return {"ok": False, "action": "install", "settings_file": str(path),
                "reason": why}

    data, error = read_settings(path)
    if error:
        return {"ok": False, "action": "install", "settings_file": str(path),
                "reason": error}

    backup = _backup(path, backup_dir) if backup_dir is not None else None
    command = relay_command(plugin, port)

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        data["hooks"] = hooks
    before = len(_entries_of(data))
    entries = [item for item in _entries_of(data)
               if not (isinstance(item, dict) and _is_our_entry(item))]
    replaced = before - len(entries)
    entries.append({"matcher": "",
                    "hooks": [{"type": HOOK_TYPE, "command": command,
                               "timeout": HOOK_TIMEOUT_SECONDS}]})
    hooks[EVENT_NAME] = entries

    try:
        _write_atomic(path, data)
    except OSError as exc:
        return {"ok": False, "action": "install", "settings_file": str(path),
                "reason": f"写不进 {path}（{type(exc).__name__}）：{exc}",
                "backup": str(backup) if backup else None}

    # 别处的旧 hook：默认**不扫**。用户 2026-11-22 明确要求只写
    # ~/.claude/settings.json，项目级 .claude/settings.json 不许动——
    # 那可能是用户按项目自己维护的文件，插件没有理由替它改。
    # 需要清别处时由调用方显式传 extra_locations。
    cleaned: list[dict[str, Any]] = []
    elsewhere = [Path(item) for item in (extra_locations or [])]
    for other in elsewhere:
        removed, other_backup, other_error = _strip_ours(other, backup_dir)
        cleaned.append({
            "settings_file": str(other),
            "removed": removed,
            "backup": str(other_backup) if other_backup else None,
            "error": other_error,
        })

    return {
        "ok": True, "action": "install", "settings_file": str(path),
        "backup": str(backup) if backup else None,
        "command": command, "port": int(port),
        "entries_for_event": len(entries),
        "replaced_here": replaced,
        "cleaned_elsewhere": cleaned,
        "note": ("已写入。重启 Claude Code 后生效；插件没启动时中继会输出空对象，"
                 "Claude Code 回退原生权限弹窗，不会卡住你。"),
    }


def _is_our_entry(entry: dict[str, Any]) -> bool:
    """这一整个 entry 是不是我们装的。

    认两种形状（command 带标记 / 老的 http 指向本机 hook/permission），
    **不按脚本路径认**：用户可能把插件目录搬了地方，按路径认就会认不出
    自己装过的那条，然后卸载留下孤儿 hook。
    """
    for hook in entry.get("hooks") or []:
        if is_our_hook(hook):
            return True
    return RELAY_MARKER in json.dumps(entry, ensure_ascii=False)


def uninstall_hook(*, settings: Any = None,
              backup_dir: Any = None) -> dict[str, Any]:
    """把中继 hook 从 settings.json 里摘掉。别人的 hook 一律不动。"""
    path = Path(settings) if settings is not None else settings_path()
    data, error = read_settings(path)
    if error:
        return {"ok": False, "action": "uninstall", "settings_file": str(path),
                "reason": error}

    hooks = data.get("hooks")
    if not isinstance(hooks, dict) or EVENT_NAME not in hooks:
        return {"ok": True, "action": "uninstall", "settings_file": str(path),
                "removed": 0, "note": "本来就没装，什么都不用做"}

    kept: list[Any] = []
    removed = 0
    for entry in _entries_of(data):
        if isinstance(entry, dict) and _is_our_entry(entry):
            removed += 1
            continue
        kept.append(entry)

    if removed == 0:
        return {"ok": True, "action": "uninstall", "settings_file": str(path),
                "removed": 0, "note": "没找到我们装的那条，可能已被手工删除"}

    backup = _backup(path, backup_dir) if backup_dir is not None else None
    if kept:
        hooks[EVENT_NAME] = kept
    else:
        del hooks[EVENT_NAME]
        if not hooks:
            data.pop("hooks", None)

    try:
        _write_atomic(path, data)
    except OSError as exc:
        return {"ok": False, "action": "uninstall", "settings_file": str(path),
                "reason": f"写不进 {path}（{type(exc).__name__}）：{exc}",
                "backup": str(backup) if backup else None}

    return {
        "ok": True, "action": "uninstall", "settings_file": str(path),
        "removed": removed, "backup": str(backup) if backup else None,
        "note": "已摘掉。Claude Code 恢复原生权限流程",
    }
