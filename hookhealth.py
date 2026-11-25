"""hook 健康度分类——**判定只有这一处**，日志与面板共用。

为什么抽出来：启动自检（``__init__._check_installed_hook_port``）和设置面板
都要回答"hook 现在好不好"。以前只有日志一个消费者，判定直接写在那个方法里；
面板是第二个消费者，如果它在旁边重写一遍，就成了**两套判据**——
诊断自己就成了第二套判据（B8 同类缺陷：诊断与常驻轮询对不上）。

所以判定在这里，纯函数、无副作用。**自动重写中继副本这个副作用留在调用方**：
分类只报告"过期了"，谁看见谁决定要不要修（启动时自动修，面板只报告）。

日志消息也在这里：调用方直接 ``logger.log(level, msg, *args)``，
不另做一张 state → 文案 的映射表（那又是第二处会漂移的东西）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .hooksetup import relay_matches, source_relay_path

# 状态码。顺序按严重度排，面板据此着色。
OK = "ok"
MISSING = "missing"              # 没装过
HTTP_STALE = "http_stale"        # 残留旧的 http 形态
NO_SCRIPT = "no_script"          # 解析不出中继脚本路径
SCRIPT_MISSING = "script_missing"  # 中继文件不在（被清理/手删）
OUTDATED = "outdated"            # 副本与插件里的版本不一致
NO_PORT = "no_port"              # 解析不出端口
PORT_MISMATCH = "port_mismatch"  # 端口与配置不一致

# 需要用户去调 hook_install 才能解决的状态。
NEEDS_INSTALL = frozenset({
    MISSING, HTTP_STALE, NO_SCRIPT, SCRIPT_MISSING, OUTDATED, NO_PORT,
    PORT_MISMATCH,
})


class HookHealth:
    """一次分类的结果。不可变；``log_method`` / ``log_message`` / ``log_args``
    是给调用方直接喂 logger 的，避免两处文案漂移。"""

    __slots__ = ("state", "tone", "title", "detail", "action", "log_method",
                 "log_message", "log_args")

    def __init__(self, *, state: str, tone: str, title: str, detail: str,
                 action: str | None, log_method: str, log_message: str,
                 log_args: tuple[Any, ...]) -> None:
        self.state = state
        self.tone = tone              # success / warning / danger
        self.title = title            # 面板标题（一句结论）
        self.detail = detail          # 面板第二行（说清后果与怎么办）
        self.action = action          # 面板上该显示哪个按钮（入口 id），None = 无
        self.log_method = log_method
        self.log_message = log_message
        self.log_args = log_args

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "tone": self.tone,
            "title": self.title,
            "detail": self.detail,
            "action": self.action,
        }


def classify_hook(installed: dict[str, Any] | None, *, target: str,
                  expected_port: int) -> HookHealth:
    """按已装配的那条 hook 判定健康度。**纯函数，不碰文件系统**。

    ``installed`` 是 ``hooksetup.installed_hook`` 的返回值（None = 没装过）。
    ``target`` 是 settings.json 路径，只用于日志/面板里告诉用户改的是哪个文件。
    ``expected_port`` 是审批服务实际在听的端口。

    中继脚本"在不在"由调用方查后通过 ``installed`` 的形状体现不出来，
    所以这里只判到"解析出了什么"；脚本是否落盘、副本是否过期，
    用 ``refine_with_relay`` 补——它同样只读。
    """
    if installed is None:
        return HookHealth(
            state=MISSING, tone="danger",
            title="审批功能不可用",
            detail="Claude Code 需要权限时不会通知本插件，你不会看到审批卡片。"
                   "点「装配」装上，重启 Claude Code 后生效。",
            action="hook_install",
            log_method="warning",
            log_message=(
                "Claude Code settings 里没有装配审批中继（{} 未找到我们的 hook）。"
                "PermissionRequest 不会打进来，卡片永远不会出现——"
                "请调一次 hook_install 入口装上，并重启 Claude Code"),
            log_args=(target,),
        )

    if installed.get("kind") == "http":
        return HookHealth(
            state=HTTP_STALE, tone="danger",
            title="残留一份过期的旧形态 hook",
            detail="它在插件没启动时可能拦住你所有的权限请求。"
                   "点「装配」替换成现在用的形态（会先备份）。",
            action="hook_install",
            log_method="error",
            log_message=(
                "{} 里残留一份过期的 http 形态审批 hook（不是现在用的 command hook）。"
                "它在插件没启动时可能拦住你所有的权限请求——"
                "请调一次 hook_install 入口，它会替换成 command hook 并先备份"),
            log_args=(target,),
        )

    script = installed.get("script")
    if not script:
        return HookHealth(
            state=NO_SCRIPT, tone="danger",
            title="hook 装上了但指向不明确",
            detail="解析不出中继脚本路径，权限请求打不进来。点「装配」重写这条 hook。",
            action="hook_install",
            log_method="error",
            log_message=(
                "无法从已装配的 hook 命令里解析出中继脚本路径（{}）。"
                "请重跑 hook_install 入口重写这条 hook"),
            log_args=(target,),
        )

    hooked = installed.get("port")
    if hooked is None:
        return HookHealth(
            state=NO_PORT, tone="danger",
            title="hook 装上了但端口不明确",
            detail="解析不出端口，权限请求打不进来。点「装配」重写这条 hook。",
            action="hook_install",
            log_method="error",
            log_message=(
                "无法从已装配的 hook 命令中解析端口（{}）。"
                "请重跑 hook_install 入口重写这条 hook"),
            log_args=(target,),
        )

    if int(hooked) != int(expected_port):
        return HookHealth(
            state=PORT_MISMATCH, tone="danger",
            title="端口不一致",
            detail="已装配的 hook 用的是旧端口 {}，插件现在听 {}。"
                   "权限请求会打到旧端口上，点「装配」重写。".format(hooked, expected_port),
            action="hook_install",
            log_method="error",
            log_message=(
                "端口不一致：配置里是 {}，但已装配的 hook 用的是 {}。"
                "PermissionRequest 会打到旧端口上，请重跑 hook_install 入口"
                "（它会替换成新端口，不会多出一条）"),
            log_args=(expected_port, hooked),
        )

    return HookHealth(
        state=OK, tone="success",
        title="审批功能就绪",
        detail="Claude Code 需要权限时会通知本插件，卡片会弹出来等你决定。",
        action=None,
        log_method="info",
        log_message="审批中继已装配且健康（{}，端口 {}）",
        log_args=(target, expected_port),
    )


def refine_with_relay(health: HookHealth, *, script: str | None,
                      plugin_dir: Any) -> HookHealth:
    """补两项只有落了盘才知道的判定：脚本在不在、副本是否过期。

    **只读**——不重写、不同步。同步是调用方的决定（启动时自动做，面板只报告）。
    传入的 ``health`` 已经是 ``classify_hook`` 的结果；这里只在它"基本没问题"
    的前提下追加判定，前面那些状态（没装/端口不一致/…）原样返回。
    """
    if health.state != OK or not script:
        return health

    if not Path(script).is_file():
        return HookHealth(
            state=SCRIPT_MISSING, tone="danger",
            title="中继文件已丢失",
            detail="{} 不在。权限请求打不通，卡片永远不会出现。"
                   "点「装配」重新放好这份文件并重写 hook。".format(script),
            action="hook_install",
            log_method="error",
            log_message=(
                "审批中继文件已丢失（{}）。它可能被宿主清理或手动删除了——"
                "PermissionRequest 会打不通，卡片永远不会出现。"
                "请调一次 hook_install 入口，它会重新放好这份文件并重写 hook"),
            log_args=(script,),
        )

    source = source_relay_path(plugin_dir)
    if source and Path(source).is_file() and not relay_matches(source, script):
        return HookHealth(
            state=OUTDATED, tone="warning",
            title="中继副本过期",
            detail="指向的 {} 与插件里的版本不一致。某些新字段可能不生效——"
                   "下次启动会自动重写，或现在点「装配」。".format(script),
            action="hook_install",
            log_method="warning",
            log_message=(
                "审批中继副本（{}）与插件里的版本不一致。"
                "某些新协议字段可能不生效；下次启动会自动重写，"
                "或调一次 hook_install 入口"),
            log_args=(script,),
        )

    return health
