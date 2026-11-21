"""同桌猫审批中继：Claude Code 的 ``PermissionRequest`` command hook。

**为什么是 command hook 而不是 http hook**（2026-11-21 真机实测后改）：

http hook 配 ``onFailure:"block"`` 时，插件没启动 = 连接被拒 = 宿主按 block
处理 = **用户所有权限请求全部卡死**。5a 实测确认了这一点：没人替用户装 hook，
而一旦按老方案装上，插件没开时的代价是彻底不能干活。

command hook 的失败语义正好是我们要的：命令退出码 0 且不给 decision →
**非阻断**，Claude Code 回退到它自己的原生权限弹窗（官方文档：command/http
默认 timeout 600s；非 2xx / 连接失败 / 超时默认非阻断，除非显式
``onFailure:"block"``）。所以这里刻意**不配** onFailure。

两种结局，都 exit 0：

* 插件活着（``/health`` 探通，1s 超时）→ 把请求原样转发给 ``/hook/permission``，
  再把插件的响应体原样打印出来。
* 插件死了 / 转发失败 / 插件回 503（= 插件没有决定）→ 打印空对象 ``{}``，
  交还 Claude Code 原生流程。

**上游进程链**（规格 §11）：转发前顺手从本进程的父进程往上爬 2–3 层，
把 ``[{name, path}]`` 塞进请求体的 ``upstream`` 字段。理由：真实链路常常是
DSH 的 agent → 起 pwsh → 起 ``claude -p`` → 它调 Bash，而卡片原来只写
「claude-code 请求执行」，用户看不到"是谁拉起的 claude"。中继自己就在这条链上，
所以由它爬比插件爬准。**只上报事实，不给结论**——爬不到就什么都不加，
由插件显示「上游未知」，绝不猜一个发起者。

用法::

    python hook_relay.py <port>          # 正常模式，从 stdin 读 hook 载荷
    python hook_relay.py --selfcheck     # 自检：只验证解释器和本文件能跑

自检模式刻意不碰网络：装配时用它验证"这条 command 真的能跑起来"，
省得装完才发现 python 不在 PATH 上，然后每次权限请求都报一次错。
"""

from __future__ import annotations

import ctypes
import json
import sys
import urllib.error
import urllib.request
from typing import Any

# 探活路径。必须与 approval.py 里的 HEALTH_PATH 一致——两边各写一份是因为
# 中继是独立脚本，不能 import 插件包（hook 触发时的 sys.path 里没有它）。
# 一致性由 test_hooksetup 里的一条测试盯着。
HEALTH_PATH = "/health"

# 探活超时。1s：插件在本机，活的话几十毫秒就应；慢了说明它卡住了，
# 那时交还原生流程比让用户干等好。
HEALTH_TIMEOUT = 1.0
# 转发超时。必须 ≥ 插件的到期时间（默认 570s），否则我们还没到期中继先断。
# 官方 command hook 默认 timeout 就是 600s，这里对齐。
FORWARD_TIMEOUT = 600.0

# ── 上游进程链（规格 §11：只展示，不参与任何决策）────────────────────────
# 往上爬几层。3 层是因为再往上通常是终端/资源管理器，对"这是我自己开的吗"
# 这个问题没有增量；也够覆盖 DSH → pwsh → claude 这条真实链路。
UPSTREAM_DEPTH = 3
# 快照里最多读多少个进程。工具枚举是全量的，理论上成千条；
# 我们只按 ppid 查几次，但给个上限防异常环境。
MAX_PROCESSES = 4096
# 进程链写进请求体的键名。**刻意带前缀**：这是中继与插件之间的私有协议，
# 而请求体本身是 Claude Code 的——将来它要是加了一个叫 ``upstream`` 的字段，
# 不带前缀的我们就会把它的内容当成自己爬的进程链显示给用户，那是错归因。
UPSTREAM_FIELD = "deskmate_upstream"

# Toolhelp32 的常量与结构。**零依赖**：只用 ctypes 调 kernel32，
# 不引 psutil / wmi 之类的东西（hook 在任何机器上都得能跑）。
_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_MAX_PATH = 260
# (HANDLE)-1。快照失败时返回的就是它；``c_void_p`` 会把 -1 报成这个值。
_INVALID_HANDLE = (1 << 64) - 1


class _ProcessEntry32(ctypes.Structure):
    """PROCESSENTRY32W。字段顺序与宽度必须和 Win32 一致，错一位就全错。"""

    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * _MAX_PATH),
    ]


def _kernel32() -> Any:
    """拿 kernel32，并把两个返回句柄的函数的返回类型说清楚。

    **不设 restype 的话默认按 ``c_int`` 截断成 32 位**，而句柄是 64 位的：
    截断之后 INVALID_HANDLE_VALUE 有可能变成 0，也可能把一个合法句柄
    误判成无效。这种事只会偶发，查起来能查一天，所以一开始就写对。
    """
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.OpenProcess.restype = ctypes.c_void_p
    return kernel32


def _process_table() -> dict[int, tuple[int, str]]:
    """``{pid: (ppid, exe 名)}``。任何失败都返回空表——上游是可选信息。"""
    kernel32 = _kernel32()
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or int(snapshot) == _INVALID_HANDLE:
        return {}
    table: dict[int, tuple[int, str]] = {}
    try:
        entry = _ProcessEntry32()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok and len(table) < MAX_PROCESSES:
            table[int(entry.th32ProcessID)] = (
                int(entry.th32ParentProcessID), entry.szExeFile)
            entry = _ProcessEntry32()
            entry.dwSize = ctypes.sizeof(_ProcessEntry32)
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return table


