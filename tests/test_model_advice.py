"""model_advice.py 模型建议单测（5b 最小版）。

核心契约（规格 §8，全是硬约束）：

1. **只建议，永不代决**——产出只有「建议 + 理由」，没有 allow/deny 回填路径；
2. **永不 auto-deny**——建议的三个取值里没有"拒绝"这一说给回填用；
3. **只发四样东西**——工具名、命令、工作目录、风险档。不发会话历史；
4. **失败分档**——没配 key / 401 / 超时 / 不合法，文案各不相同；
5. **不抛异常**——调用方在审批 timer 里跑它，抛异常会带走整轮建卡。

用假 HTTP server 验协义，用 monkeypatch 验失败档，不起真实网络请求。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from plugin.plugins.agent_deskmate.model_advice import (
    MAX_TOKENS,
    REQUEST_TIMEOUT,
    STATE_AUTH_FAILED,
    STATE_BAD_RESPONSE,
    STATE_DISABLED,
    STATE_INVALID,
    STATE_NO_KEY,
    STATE_OK,
    STATE_TIMEOUT,
    SUGGEST_ALLOW,
    SUGGEST_DENY,
    SUGGEST_UNSURE,
    Advice,
    ModelAdvisor,
    _parse,
    build_prompt,
)


class _Recorder:
    """记录客户端发过来的请求，供断言"只发了该发的"。"""

    def __init__(self) -> None:
        self.bodies: list[dict] = []
        self.headers: list[dict] = []
        self.paths: list[str] = []


def _serve(respond, recorder: _Recorder) -> tuple[ThreadingHTTPServer, int]:
    """起一个只记录请求、按 ``respond`` 应答的假模型服务。

    ``respond`` 收 ``(handler, status)`` 并负责把响应写回 socket。
    用回调而不是 handler 子类：每种失败档位只要写一个几行的 lambda，
    不用为一个 401 复制一整个类。
    """

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:  # noqa: A002
            return

        def do_POST(self) -> None:  # noqa: N802  标准库命名
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            recorder.paths.append(self.path)
            recorder.headers.append({k.lower(): v for k, v in self.headers.items()})
            try:
                recorder.bodies.append(json.loads(raw))
            except ValueError:
                recorder.bodies.append({})
            respond(self, self.path)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, int(server.server_address[1])


def _json_responder(payload: Any, *, status: int = 200,
                    raw_body: bytes | None = None) -> Any:
    """按 JSON 应答；传 raw_body 就原样吐字节（用于造非法响应）。"""
    body = (raw_body if raw_body is not None
            else json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def respond(handler: Any, _path: str) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    return respond


# ── prompt 只含四样东西 ───────────────────────────────────────────────────

def test_prompt_sends_only_the_four_agreed_fields() -> None:
    """规格 §8：只发工具名、命令、工作目录、风险档。

    多一样都算越界——会话历史、初始 prompt、转录内容都不在该发之列。
    """
    prompt = build_prompt(tool_name="Bash", command="rm -rf build/",
                          cwd=r"D:\workspace", risk="critical")
    for expected in ("Bash", "rm -rf build/", r"D:\workspace", "critical"):
        assert expected in prompt
    for forbidden in ("session", "transcript", "prompt 内容", "历史"):
        assert forbidden not in prompt


def test_prompt_clips_a_huge_command() -> None:
    """命令可能几百 KB。截断再发——既省 token 也缩小泄露面。"""
    prompt = build_prompt(tool_name="Bash", command="x" * 100000,
                          cwd="", risk="notice")
    assert len(prompt) < 2000


def test_advice_state_texts_never_solicit_or_imply_the_gate_is_off() -> None:
    """每档降级文案都是陈述句：不索取 key，不暗示闸门不工作。"""
    from plugin.plugins.agent_deskmate.model_advice import ADVICE_STATE_TEXT

    for state, text in ADVICE_STATE_TEXT.items():
        if not text:
            continue
        assert "填写" not in text, f"{state} 出现了索取式措辞"
        assert "即可" not in text, f"{state} 出现了索取式措辞"
        assert "安全兜底与模型无关" in text, f"{state} 没说清兜底不依赖模型"


# ── 解析 ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("建议：放行\n理由：只读命令，无副作用", SUGGEST_ALLOW),
    ("建议：别放行\n理由：会删掉整个工作区", SUGGEST_DENY),
    ("建议：不确定\n理由：看不出意图", SUGGEST_UNSURE),
    ("**建议：放行**\n**理由：** 只读", SUGGEST_ALLOW),
    ("suggestion: allow\nreason: read-only", SUGGEST_ALLOW),
    ("建议：放行\n理由：第一句\n建议：别放行\n理由：改口了", SUGGEST_ALLOW),
])
def test_parse_handles_the_expected_shapes(text: str, expected: str) -> None:
    suggestion, _ = _parse(text)
    assert suggestion == expected


def test_parse_falls_back_to_unsure_not_to_an_error() -> None:
    """答非所问时给"不确定"，不是报错——那比事实更近，也不会像插件坏了。"""
    suggestion, reason = _parse("我不知道该说什么")
    assert suggestion == SUGGEST_UNSURE
    assert reason == ""


def test_parse_empty_text() -> None:
    assert _parse("") == (SUGGEST_UNSURE, "")


# ── 配置探针（不发请求） ──────────────────────────────────────────────────

def test_disabled_advisor_never_calls_out() -> None:
    advisor = ModelAdvisor(enabled=False, api_key="k", base_url="http://x",
                           model="m")
    assert advisor.configured() is False
    assert advisor.suggest(tool_name="Bash", command="ls", cwd="",
                           risk="notice").state == STATE_DISABLED


@pytest.mark.parametrize("kwargs,missing", [
    ({"api_key": "", "base_url": "http://x", "model": "m"}, "api_key"),
    ({"api_key": "k", "base_url": "", "model": "m"}, "base_url"),
    ({"api_key": "k", "base_url": "http://x", "model": ""}, "model"),
])
def test_missing_field_is_named_specifically(kwargs: dict, missing: str) -> None:
    """缺哪一样就说哪一样——用户看到"没配 key"会去配，看到"调用失败"只会懵。"""
    advisor = ModelAdvisor(enabled=True, **kwargs)
    assert advisor.configured() is False
    assert advisor.missing() == f"未配置 {missing}"
    advice = advisor.suggest(tool_name="Bash", command="ls", cwd="", risk="notice")
    assert advice.state == STATE_NO_KEY
    assert advice.detail == f"未配置 {missing}"


def test_unknown_proocol_falls_back_to_openai() -> None:
    """协议名写错时按 openai 形状发，不要静默不发。"""
    advisor = ModelAdvisor(enabled=True, protocol="gibberish", api_key="k",
                           base_url="http://127.0.0.1:1", model="m")
    assert advisor.protocol == "openai"


# ── 真实 HTTP：协义与端点 ─────────────────────────────────────────────────

def _ok_server(recorder: _Recorder, reply: str) -> tuple[Any, str]:
    payload = {"choices": [{"message": {"content": reply}}]}
    server, port = _serve(_json_responder(payload), recorder)
    return server, f"http://127.0.0.1:{port}"


def test_openai_request_shape() -> None:
    recorder = _Recorder()
    server, base = _ok_server(recorder, "建议：放行\n理由：只读命令")
    try:
        advisor = ModelAdvisor(enabled=True, protocol="openai", api_key="secret-key",
                               base_url=base, model="deepseek-chat")
        advice = advisor.suggest(tool_name="Bash", command="ls -la",
                                 cwd=r"D:\workspace", risk="notice")
    finally:
        server.shutdown()
        server.server_close()

    assert advice.ok is True
    assert advice.suggestion == SUGGEST_ALLOW
    assert advice.reason == "只读命令"
    assert advice.model == "deepseek-chat"
    assert recorder.paths == ["/chat/completions"]
    body = recorder.bodies[0]
    # 契约：temperature 0、max_tokens 小
    assert body["temperature"] == 0
    assert body["max_tokens"] == MAX_TOKENS
    assert body["model"] == "deepseek-chat"
    # 鉴权用 Bearer，且 key 不在 body 里
    assert recorder.headers[0]["authorization"] == "Bearer secret-key"
    assert "secret-key" not in json.dumps(body)


def test_anthropic_request_shape() -> None:
    recorder = _Recorder()
    payload = {"content": [{"type": "text", "text": "建议：别放行\n理由：不可恢复"}]}
    server, port = _serve(_json_responder(payload), recorder)
    try:
        advisor = ModelAdvisor(enabled=True, protocol="anthropic", api_key="sk-ant",
                               base_url=f"http://127.0.0.1:{port}", model="claude-x")
        advice = advisor.suggest(tool_name="Bash", command="rm -rf /",
                                 cwd="", risk="critical")
    finally:
        server.shutdown()
        server.server_close()

    assert advice.suggestion == SUGGEST_DENY
    assert advice.reason == "不可恢复"
    assert recorder.paths == ["/v1/messages"]
    # anthropic 用 x-api-key，不用 Bearer
    assert recorder.headers[0]["x-api-key"] == "sk-ant"
    assert recorder.headers[0]["anthropic-version"]
    assert "system" in recorder.bodies[0]


def test_401_is_its_own_tier() -> None:
    recorder = _Recorder()
    server, port = _serve(
        _json_responder({"error": "unauthorized"}, status=401), recorder)
    try:
        advisor = ModelAdvisor(enabled=True, api_key="bad", model="m",
                               base_url=f"http://127.0.0.1:{port}")
        advice = advisor.suggest(tool_name="Bash", command="ls", cwd="", risk="notice")
    finally:
        server.shutdown()
        server.server_close()
    assert advice.state == STATE_AUTH_FAILED
    assert advice.ok is False


def test_unparseable_body_is_the_invalid_tier() -> None:
    recorder = _Recorder()
    server, port = _serve(_json_responder(None, raw_body=b"not j"), recorder)
    try:
        advisor = ModelAdvisor(enabled=True, api_key="k", model="m",
                               base_url=f"http://127.0.0.1:{port}")
        advice = advisor.suggest(tool_name="Bash", command="ls", cwd="", risk="notice")
    finally:
        server.shutdown()
        server.server_close()
    assert advice.state == STATE_INVALID


def test_empty_choices_is_the_invalid_tier() -> None:
    recorder = _Recorder()
    server, base = _ok_server(recorder, "")
    try:
        advisor = ModelAdvisor(enabled=True, api_key="k", model="m", base_url=base)
        advice = advisor.suggest(tool_name="Bash", command="ls", cwd="", risk="notice")
    finally:
        server.shutdown()
        server.server_close()
    assert advice.state == STATE_INVALID
    assert advice.detail == "响应里没有正文"


def test_connection_refused_is_bad_response_not_a_crash() -> None:
    """连不上不能抛异常——调用方在审批 timer 里，抛了会带走整轮建卡。"""
    advisor = ModelAdvisor(enabled=True, api_key="k", model="m",
                           base_url="http://127.0.0.1:1")
    advice = advisor.suggest(tool_name="Bash", command="ls", cwd="", risk="notice")
    assert advice.state == STATE_BAD_RESPONSE
    assert advice.ok is False


def test_timeout_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    """超过 15s 就是超时档。用假 urlopen 造，不用真等 15 秒。"""
    import plugin.plugins.agent_deskmate.model_advice as advice_mod

    def fake_urlopen(*_args: Any, **_kwargs: Any):
        raise TimeoutError("timed out")

    monkeypatch.setattr(advice_mod.urllib.request, "urlopen", fake_urlopen)
    advisor = ModelAdvisor(enabled=True, api_key="k", model="m",
                           base_url="http://127.0.0.1:1")
    advice = advisor.suggest(tool_name="Bash", command="ls", cwd="", risk="notice")
    assert advice.state == STATE_TIMEOUT
    assert advice.detail == f"{REQUEST_TIMEOUT:g}s"


def test_ping_reports_model_and_elapsed() -> None:
    """「测试连接」要回显模型名与耗时，且不发任何命令。"""
    recorder = _Recorder()
    server, base = _ok_server(recorder, "在")
    try:
        advisor = ModelAdvisor(enabled=True, api_key="k", model="deepseek-chat",
                               base_url=base)
        result = advisor.ping()
    finally:
        server.shutdown()
        server.server_close()
    assert result["ok"] is True
    assert result["state"] == STATE_OK
    assert result["model"] == "deepseek-chat"
    assert isinstance(result["elapsed_ms"], int)
    # 只发一句招呼，不含任何命令
    sent = json.dumps(recorder.bodies[0], ensure_ascii=False)
    assert "rm -rf" not in sent
    assert "工具" not in sent


def test_ping_never_leaks_the_api_key() -> None:
    """key 不进返回值——日志、返回值、卡片都不该出现它。"""
    recorder = _Recorder()
    server, base = _ok_server(recorder, "在")
    try:
        advisor = ModelAdvisor(enabled=True, api_key="super-secret-key",
                               model="m", base_url=base)
        result = advisor.ping()
    finally:
        server.shutdown()
        server.server_close()
    assert "super-secret-key" not in json.dumps(result, ensure_ascii=False)


def test_ping_without_config_reports_the_gap() -> None:
    advisor = ModelAdvisor(enabled=False)
    result = advisor.ping()
    assert result["ok"] is False
    assert result["state"] == STATE_DISABLED


# ── 纪律：建议永不通向回填 ────────────────────────────────────────────────

def test_advice_has_no_decision_field() -> None:
    """Advice 的产出里没有 allow/deny 回填位——想加就得先改这个测试。"""
    advice = Advice(STATE_OK, suggestion=SUGGEST_ALLOW, reason="x")
    fields = set(vars(advice))
    assert "decision" not in fields
    assert "behavior" not in fields
    assert set(vars(advice)) == {"state", "suggestion", "reason", "model",
                                 "elapsed_ms", "detail"}


def test_approval_module_does_not_import_the_advisor() -> None:
    """规格 §8：模型只给建议。审批中继不该知道模型的存在。

    这条守着"建议"与"回填"之间的墙：哪天有人想图方便让模型直接回填，
    第一步就是在这里 import，测试会红。
    """
    source = (Path(__file__).resolve().parents[1] / "approval.py").read_text(
        encoding="utf-8")
    assert "model_advice" not in source
    assert "ModelAdvisor" not in source
