"""播报方式开关的"当即生效"测试（2026-11-26 真机逼出来的）。

**这条不是理论担忧**：真机上用户点了「让猫娘说一说」，面板弹「已保存」，
配置也真写进去了（``broadcast_speak = true``），但下一轮播报照旧用旧值。
日志里 "配置已变更" 一次都没出现——``self.config.set()`` 从插件内部
写配置**不会触发宿主的 config_change 生命周期**。

于是修法是：写完配置必须当场翻生效的那个对象。这里钉住三件事：
  1. 翻的是 ``_broadcaster.speak``，不是重建适配器；
  2. ``ai_behavior`` 跟着变（下一条播报就生效）；
  3. **不许调 ``_reset_watch_state()``**——那会把现有转录文件当成
     "运行期新出现"从 0 读，把几个月历史倒进来播一遍。
"""

from __future__ import annotations

from plugin.plugins.agent_deskmate import AgentDeskmatePlugin
from plugin.plugins.agent_deskmate.broadcast import Broadcaster


class _Logger:
    def info(self, *a, **kw):
        pass

    def warning(self, *a, **kw):
        pass

    def error(self, *a, **kw):
        pass


class SpeakHost:
    """只借 ``settings_set_speak`` 一个方法，其余手搭。"""

    settings_set_speak = AgentDeskmatePlugin.settings_set_speak

    def _reset_watch_state(self):
        # 刻意**不借真方法**：借了就会真的清字段。这里只计数——
        # 测试要的是没人调它，不是它能不能跑。
        self._reset_calls += 1

    def __init__(self, *, key_set: bool = True):
        self._broadcaster = Broadcaster(
            lambda **kw: {"status": "accepted"},
            _Logger(), source="agent_deskmate", speak=False)
        self._reset_calls = 0
        self._key_set = key_set
        self._written: dict[str, object] = {}
        self.logger = _Logger()

        class _Config:
            async def set(inner_self, path, value, timeout=None):
                self._written[path] = value

        self.config = _Config()

    async def _plugin_config(self):
        if not self._key_set:
            return {}
        return {"model": {"api_key": "k"}}


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_flips_the_live_broadcaster():
    host = SpeakHost()
    assert host._broadcaster.speak is False
    res = _run(host.settings_set_speak(speak=True))
    assert res.is_ok() is True
    assert host._broadcaster.speak is True
    assert host._broadcaster.ai_behavior == "respond"
    assert host._written["agent_deskmate.approval.broadcast_speak"] is True


def test_flips_back_too():
    host = SpeakHost()
    host._broadcaster.speak = True
    _run(host.settings_set_speak(speak=False))
    assert host._broadcaster.speak is False
    assert host._broadcaster.ai_behavior == "blind"


def test_does_not_reset_watch_state():
    """**这条是防回归的那条**：重建适配器会把历史倒进来播一遍。"""
    host = SpeakHost()
    _run(host.settings_set_speak(speak=True))
    assert host._reset_calls == 0


def test_refused_without_key():
    """红线 3：没 key 时不许开，而且必须说清原因。"""
    host = SpeakHost(key_set=False)
    res = _run(host.settings_set_speak(speak=True))
    assert res.is_ok() is False
    assert "key" in str(res.err()).lower()
    # 拒绝了就不该改生效对象，也不该写配置
    assert host._broadcaster.speak is False
    assert host._written == {}


def test_no_broadcaster_yet_is_not_an_error():
    """还没 poll 过（广播器未建）时点击：只写配置，下一轮自会读到。"""
    host = SpeakHost()
    host._broadcaster = None
    res = _run(host.settings_set_speak(speak=True))
    assert res.is_ok() is True
    assert host._written["agent_deskmate.approval.broadcast_speak"] is True


# ── speak_test：唯一能推任意文本的入口 ───────────────────────────────────


class SpeakTestHost(SpeakHost):
    speak_test = AgentDeskmatePlugin.speak_test

    def __init__(self, *, key_set: bool = True, broadcaster=None):
        super().__init__(key_set=key_set)
        self._broadcaster = broadcaster if broadcaster is not None else self._broadcaster
        # 父类造的广播器默认 speak=False；这个入口测的是"让她念"，
        # 所以默认按用户开了开关来。
        self._broadcaster.speak = True
        self.pushed: list[dict] = []

        class _Push:
            def __call__(inner, **kw):
                self.pushed.append(kw)
                return {"submitted": True}

        self.push_message = _Push()


def test_pushes_both_channels():
    host = SpeakTestHost()
    res = _run(host.speak_test(text="已自动放行，风险很低。"))
    assert res.is_ok() is True
    assert len(host.pushed) == 2
    # ① 念：空 visibility + respond + 指示包裹
    assert host.pushed[0]["visibility"] == []
    assert host.pushed[0]["ai_behavior"] == "respond"
    assert "念出来" in host.pushed[0]["parts"][0]["text"]
    # ② 留底：chat + blind + 精确原文
    assert host.pushed[1]["visibility"] == ["chat"]
    assert host.pushed[1]["ai_behavior"] == "blind"
    assert host.pushed[1]["parts"][0]["text"] == "已自动放行，风险很低。"


def test_empty_text_refused():
    host = SpeakTestHost()
    res = _run(host.speak_test(text="   "))
    assert res.is_ok() is False
    assert host.pushed == []


def test_too_long_refused():
    host = SpeakTestHost()
    res = _run(host.speak_test(text="啊" * 200))
    assert res.is_ok() is False
    assert "越长" in str(res.err())
    assert host.pushed == []


def test_no_key_refused():
    host = SpeakTestHost(key_set=False)
    res = _run(host.speak_test(text="x"))
    assert res.is_ok() is False
    assert "key" in str(res.err()).lower()
    assert host.pushed == []




def test_manual_entry_respects_the_cooldown_and_says_so():
    """**2026-11-26 真机踩过**：连着点三次，后两次被冷却挡了，聊天框里
    只剩留底文字，看起来像"该说的没说"。

    用户 2026-11-26 的裁决：限制保留，但**必须写在 UI 上**，不能静默挡人。
    所以这里钉两件事——照算冷却，且返回的 message 明说原因。
    """
    import plugin.plugins.agent_deskmate.broadcast as bc
    host = SpeakTestHost()
    host._broadcaster.resolve_ai_behavior(now=1000.0)   # 消耗一次 respond
    real_time = bc.time.time
    bc.time.time = lambda: 1001.0                        # 只过了 1 秒
    try:
        res = _run(host.speak_test(text="第二句"))
    finally:
        bc.time.time = real_time
    assert res.is_ok() is True
    payload = res.unwrap()
    assert payload["spoken"] == "skipped"
    assert "30 秒" in payload["message"]
    # 留底照发——用户眼睛看的那条不该被语音的冷却拖累
    assert len(host.pushed) == 1
    assert host.pushed[0]["ai_behavior"] == "blind"


def test_after_the_window_it_speaks_again():
    import plugin.plugins.agent_deskmate.broadcast as bc
    host = SpeakTestHost()
    host._broadcaster.resolve_ai_behavior(now=1000.0)
    real_time = bc.time.time
    bc.time.time = lambda: 1000.0 + bc.RESPOND_COOLDOWN + 1
    try:
        res = _run(host.speak_test(text="第三句"))
    finally:
        bc.time.time = real_time
    assert res.is_ok() is True
    assert len(host.pushed) == 2
    assert host.pushed[0]["ai_behavior"] == "respond"