def _image_path(pid: int) -> str:
    """进程可执行文件的完整路径。拿不到就返回空串。"""
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(_MAX_PATH + 1)
        size = ctypes.c_ulong(_MAX_PATH + 1)
        # 2 == QueryFullProcessImageNameW（问路径，不问设备路径）
        if not kernel32.QueryFullProcessImageNameW(handle, 2, buffer,
                                                  ctypes.byref(size)):
            # 某些受保护的进程只给得出查询 0 的短形式，退一步再试。
            size = ctypes.c_ulong(_MAX_PATH + 1)
            kernel32.QueryFullProcessImageNameW(handle, 0, buffer,
                                                ctypes.byref(size))
        return buffer.value or ""
    finally:
        kernel32.CloseHandle(handle)


def _upstream() -> list[dict[str, str]]:
    """从父进程往上爬，返回 ``[{name, path}]``（从近到远）。

    任何一步失败都**就地停**，把已经拿到的返回——半条链比没有好，
    而编一段完整的比半条危险得多。非 Windows 直接返回空表。
    """
    if not hasattr(ctypes, "windll"):
        return []
    try:
        table = _process_table()
    except Exception:  # noqa: BLE001  归因是可选信息，任何异常都不该冒出去
        return []
    if not table:
        return []

    kernel32 = _kernel32()
    pid = int(kernel32.GetCurrentProcessId())
    seen = {pid}
    chain: list[dict[str, str]] = []
    for _ in range(UPSTREAM_DEPTH):
        row = table.get(pid)
        if row is None:
            break
        ppid = row[0]
        if ppid in seen or ppid <= 0:
            break
        seen.add(ppid)
        # **名字要查 ppid 那一行**：``table[pid]`` 里存的是 pid 自己的名字，
        # 拿它当父进程的名字会把"python.exe 的父亲是 python.exe"报上去
        # （第一版就这么错的，真机一看就露馅）。
        parent = table.get(ppid)
        chain.append({"name": (parent[1] if parent else "") or "",
                      "path": _image_path(ppid)})
        pid = ppid
    return chain

# 探活路径。必须与 approval.py 里的 HEALTH_PATH 一致——两边各写一份是因为
# 中继是独立脚本，不能 import 插件包（hook 触发时的 sys.path 里没有它）。
# 一致性由 test_hooksetup 里的一条测试盯着。
HEALTH_PATH = "/health"

# 探活超时。1s：插件在本机，活的话几十毫秒就应；慢了说明它卡住了，
# 那时交还原生流程比让用户干等好。
HEALTH_TIMEOUT = 1.0
# 转发超时。必须 ≥ 插件的到期时间（默认 570s），否则我们还没到期中继先断。
# 官方 command hook 默认 timeout 就是 600s，这里对齐。
FORWARD_TIMEOUT = 600.0


def _emit(payload: dict) -> None:
    """往 stdout 写一行 JSON。写不出去就只能当没有决定。"""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()


def _read_stdin() -> bytes:
    try:
        return sys.stdin.buffer.read()
    except (OSError, ValueError, AttributeError):
        return b""


def _alive(port: str) -> bool:
    """探活。任何异常都算"不在"——不细分，中继不需要知道原因。"""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{HEALTH_PATH}", timeout=HEALTH_TIMEOUT
        ) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001  探活失败的原因不影响下一步：交还原生流程
        return False


def _with_upstream(body: bytes) -> bytes:
    """把上游进程链塞进请求体。**塞不进去就原样转发**——归因是可选信息。

    体不是 JSON、或者带上 ``upstream`` 之后序列化失败，都按原样发：
    为了一个展示字段把整个审批请求搞坏，是本末倒置。
    """
    chain = _upstream()
    if not chain:
        return body
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return body
    if not isinstance(payload, dict):
        return body
    payload[UPSTREAM_FIELD] = chain
    try:
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return body


def _forward(port: str, body: bytes) -> None:
    """转发给插件，并把它的应答原样交给 Claude Code。

    HTTP 200 = 插件给了决定，原样透传（它的 body 就是官方 decision schema）。
    其它一切（503 = 插件没有决定 / 连接断 / 超时）→ 空对象，交还原生流程。
    """
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/hook/permission",
        data=_with_upstream(body),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=FORWARD_TIMEOUT) as response:
            if response.status != 200:
                _emit({})
                return
            sys.stdout.write(response.read().decode("utf-8", "replace"))
            sys.stdout.flush()
    except urllib.error.HTTPError:
        # 503 = 到期无人决定。这不是错误，是插件明确的"我没有决定"，
        # 交还原生流程正是规格要的行为。
        _emit({})
    except Exception:  # noqa: BLE001  转发失败不能把 hook 变成阻断性错误
        _emit({})


def main(argv: list[str]) -> int:
    if "--selfcheck" in argv:
        _emit({"ok": True, "script": __file__})
        return 0

    # ``--marker <值>`` 是装配方写进来的识别凭据（Windows 的 cmd 不把 ``#``
    # 当注释，所以不能用注释）。它对本脚本没有任何意义，连值一起丢掉。
    args: list[str] = []
    skip_next = False
    for item in argv:
        if skip_next:
            skip_next = False
            continue
        if item == "--marker":
            skip_next = True
            continue
        args.append(item)

    positional = [item for item in args if not item.startswith("--")]
    if not positional:
        # 没给端口：什么都不做，交还原生流程。不猜端口。
        _emit({})
        return 0

    port = positional[0]
    body = _read_stdin()
    if not body.strip():
        _emit({})
        return 0
    if not _alive(port):
        _emit({})
        return 0
    _forward(port, body)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
