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
