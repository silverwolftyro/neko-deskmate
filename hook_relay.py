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

* 插件活着（``/health`` 探通**并核对身份**，1s 超时）→ 把请求原样转发给
  ``/hook/permission``，**校验应答形状**后打出来。
* 插件死了 / 端口上住的是别的程序 / 转发失败 / 插件回 503 / 应答不合法
  → 打印空对象 ``{}``，交还 Claude Code 原生流程。

**校验是 2026-11-22 A 批补的第二道门**：第一道门只管"插件在不在"，
管不了"端口上是不是插件"。插件没启动时若别的进程占了 48921，中继会把
完整命令发给它，还会把它返回的任意内容转交 Claude Code。所以转发前核对
``/health`` 的服务标识与 token，转发后用 ``_valid_decision`` 验形状——
任何一条不过都按"没有决定"处理，**宁可多弹一次原生权限框**。

**上游进程链**（规格 §11）：转发前顺手从本进程的父进程往上爬
``UPSTREAM_DEPTH`` 层，把 ``[{name, path}]`` 塞进请求体的 ``upstream`` 字段。
理由：真实链路常常是 DSH 的 agent → 起 pwsh → 起 ``claude -p`` → 它调 Bash，
而卡片原来只写「claude-code 请求执行」，用户看不到"是谁拉起的 claude"。
中继自己就在这条链上，所以由它爬比插件爬准。**只上报事实，不给结论**——
爬不到就什么都不加，由插件显示「上游未知」，绝不猜一个发起者。
纯 shell 层由插件在展示时滤掉，这里一律照实上报（滤的事不在这里做：
中继只负责"看到什么报什么"）。

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
# 往上爬几层。B5 从 3 提到 5：真实链路常常是
#   DSH agent → pwsh → node → claude -p → 又一层 shell → python(中继)
# 3 层就断在 `bash.exe ← bash.exe ← claude.exe`，看不到真正的发起者
# （真机实测）。多爬两层不花钱——纯 shell 层由插件在**展示时**滤掉，
# 爬过去才能找到更上面那个有意义的名字。
UPSTREAM_DEPTH = 5
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

# ── 应答校验（A 批 A3） ────────────────────────────────────────────────────
# 为什么必须校验：**端口上住的不一定是我们的插件**。插件没启动时若别的进程
# 占了 48921，中继会把完整命令发给它，还会把它返回的任意内容（包括一个
# 长得像 allow 的对象）原样转交 Claude Code——那是把审批权交给一个来路不明
# 的对端。所以转发之前先探活核对身份，转发之后先校验应答形状。
#
# 校验失败一律按"没有决定"处理：输出空对象，交还原生权限流程。
# 宁可多弹一次原生权限框，不能把一个非法应答当成批准。

# 探活应答里的服务标识。必须与 approval.py 的 HEALTH_SERVICE 一致。
HEALTH_SERVICE = "agent_deskmate"
# 探活应答里 token 的前缀。**只认前缀**：将来插件升级换了 token 尾巴，
# 老中继不会因此把插件当敌人（那是另一种静默失败）。
HEALTH_TOKEN_PREFIX = "deskmate-approval-"
# 官方 hook 事件名。应答里它必须对得上，否则说明对面不知道自己在回什么。
HOOK_EVENT_NAME = "PermissionRequest"
# decision.behavior 只准这两个值。
DECISION_BEHAVIOURS = ("allow", "deny")
# 读应答的上限（1 MiB）。一个合法的 decision 只有几百字节；大到这个程度
# 只能说明对面在塞别的东西，那就别读下去。
MAX_RESPONSE_BYTES = 1024 * 1024
# 探活应答的读取上限。它只有一个身份对象，几十字节就够了。
MAX_HEALTH_BYTES = 64 * 1024


def _emit(payload: dict) -> None:
    """往 stdout 写一行 JSON。写不出去就只能当没有决定。

    **输出必须保持纯 ASCII**（``ensure_ascii=True``，中文变 ``\\uXXXX``）。
    2026-11-24 真机 P0'：这里原来写 ``ensure_ascii=False``，而这个 stdout
    的编码是跟着 locale 走的（中文 Windows = cp936/GBK），中文被写成 GBK
    字节；Claude Code 按 UTF-8 读，拒绝理由就变成
    ``ͬ��è���û���`` 这种乱码。后果不是"难看"：模型看不懂
    "不要换写法重试、停下来等用户"，转而自己排查宿主和插件内部
    （真机实录见 0.6.0 验收反馈 §2.4）。

    **本地测试为什么一次都没抓到**：开发机 shell 里有
    ``PYTHONIOENCODING=utf-8``，子进程的 stdout 就是 UTF-8，两种写法
    输出逐字节相同。剥掉那个变量才复现——所以回归测试也必须剥掉它，
    否则是一条永远绿的假测试（见 tests/test_hooksetup.py 的编码用例）。

    纯 ASCII 是对编码不敏感的最强形状：UTF-8 / GBK / latin-1 解同一串
    字节得到同一个字符串，不依赖写方 locale、也不依赖读方假设。
    """
    sys.stdout.write(json.dumps(payload, ensure_ascii=True))
    sys.stdout.flush()


