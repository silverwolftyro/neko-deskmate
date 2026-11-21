"""审批卡片在「模型建议」这条链路上的回归测试。

**为什么单独立一个文件**：2026-11-22 真机跑出一个致命 bug——
配好 key + 授权后，第一次真实请求，卡片创建抛 ``KeyError('ok')``，
卡片根本没弹出来。用户看到的是"插件装了但完全没反应"。

根因是三处词汇不一致：

* ``model_advice.py`` 的 ``STATE_OK = "ok"``（调用结果码）
* ``approval.py`` 的 ``build_card_html`` 只认 ``"ready"``（展示状态）
* ``approval.py`` 的 ``ADVICE_TEXT`` 里没有 ``"ok"`` 这个键

``_advice_for_card`` 把第一套词直接传给了只认第二套词的地方，
模型一成功就 ``ADVICE_TEXT["ok"]`` → KeyError → 整张卡片崩掉。

**当时的测试为什么没抓住**：所有建议相关的测试都在测
``build_card_html`` 这个纯函数（喂字符串进去看 HTML），
没有一条测试走过 ``advisor.suggest() → _advice_for_card() →
build_card_html() → ctx.create_card()`` 这条完整链路。
翻译错了当然看不出来。

所以这里补的正是**链路测试**，外加一条针对未知状态的 property 式测试。
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from plugin.plugins.agent_deskmate.approval import (
    ADVICE_STATE_FALLBACK,
    ADVICE_STATE_READY,
    ADVICE_STATES,
    ApprovalGateway,
    ApprovalRequest,
    PendingApproval,
    build_card_html,
)
from plugin.plugins.agent_deskmate.model_advice import Advice, ModelAdvisor

PLUGIN_DIR = Path(__file__).resolve().parents[1]


class ListLogger:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def _log(self, level: str, message: str, *args: object) -> None:
        try:
            rendered = str(message).format(*args) if args else str(message)
        except (IndexError, KeyError, ValueError):
            rendered = f"{message} | {args!r}"
        self.lines.append((level, rendered))

    def info(self, message: str, *args: object) -> None:
        self._log("info", message, *args)

    def warning(self, message: str, *args: object) -> None:
        self._log("warning", message, *args)

    def error(self, message: str, *args: object) -> None:
        self._log("error", message, *args)


class FakeCard:
    def __init__(self, card_id: str) -> None:
        self.id = card_id


class FakeCtx:
    """只实现 _create_approval_card 用到的那个方法。"""

    def __init__(self) -> None:
        self.cards: list[dict[str, Any]] = []

    async def create_card(self, **kwargs: Any) -> FakeCard:
        self.cards.append(kwargs)
        return FakeCard(f"card-{len(self.cards)}")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _gateway(**kwargs: Any) -> ApprovalGateway:
    gateway = ApprovalGateway(ListLogger(), port=_free_port(),
                              timeout_seconds=30.0, **kwargs)
    assert gateway.start()
    return gateway


def _pending(gateway: ApprovalGateway, rid: str = "r1") -> PendingApproval:
    request = ApprovalRequest(session_id="sess-abc", request_id=rid,
                              tool_name="Bash", tool_summary="Bash: ls -la",
                              working_directory=r"D:\workspace")
    record = PendingApproval(request)
    with gateway._lock:  # noqa: SLF001  测试直接摆一条记录进去
        gateway._records[record.key] = record  # noqa: SLF001
    return record


def _plugin(gateway: ApprovalGateway, advisor: ModelAdvisor | None,
            logger: ListLogger, ctx: FakeCtx):
    """造一个只装了本链路需要的属性的插件实例。

    刻意绕开 ``__init__``：那会拉起整个 SDK（文件日志、宿主上下文），
    而这里要测的只是"建议 → 卡片"这一条路。属性名就是被测对象本身，
    耦合是故意的。
    """
    from plugin.plugins.agent_deskmate import AgentDeskmatePlugin

    plugin = object.__new__(AgentDeskmatePlugin)
    plugin._approval = gateway
    plugin._advisor = advisor
    plugin._advice_pool = None
    plugin.logger = logger
    plugin.ctx = ctx
    return plugin


def _live_advisor(reply: str) -> tuple[ModelAdvisor, Any]:
    """起一个假模型服务，返回指向它的 advisor（协议形状走 openai）。"""
    recorder = {"bodies": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:  # noqa: A002
            return

        def do_POST(self) -> None:  # noqa: N802  标准库命名
            length = int(self.headers.get("Content-Length") or 0)
            recorder["bodies"].append(json.loads(self.rfile.read(length)))
            body = json.dumps({"choices": [{"message": {"content": reply}}]},
                              ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = int(server.server_address[1])
    advisor = ModelAdvisor(enabled=True, protocol="openai", api_key="k",
                           base_url=f"http://127.0.0.1:{port}",
                           model="deepseek-chat")
    return advisor, server


# ── 回归 1：完整链路，模型成功给出建议 ───────────────────────────────────

def test_successful_advice_survives_the_whole_card_path() -> None:
    """真机故障的回归：模型成功 → 卡片必须出得来，且带建议。

    走完整链路：真 advisor（假 HTTP）→ _advice_for_card →
    build_card_html → ctx.create_card。任何一步抛异常这里都会红。
    """
    advisor, server = _live_advisor("建议：放行\n理由：只读命令，无副作用")
    gateway = _gateway(ai_judgement=True, has_api_key=True)
    try:
        gateway.grant_authorization(("ai_judgement", "auto_decide"))
        logger, ctx = ListLogger(), FakeCtx()
        plugin = _plugin(gateway, advisor, logger, ctx)
        pending = _pending(gateway)

        asyncio.run(plugin._create_approval_card(pending))  # noqa: SLF001

        assert len(ctx.cards) == 1, "卡片没建出来——这就是真机上那个故障"
        html = ctx.cards[0]["html"]
        # 建议正文真的在卡片里，而且是**人话**，不是机器 token
        assert "建议：倾向放行" in html
        assert "理由：只读命令，无副作用" in html
        # 同族第二例的守护：卡片上不许出现 allow / deny / unsure 这种 token
        for token in ("建议：allow", "建议：deny", "建议：unsure"):
            assert token not in html, f"卡片泄了机器词 {token}"
        # 依据引用的是真实字段，不是编的
        assert "Bash" in html
        assert r"D:\workspace" in html
        assert "deepseek-chat" in html
        # 三个按钮一个都不能少
        for action in ("deskmate_approve", "deskmate_deny", "deskmate_explain"):
            assert f'data-neko-action="{action}"' in html
        # 建议块在按钮上方（规格 §16.1）
        assert html.index("建议：倾向放行") < html.index(
            'data-neko-action="deskmate_approve"')
        # 没有 warning：这条路不该有任何降级
        assert not [line for line in logger.lines if line[0] == "warning"], logger.lines
    finally:
        server.shutdown()
        server.server_close()
        gateway.stop()


def test_advice_state_reaching_the_card_is_a_known_one() -> None:
    """_advice_for_card 交出来的 state 必须是卡片认得的那个词。

    这条直接钉死"两套词表"：翻译点产出物必须落在 ADVICE_STATES 里。
    """
    advisor, server = _live_advisor("建议：别放行\n理由：会影响工作区")
    gateway = _gateway(ai_judgement=True, has_api_key=True)
    try:
        gateway.grant_authorization(("ai_judgement", "auto_decide"))
        logger, ctx = ListLogger(), FakeCtx()
        plugin = _plugin(gateway, advisor, logger, ctx)
        pending = _pending(gateway)

        advice = asyncio.run(plugin._advice_for_card(pending))  # noqa: SLF001

        assert advice["state"] == ADVICE_STATE_READY
        assert advice["state"] in ADVICE_STATES
        # 建议取值已被翻译成人话，且不是机器 token
        assert advice["suggestion"] == "倾向别放行"
        assert advice["suggestion"] not in ("allow", "deny", "unsure")
        assert advice["reason"] == "会影响工作区"
    finally:
        server.shutdown()
        server.server_close()
        gateway.stop()


@pytest.mark.parametrize("outcome_state", ["no_key", "auth_failed", "timeout",
                                           "invalid", "bad_response",
                                           "disabled", "unauthorized", "error"])
def test_every_failure_outcome_still_produces_a_card(
        outcome_state: str) -> None:
    """每一种失败档位，卡片都必须出得来。

    建议挂了不能带走卡片——卡片是核心功能，建议是锦上添花。
    """
    gateway = _gateway(ai_judgement=True, has_api_key=True)
    try:
        gateway.grant_authorization(("ai_judgement", "auto_decide"))
        logger, ctx = ListLogger(), FakeCtx()

        def broken(**_kwargs: Any) -> Advice:
            return Advice(outcome_state, detail="注入的失败")

        advisor = ModelAdvisor(enabled=True, api_key="k", base_url="http://x",
                               model="m")
        advisor.suggest = broken  # type: ignore[method-assign]
        plugin = _plugin(gateway, advisor, logger, ctx)
        pending = _pending(gateway)

        asyncio.run(plugin._create_approval_card(pending))  # noqa: SLF001

        assert len(ctx.cards) == 1, f"{outcome_state} 档位下卡片没出来"
        html = ctx.cards[0]["html"]
        assert "同桌猫 · 审批请求" in html
        assert 'data-neko-action="deskmate_approve"' in html
        # 失败档位不编造建议
        assert "建议：" not in html
    finally:
        gateway.stop()


def test_advisor_exploding_still_produces_a_card() -> None:
    """advisor 抛异常也不能带走卡片。"""
    gateway = _gateway(ai_judgement=True, has_api_key=True)
    try:
        gateway.grant_authorization(("ai_judgement", "auto_decide"))
        logger, ctx = ListLogger(), FakeCtx()

        def exploding(**_kwargs: Any) -> Advice:
            raise RuntimeError("模型客户端炸了")

        advisor = ModelAdvisor(enabled=True, api_key="k", base_url="http://x",
                               model="m")
        advisor.suggest = exploding  # type: ignore[method-assign]
        plugin = _plugin(gateway, advisor, logger, ctx)

        asyncio.run(plugin._create_approval_card(_pending(gateway)))  # noqa: SLF001

        assert len(ctx.cards) == 1
        assert "同桌猫 · 审批请求" in ctx.cards[0]["html"]
    finally:
        gateway.stop()


def test_zero_key_still_produces_a_card() -> None:
    """零 key（默认态）下建议块降级，卡片照常。"""
    gateway = _gateway()
    try:
        logger, ctx = ListLogger(), FakeCtx()
        plugin = _plugin(gateway, None, logger, ctx)

        asyncio.run(plugin._create_approval_card(_pending(gateway)))  # noqa: SLF001

        assert len(ctx.cards) == 1
        html = ctx.cards[0]["html"]
        assert "安全兜底与模型无关" in html
        assert "建议：" not in html
    finally:
        gateway.stop()


# ── 回归 2：未知 state 绝不能抛异常（property 式） ────────────────────────

UNKNOWN_STATES = [
    "ok",                     # ← 真机事故就是这一个：调用结果码被当成展示状态
    "OK",
    "ready ",
    " ready",
    "",
    "不存在的状态",
    "no-key",
    "noKey",
    "auth_failed",
    "timeout",
    "bad_response",
    "unknown",
    "None",
    "null",
    "0",
    "ready,disabled",
    "disabled;drop",
]


@pytest.mark.parametrize("state", UNKNOWN_STATES)
def test_unknown_advice_state_never_raises(state: str) -> None:
    """随便喂什么 state 给 build_card_html，都不许抛异常。

    property 式：不枚举"我们认为会出现的坏值"，而是把一整类可疑输入
    （大小写变体、前后空格、空串、中文、另一种拼写、注入串）全扫一遍。
    真正的判据只有一条：**它永远返回一段能渲染的 HTML**。
    """
    html = build_card_html(subject="claude-code", tool_summary="Bash: ls",
                           session_id="s", working_directory=None,
                           timeout_seconds=570, advice_state=state)
    assert html.startswith("<section>")
    assert "同桌猫 · 审批请求" in html
    # 按钮永远在——卡片的核心功能不依赖建议
    assert 'data-neko-action="deskmate_approve"' in html
    assert 'data-neko-action="deskmate_deny"' in html
    assert 'data-neko-action="deskmate_explain"' in html


@pytest.mark.parametrize("state", UNKNOWN_STATES)
def test_unknown_advice_state_falls_back_and_logs(state: str) -> None:
    """未知状态 → 用 disabled 的陈述句 + 记一条 warning。"""
    logger = ListLogger()
    html = build_card_html(subject="claude-code", tool_summary="Bash: ls",
                           session_id="sess-xyz", working_directory=None,
                           timeout_seconds=570, advice_state=state,
                           logger=logger)
    # 走兜底文案，不编建议
    assert "安全兜底与模型无关" in html
    assert "建议：" not in html
    if state in ADVICE_STATES:
        # 合法状态不该触发 warning
        assert not [line for line in logger.lines if line[0] == "warning"]
    else:
        warnings = [line for line in logger.lines if line[0] == "warning"]
        assert warnings, f"{state!r} 是未知状态，却没记 warning"
        assert "未知的建议状态" in warnings[0][1]
        assert repr(state) in warnings[0][1]


def test_known_states_do_not_warn() -> None:
    """合法状态一律不告警——否则 warning 会变成噪音，真告警就被淹了。"""
    for state in sorted(ADVICE_STATES):
        logger = ListLogger()
        build_card_html(subject="c", tool_summary="Bash: ls", session_id="s",
                        working_directory=None, timeout_seconds=570,
                        advice_state=state, logger=logger)
        assert not [line for line in logger.lines if line[0] == "warning"], (
            f"合法状态 {state!r} 触发了 warning")


def test_every_known_state_renders_something() -> None:
    """ADVICE_STATES 里每个状态都必须渲染出非空的建议块。"""
    for state in sorted(ADVICE_STATES):
        html = build_card_html(subject="c", tool_summary="Bash: ls",
                               session_id="s", working_directory=None,
                               timeout_seconds=570, advice_state=state,
                               suggestion="放行", reason="只读", basis="无副作用")
        if state == ADVICE_STATE_READY:
            assert "建议：放行" in html
        else:
            assert "安全兜底与模型无关" in html


def test_fallback_state_is_itself_known() -> None:
    """兜底态必须合法——否则未知状态会掉进另一个未知状态里。"""
    assert ADVICE_STATE_FALLBACK in ADVICE_STATES
    assert ADVICE_STATE_READY in ADVICE_STATES
