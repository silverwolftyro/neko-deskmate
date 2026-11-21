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

用法::

    python hook_relay.py <port>          # 正常模式，从 stdin 读 hook 载荷
    python hook_relay.py --selfcheck     # 自检：只验证解释器和本文件能跑

自检模式刻意不碰网络：装配时用它验证"这条 command 真的能跑起来"，
省得装完才发现 python 不在 PATH 上，然后每次权限请求都报一次错。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

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


def _forward(port: str, body: bytes) -> None:
    """转发给插件，并把它的应答原样交给 Claude Code。

    HTTP 200 = 插件给了决定，原样透传（它的 body 就是官方 decision schema）。
    其它一切（503 = 插件没有决定 / 连接断 / 超时）→ 空对象，交还原生流程。
    """
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/hook/permission",
        data=body,
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