def _read_stdin() -> bytes:
    try:
        return sys.stdin.buffer.read()
    except (OSError, ValueError, AttributeError):
        return b""


def _warn(message: str) -> None:
    """往 stderr 写一行诊断。**不往 stdout 写**——stdout 是给宿主的应答。"""
    sys.stderr.write(f"deskmate-relay: {message}\n")
    sys.stderr.flush()


def _alive(port: str) -> bool:
    """探活，并核对对面**是不是自己这一版插件**（A 批 A3）。

    三个条件全中才算活：200、服务标识对、token 前缀对。
    任何异常都算"不在"——不细分，中继不需要知道原因。

    只答 ``{"ok": true}`` 是不够的：那证明不了对面不会把命令送给别人。
    """
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{HEALTH_PATH}", timeout=HEALTH_TIMEOUT
        ) as response:
            if response.status != 200:
                return False
            raw = response.read(MAX_HEALTH_BYTES)
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001  探活失败的原因不影响下一步：交还原生流程
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("service") != HEALTH_SERVICE:
        _warn(f"端口 {port} 上的服务不是同桌猫（{payload.get('service')!r}），不转发")
        return False
    token = payload.get("token")
    if not (isinstance(token, str) and token.startswith(HEALTH_TOKEN_PREFIX)):
        _warn(f"端口 {port} 上的服务版本对不上（token={token!r}），不转发")
        return False
    return True


def _valid_decision(body: bytes) -> dict | None:
    """校验插件的应答是不是一个合法的 decision。不合法返回 None。

    三条硬条件（A 批 A3）：是 JSON 对象、``hookEventName`` 对得上、
    ``decision.behavior`` 是 allow / deny 之一。少一条就整个丢掉。

    **不看内容、只看形状**：我们不该因为"这个 allow 长得不像"就二次判断
    该不该放行——那是替用户做决定。形状不对只说明对面不是能回话的那一方，
    那时正确的做法是当它没回答。
    """
    if len(body) > MAX_RESPONSE_BYTES:
        _warn(f"应答 {len(body)} 字节超过上限 {MAX_RESPONSE_BYTES}，丢弃")
        return None
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        _warn("应答不是合法 JSON，丢弃")
        return None
    if not isinstance(payload, dict):
        _warn("应答不是 JSON 对象，丢弃")
        return None
    output = payload.get("hookSpecificOutput")
    if not isinstance(output, dict):
        _warn("应答里没有 hookSpecificOutput，丢弃")
        return None
    if output.get("hookEventName") != HOOK_EVENT_NAME:
        _warn(f"应答的 hookEventName 是 {output.get('hookEventName')!r}，丢弃")
        return None
    decision = output.get("decision")
    if not isinstance(decision, dict):
        _warn("应答里没有 decision，丢弃")
        return None
    behavior = decision.get("behavior")
    if behavior not in DECISION_BEHAVIOURS:
        _warn(f"应答的 behavior 是 {behavior!r}，丢弃")
        return None
    return payload


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
    """转发给插件，并把它的应答交给 Claude Code——**先验形状，再转交**。

    HTTP 200 + 合法 decision → 原样透传。
    其它一切（503 = 插件没有决定 / 连接断 / 超时 / 应答不合法）→ 空对象，
    交还原生流程。原样透传的只能是通过 ``_valid_decision`` 的那一份。
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
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError:
        # 503 = 到期无人决定。这不是错误，是插件明确的"我没有决定"，
        # 交还原生流程正是规格要的行为。
        _emit({})
        return
    except Exception as exc:  # noqa: BLE001  转发失败不能把 hook 变成阻断性错误
        _warn(f"转发失败（{type(exc).__name__}）：{exc}")
        _emit({})
        return

    decision = _valid_decision(raw)
    if decision is None:
        # 已经打过 warning 了。**不把原始应答交出去**——那正是要防的那件事。
        _emit({})
        return
    # 同 ``_emit``：**纯 ASCII**。拒绝理由整段走这一行出去，
    # 写成 locale 编码（中文 Windows = GBK）的话，Claude Code 按 UTF-8
    # 读出来就是乱码，而拒绝理由是"这次不让跑"的唯一载体——
    # 对方看不懂它就会自己瞎猜（0.6.0 真机：连发 6 次编码实验 +
    # 去读宿主和插件内部目录）。写 ASCII 与 locale 彻底无关。
    sys.stdout.write(json.dumps(decision, ensure_ascii=True))
    sys.stdout.flush()


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
