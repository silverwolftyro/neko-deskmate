"""5b 最小版：模型建议。**只建议，永不代决。**

规格 §8 的约束在这里是硬编码的，不是配置项：

* 模型输出只填卡片上的「建议 + 理由」两块，**不参与任何回填**。
  L2 自动放行仍然要用户签字授权 + 门控全过 + 有判断后端，那条路不接这个类。
* L1 建议仍然要求 ``ai_judgement=true`` **且** 用户在授权入口签过字。
  模型能说话了，不等于它能替用户决定。

**零新依赖**：只用标准库 ``urllib``。宿主是打包发行的 Python，
加一个 openai/anthropic SDK 就要改依赖清单，不值当。

调用契约（写死，不做成配置）：

* 超时 15s——审批是"人在等"，等 15s 还没有建议就降级，不要让用户干等；
* ``temperature=0``——同一个问题要得到同一个答案，建议不该有随机性；
* ``max_tokens`` 给小（300）——只要一句建议一句理由，多出来的都是浪费；
* **只发四样东西**：工具名、命令、工作目录、风险档。
  刻意不发会话历史、初始 prompt、转录内容——那些是 5b 之后的事，
  而且发得越多，泄露面越大。

失败分档（每档文案不同，见 ``ADVICE_STATE``）：没配 key / 401 鉴权失败 /
超时 / 返回不合法。不合成一个"调用失败"——用户看到"没配 key"会去配 key，
看到"调用失败"只会以为插件坏了。
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any

# 调用契约常量。写死在这里，不从配置读——见模块文档。
REQUEST_TIMEOUT = 15.0
MAX_TOKENS = 300
TEMPERATURE = 0.0
ANTHROPIC_VERSION = "2023-06-01"

# 建议的三个取值。**没有"拒绝"**——模型永不代用户拒绝（规格 §2 硬规则 3）。
SUGGEST_ALLOW = "allow"
SUGGEST_DENY = "deny"
SUGGEST_UNSURE = "unsure"
SUGGESTIONS = (SUGGEST_ALLOW, SUGGEST_DENY, SUGGEST_UNSURE)

# 失败/降级档位。每档对应卡片上一句不同的话。
STATE_OK = "ok"
STATE_NO_KEY = "no_key"
STATE_AUTH_FAILED = "auth_failed"
STATE_TIMEOUT = "timeout"
STATE_INVALID = "invalid"
STATE_BAD_RESPONSE = "bad_response"
STATE_DISABLED = "disabled"
STATE_UNAUTHORIZED = "unauthorized"
STATE_ERROR = "error"

# 卡片上「建议」那一块的降级文案。**全部陈述句**：
# 不说"填个 key 就好"（索取），不说"模型不可用"（暗示闸门不工作）。
ADVICE_STATE_TEXT = {
    STATE_OK: "",                       # 有真建议时由调用方填三行
    STATE_NO_KEY: "未配置模型，当前只做提醒。安全兜底与模型无关。",
    STATE_AUTH_FAILED: "模型鉴权失败（401），本次不给建议。安全兜底与模型无关。",
    STATE_TIMEOUT: "模型 15 秒内没有回应，本次不给建议。安全兜底与模型无关。",
    STATE_INVALID: "模型的回复无法解析，本次不给建议。安全兜底与模型无关。",
    STATE_BAD_RESPONSE: "模型接口返回异常，本次不给建议。安全兜底与模型无关。",
    STATE_DISABLED: "未开启模型建议。安全兜底与模型无关。",
    STATE_UNAUTHORIZED: "尚未确认授权，暂不给出建议。安全兜底与模型无关。",
    STATE_ERROR: "本次无法给出建议。安全兜底与模型无关。",
}

# 给模型的系统提示。刻意要求固定两行格式——解析逻辑靠它，
# 但解析本身是宽容的（见 _parse），模型多说话不会让它失败。
SYSTEM_PROMPT = (
    "你是本地审批插件里的建议器。用户会给你一条待执行的命令及其上下文。"
    "你只提建议，决定权在用户手上。"
    "严格按下面两行回复，不要多余内容：\n"
    "建议：放行 / 别放行 / 不确定\n"
    "理由：一句中文，不超过 40 字，说明你依据什么\n"
    "判断标准：只读、可重建、影响范围局限 → 倾向放行；"
    "不可恢复、影响系统或他人、意图不明 → 倾向别放行或不确定。"
)

_PROMPT_TEMPLATE = (
    "工具：{tool}\n"
    "命令：{command}\n"
    "工作目录：{cwd}\n"
    "本地规则判定的风险档：{risk}\n"
)


class Advice:
    """一次建议的结果。``state`` 非 ``ok`` 时 ``suggestion``/``reason`` 为空。"""

    def __init__(self, state: str, *, suggestion: str = "", reason: str = "",
                 model: str = "", elapsed_ms: int = 0, detail: str = "") -> None:
        self.state = state
        self.suggestion = suggestion
        self.reason = reason
        self.model = model
        self.elapsed_ms = elapsed_ms
        self.detail = detail

    @property
    def ok(self) -> bool:
        return self.state == STATE_OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "suggestion": self.suggestion,
            "reason": self.reason,
            "model": self.model,
            "elapsed_ms": self.elapsed_ms,
            "detail": self.detail,
        }


def _clip(text: Any, limit: int = 400) -> str:
    """命令可能很长。截断再发——发全量既浪费 token 又扩大泄露面。"""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def build_prompt(*, tool_name: str, command: str, cwd: str, risk: str) -> str:
    """组装用户消息。**只含这四样**，见模块文档。"""
    return _PROMPT_TEMPLATE.format(
        tool=tool_name or "未知工具",
        command=_clip(command) or "（未提供）",
        cwd=cwd or "未知",
        risk=risk or "未知",
    )


def _endpoint(base_url: str, protocol: str) -> str:
    """拼接口地址。base_url 末尾的斜杠由用户负责，这里只补协议路径。"""
    root = (base_url or "").strip().rstrip("/")
    if protocol == "anthropic":
        return f"{root}/v1/messages"
    return f"{root}/chat/completions"


def _body(protocol: str, model: str, prompt: str) -> dict[str, Any]:
    if protocol == "anthropic":
        return {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "temperature": TEMPERATURE,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        }
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }


def _headers(protocol: str, api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if protocol == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = ANTHROPIC_VERSION
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _extract_text(payload: Any, protocol: str) -> str:
    """从两种协议的响应里取出正文。取不到返回空串 → 上层判 invalid。"""
    if not isinstance(payload, dict):
        return ""
    if protocol == "anthropic":
        blocks = payload.get("content")
        if isinstance(blocks, list):
            parts = [block.get("text", "") for block in blocks
                     if isinstance(block, dict) and block.get("type") == "text"]
            return "\n".join(str(part) for part in parts).strip()
        return ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict):
            return str(message.get("content") or "").strip()
    return ""


def _parse(text: str) -> tuple[str, str]:
    """从模型回复里解析「建议 + 理由」。宽容优先：多说话不影响解析。

    **第一行说了算**：模型如果在后面又改口，以第一次为准——理由同上，
    我们不让一段自由文本反过来改判断。认不出建议就返回 ``unsure``
    而不是报错：模型答非所问时"不确定"比"调用失败"更接近事实，
    也不会让用户以为插件坏了。
    """
    suggestion = ""
    reason = ""
    for raw in str(text or "").splitlines():
        line = raw.strip().lstrip("*#>-").strip()
        if not line:
            continue
        head, sep, tail = (line.partition("：") if "：" in line
                           else line.partition(":"))
        if not sep:
            continue
        key = head.strip().lower()
        value = tail.strip()
        if key in ("建议", "suggestion") and not suggestion:
            suggestion = _normalize(value) or SUGGEST_UNSURE
        elif key in ("理由", "reason") and not reason:
            reason = _clip(value, 80)
    return suggestion or SUGGEST_UNSURE, reason


def _normalize(value: str) -> str:
    flat = value.strip().lower()
    if any(word in flat for word in ("别放行", "不要放行", "拒绝", "deny", "block")):
        return SUGGEST_DENY
    if any(word in flat for word in ("放行", "允许", "allow", "approve")):
        return SUGGEST_ALLOW
    if any(word in flat for word in ("不确定", "无法判断", "unsure", "unknown")):
        return SUGGEST_UNSURE
    return ""


class ModelAdvisor:
    """模型建议客户端。线程安全（无共享可变状态），可在线程池里调用。"""

    def __init__(self, *, protocol: str = "openai", base_url: str = "",
                 api_key: str = "", model: str = "",
                 enabled: bool = False) -> None:
        proto = str(protocol or "openai").strip().lower()
        self.protocol = proto if proto in ("openai", "anthropic") else "openai"
        self.base_url = str(base_url or "").strip()
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "").strip()
        self.enabled = bool(enabled)

    # ── 只读探针：不发起任何请求 ──────────────────────────────────────────

    def configured(self) -> bool:
        """四样齐了才叫配好。缺一样都给出**具体**缺哪一样。"""
        return bool(self.enabled and self.api_key and self.base_url and self.model)

    def missing(self) -> str:
        if not self.enabled:
            return "未开启模型建议"
        for name, value in (("api_key", self.api_key), ("base_url", self.base_url),
                            ("model", self.model)):
            if not value:
                return f"未配置 {name}"
        return ""

    # ── 调用 ──────────────────────────────────────────────────────────────

    def suggest(self, *, tool_name: str, command: str, cwd: str,
                risk: str) -> Advice:
        """拿一条建议。任何失败都返回带档位的 Advice，**不抛异常**。

        调用方（审批 timer）在线程池里跑它；这里抛异常会把一轮卡片处理
        整个带掉，所以失败必须变成返回值。
        """
        if not self.enabled:
            return Advice(STATE_DISABLED)
        gap = self.missing()
        if gap:
            return Advice(STATE_NO_KEY, detail=gap)
        if not self.configured():
            return Advice(STATE_NO_KEY, detail=self.missing())

        prompt = build_prompt(tool_name=tool_name, command=command,
                              cwd=cwd, risk=risk)
        request = urllib.request.Request(
            _endpoint(self.base_url, self.protocol),
            data=json.dumps(_body(self.protocol, self.model, prompt),
                            ensure_ascii=False).encode("utf-8"),
            headers=_headers(self.protocol, self.api_key),
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            elapsed = _elapsed_ms(started)
            if exc.code in (401, 403):
                return Advice(STATE_AUTH_FAILED, model=self.model,
                              elapsed_ms=elapsed, detail=f"HTTP {exc.code}")
            return Advice(STATE_BAD_RESPONSE, model=self.model,
                          elapsed_ms=elapsed, detail=f"HTTP {exc.code}")
        except (TimeoutError, socket.timeout):
            return Advice(STATE_TIMEOUT, model=self.model,
                          elapsed_ms=_elapsed_ms(started),
                          detail=f"{REQUEST_TIMEOUT:g}s")
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                return Advice(STATE_TIMEOUT, model=self.model,
                              elapsed_ms=_elapsed_ms(started),
                              detail=f"{REQUEST_TIMEOUT:g}s")
            return Advice(STATE_BAD_RESPONSE, model=self.model,
                          elapsed_ms=_elapsed_ms(started), detail=str(reason))
        except (OSError, ValueError) as exc:
            return Advice(STATE_BAD_RESPONSE, model=self.model,
                          elapsed_ms=_elapsed_ms(started),
                          detail=type(exc).__name__)

        try:
            payload = json.loads(raw)
        except ValueError:
            return Advice(STATE_INVALID, model=self.model,
                          elapsed_ms=_elapsed_ms(started), detail="响应不是 JSON")
        text = _extract_text(payload, self.protocol)
        if not text:
            return Advice(STATE_INVALID, model=self.model,
                          elapsed_ms=_elapsed_ms(started), detail="响应里没有正文")
        suggestion, reason = _parse(text)
        return Advice(STATE_OK, suggestion=suggestion,
                      reason=reason or "模型未给理由", model=self.model,
                      elapsed_ms=_elapsed_ms(started))

    def ping(self) -> dict[str, Any]:
        """「测试连接」：发一句最短的招呼，回显模型名与耗时。

        刻意不发命令、不发任何上下文——连通性测试不需要那些。
        """
        if not self.enabled:
            # 刻意不写 "ai_judgement = false"：那是配置键名，
            # 泄到界面上用户不知道那是什么（C5 的老毛病）。
            return {"ok": False, "state": STATE_DISABLED,
                    "reason": "未开启模型建议"}
        gap = self.missing()
        if gap:
            return {"ok": False, "state": STATE_NO_KEY, "reason": gap}
        request = urllib.request.Request(
            _endpoint(self.base_url, self.protocol),
            data=json.dumps(_body(self.protocol, self.model, "回复一个字：在"),
                            ensure_ascii=False).encode("utf-8"),
            headers=_headers(self.protocol, self.api_key),
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                raw = response.read().decode("utf-8", "replace")
                status = response.status
        except urllib.error.HTTPError as exc:
            return {"ok": False, "state": (STATE_AUTH_FAILED
                                           if exc.code in (401, 403)
                                           else STATE_BAD_RESPONSE),
                    "reason": f"HTTP {exc.code}", "model": self.model,
                    "elapsed_ms": _elapsed_ms(started)}
        except (TimeoutError, socket.timeout):
            return {"ok": False, "state": STATE_TIMEOUT, "model": self.model,
                    "reason": f"{REQUEST_TIMEOUT:g}s 内没有回应",
                    "elapsed_ms": _elapsed_ms(started)}
        except (OSError, ValueError, urllib.error.URLError) as exc:
            return {"ok": False, "state": STATE_BAD_RESPONSE, "model": self.model,
                    "reason": str(getattr(exc, "reason", exc)),
                    "elapsed_ms": _elapsed_ms(started)}
        try:
            payload = json.loads(raw)
        except ValueError:
            return {"ok": False, "state": STATE_INVALID, "model": self.model,
                    "reason": "响应不是 JSON", "elapsed_ms": _elapsed_ms(started)}
        text = _extract_text(payload, self.protocol)
        if not text:
            return {"ok": False, "state": STATE_INVALID, "model": self.model,
                    "reason": "响应里没有正文", "elapsed_ms": _elapsed_ms(started)}
        return {"ok": True, "state": STATE_OK, "model": self.model,
                "elapsed_ms": _elapsed_ms(started), "status": status,
                "reply": _clip(text, 40),
                "note": "连通正常。模型只给建议，永不代你决定"}


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
