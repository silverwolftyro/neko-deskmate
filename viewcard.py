"""AgentHUD 视图（``ctx.create_view``）的最小验证探针。

## 为什么单独一个模块

``create_view`` 是 2026-11-27 才接上的 SDK 能力（宿主 11-27 树新增），
这个插件此前只会 ``create_card``（聊天里的审批卡）。视图走的是 **AgentHUD
的插件内容页签**，不是聊天消息——弹出/替换/关闭的语义都和卡片不一样，
所以第一次接必须单独探，不能直接改审批卡那条路。

HTML 与 actions 的拼装放在这里而不是入口里，理由同 ``approval.build_card_html``：
入口要能被单测打到，而 HTML 是纯字符串变换。

## 命名：为什么叫 ``build_probe_result_html`` 而不叫 ``build_result_html``

``approval.py`` 已经有一个 ``build_result_html(note)``（位置参数，一句话的
结果卡），``__init__.py`` 早就 import 了它。第一版图省事也叫这个名字，
结果 **import 把它遮住了**——``_note_button_unavailable()`` 那句
「这个按钮暂时不可用」改成调用我的关键字专用版本，一个 TypeError 被
``except Exception`` 吞掉，表现是「按钮点不动时那句人话不出现了」。
是 ``test_card_advice.py::test_host_error_never_reaches_the_screen``
当场抓住的。

**教训：往 ``__init__.py`` 加 import 前，先 grep 一遍同名符号。**
这个文件 import 了二十多个模块，同名不报错、只是静默换掉一个函数。

## 三条硬规矩（与审批卡同一套）

1. **禁脚本**：HTML 跑在禁止脚本的 iframe 里，``onclick`` / ``<script>`` 一律不执行。
   按钮只能写 ``<button data-neko-action="按钮id">``，父页面负责绑事件。
2. **动态内容必须转义**：探针编号是从 ``uuid4`` 来的，理论安全，但这个模块
   将来会被真数据复用——``html.escape`` 是默认动作，不是可选优化。
3. **视图不进聊天、不进 AI 上下文**：所以探针卡里不写路径、不写命令原文。
   要那些去看聊天里的审批卡，那条路有它自己的披露规则。
"""

from __future__ import annotations

import html
import uuid
from typing import Any

# 入口 id。**三处必须完全一致**：``actions`` 的 ``entry``、``@ui.action(id=)``、
# ``@plugin_entry(id=)``。对不上就是按钮点了没反应（宿主按 id 找入口）。
ENTRY_VIEW_PROBE = "view_probe"
ENTRY_VIEW_DECIDE = "view_probe_decide"
ENTRY_VIEW_CLOSE = "view_probe_close"

# 按钮 id。只在本插件的 HTML 与 actions 字典之间对应，不要求等于入口 id。
ACTION_ALLOW = "probe_allow"
ACTION_DENY = "probe_deny"
ACTION_CLOSE = "probe_close"

VIEW_CSS = """
section { padding: 12px; font-size: 13px; line-height: 1.6; }
h3 { margin: 0 0 8px; font-size: 14px; }
p { margin: 0 0 10px; }
.meta { color: #888; font-size: 12px; margin: 0 0 12px; }
.row { display: flex; gap: 8px; }
button { padding: 6px 14px; cursor: pointer; }
.result { font-weight: 600; }
.echo { color: #666; font-size: 12px; white-space: pre-wrap; }
"""


def new_probe_id() -> str:
    """探针编号。8 位十六进制——够辨认，又不长到念不出来。

    刻意不用会话 id 或路径：视图虽然不进聊天，但用户会截图，
    而截图里的每个标识都会被他拿去排查。
    """
    return uuid.uuid4().hex[:8]


def build_view_html(probe_id: str) -> str:
    """假审批卡的初始样子。"""
    return (
        "<section>"
        "<h3>同桌猫 · AgentHUD 视图探针</h3>"
        "<p>这是一张<strong>假</strong>审批卡，用来验证 "
        "<code>ctx.create_view()</code>：视图能不能弹出、按钮回调能不能"
        "拿到 args 并改卡片。它不对应任何真实请求，点哪个都不会影响 "
        "Claude Code。</p>"
        f'<p class="meta">探针编号 {html.escape(probe_id)}</p>'
        '<div class="row">'
        f'<button data-neko-action="{ACTION_ALLOW}">允许</button>'
        f'<button data-neko-action="{ACTION_DENY}">拒绝</button>'
        f'<button data-neko-action="{ACTION_CLOSE}">关掉</button>'
        "</div>"
        "</section>"
    )


def build_view_actions(probe_id: str) -> dict[str, dict[str, Any]]:
    """三个按钮 → 两个入口，用 ``args`` 区分。

    ``probe_id`` 进 args 是**刻意的**：用户要验的第三条就是
    「按钮回调能不能拿到 args」。只传一个 ``decision`` 的话，
    回传成功了也看不出是参数机制在起作用。
    """
    return {
        ACTION_ALLOW: {"entry": ENTRY_VIEW_DECIDE,
                       "args": {"probe_id": probe_id, "decision": "allow"}},
        ACTION_DENY: {"entry": ENTRY_VIEW_DECIDE,
                      "args": {"probe_id": probe_id, "decision": "deny"}},
        ACTION_CLOSE: {"entry": ENTRY_VIEW_CLOSE,
                       "args": {"probe_id": probe_id}},
    }


def build_probe_result_html(*, probe_id: str, decision: str,
                      view_id: str, lanlan_name: str) -> str:
    """点完之后的样子。**把收到的参数原样回显**——这是探针的全部意义。"""
    label = "已允许" if decision == "allow" else "已拒绝"
    echo = (
        f"decision = {decision}\n"
        f"probe_id = {probe_id}\n"
        f"view_id  = {view_id}\n"
        f"lanlan   = {lanlan_name or '（宿主没给，走主服务路由）'}"
    )
    return (
        "<section>"
        f'<h3 class="result">{label}（探针 {html.escape(probe_id)}）</h3>'
        "<p>按钮回调拿到了上面的参数，并用 "
        "<code>get_view()</code> 把这张卡改成了现在这样。</p>"
        f'<p class="echo">{html.escape(echo)}</p>'
        "</section>"
    )


def build_closed_html(probe_id: str) -> str:
    """用户主动关掉之后留在原处的一行。

    宿主语义：``close()`` 结束的是**这次显示实例**，视图从页签上消失。
    这里回一行字是因为 ``update`` 在 close 之后仍然提交得动（只是不再
    重新打开）——探针要把这个区别显示出来，而不是靠猜。
    """
    return (
        "<section>"
        f"<h3>已关掉（探针 {html.escape(probe_id)}）</h3>"
        "<p>视图的这次显示结束了。下面的字你看不到——"
        "它只是证明 close 之后的 update 不会把视图重新拉出来。</p>"
        "</section>"
    )
