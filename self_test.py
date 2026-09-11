"""豆包语音 TTS 插件本地自检脚本。

不依赖宿主与网络：注入假 maibot_sdk 加载 plugin.py，用 FakeCtx 覆盖
配置模型、文本切分、音色解析、概率模式、出站替换钩子、命令/Tool 管控、
本地缓存与生命周期日志。

用法：
    pip install aiohttp pydantic
    python self_test.py
"""

import asyncio
import sys
import tempfile
import time
import types
from pathlib import Path

HERE = Path(__file__).parent


def install_fake_sdk() -> None:
    """注入最小化 maibot_sdk 假实现，让 plugin.py 可独立导入。"""

    sdk = types.ModuleType("maibot_sdk")

    class MaiBotPlugin:
        def __init__(self) -> None:
            self._plugin_config_instance = None
            self._ctx = None

        @property
        def config(self):
            return self._plugin_config_instance

        @property
        def ctx(self):
            return self._ctx

    class PluginConfigBase:
        # 普通类 + 类属性默认值（假 Field 返回 default）；kwargs 覆盖
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            for key, value in kwargs.items():
                setattr(self, key, value)

    def Field(default=None, default_factory=None, **kwargs):  # noqa: ANN001, ANN003
        return default_factory() if default_factory is not None else default

    def _keep(func):  # noqa: ANN001, ANN202 原样保留函数
        return func

    def _decorator_factory(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        """装饰器占位：先收配置参数（pattern 等记录到函数属性供断言），再原样保留函数。"""
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]

        def wrap(func):  # noqa: ANN001, ANN202
            if kwargs.get("pattern"):
                func._cmd_pattern = kwargs["pattern"]
            return func

        return wrap

    sdk.MaiBotPlugin = MaiBotPlugin
    sdk.PluginConfigBase = PluginConfigBase
    sdk.Field = Field
    sdk.Command = _decorator_factory
    sdk.Tool = _decorator_factory
    sdk.HookHandler = _decorator_factory

    sdk_types = types.ModuleType("maibot_sdk.types")
    sdk_types.HookMode = types.SimpleNamespace(BLOCKING="blocking", OBSERVE="observe")
    sdk_types.HookOrder = types.SimpleNamespace(EARLY="early", NORMAL="normal", LATE="late")
    sdk_types.ErrorPolicy = types.SimpleNamespace(SKIP="skip", LOG="log", ABORT="abort")
    sdk_types.ToolParamType = types.SimpleNamespace(STRING="string", INTEGER="integer")
    sdk_types.ToolParameterInfo = lambda **kwargs: types.SimpleNamespace(**kwargs)  # noqa: ANN003

    sys.modules["maibot_sdk"] = sdk
    sys.modules["maibot_sdk.types"] = sdk_types


install_fake_sdk()

import plugin as plugin_module  # noqa: E402  # noqa: E402  pylint: disable=wrong-import-position
from plugin import (  # noqa: E402
    DEFAULT_VOICE_DISPLAY,
    PRESET_VOICES,
    SUPPORTED_CONFIG_VERSION,
    _resolve_voice_type,
    _split_sentences,
    BehaviorSectionConfig,
    DoubaoSectionConfig,
    DoubaoTTSPlugin,
    DoubaoTTSRootConfig,
)


class FakeLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def _log(self, level: str, msg: str, *args) -> None:  # noqa: ANN001
        self.records.append((level, (msg % args) if args else str(msg)))

    def info(self, msg: str, *args) -> None:  # noqa: ANN001
        self._log("info", msg, *args)

    def warning(self, msg: str, *args) -> None:  # noqa: ANN001
        self._log("warning", msg, *args)

    def error(self, msg: str, *args) -> None:  # noqa: ANN001
        self._log("error", msg, *args)

    def texts(self, level: str | None = None) -> list[str]:
        return [t for lv, t in self.records if level is None or lv == level]


class FakeSend:
    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []
        self.customs: list[tuple] = []

    async def text(self, content: str, stream_id: str, **kwargs) -> bool:  # noqa: ANN003
        self.texts.append((content, stream_id))
        return True

    async def custom(self, kind: str, data: str, stream_id: str, **kwargs) -> bool:  # noqa: ANN003
        self.customs.append((kind, data, stream_id, kwargs))
        return True


class FakeMaisaka:
    class _Context:
        def __init__(self, outer: "FakeMaisaka") -> None:
            self._outer = outer

        async def append(self, **kwargs) -> dict:  # noqa: ANN003
            self._outer.appends.append(kwargs)
            return {"success": True}

    def __init__(self) -> None:
        self.appends: list[dict] = []
        self.context = FakeMaisaka._Context(self)


def make_plugin(**behavior) -> tuple[DoubaoTTSPlugin, FakeLogger, FakeSend, FakeMaisaka]:
    """构造插件实例：config/ctx 走 SDK 注入同款属性。"""
    cfg = DoubaoTTSRootConfig(behavior=BehaviorSectionConfig(**behavior))
    logger, send, maisaka = FakeLogger(), FakeSend(), FakeMaisaka()
    ctx = types.SimpleNamespace(logger=logger, send=send, maisaka=maisaka, paths=None)
    plugin = DoubaoTTSPlugin()
    plugin._plugin_config_instance = cfg  # noqa: SLF001
    plugin._ctx = ctx  # noqa: SLF001
    return plugin, logger, send, maisaka


def main() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        if not cond:
            failures.append(name)

    # ── 1. 配置模型 ──
    cfg = DoubaoTTSRootConfig()
    check("配置模型可实例化", cfg.plugin.enabled is True)
    check("config_version 默认值与常量一致", cfg.plugin.config_version == SUPPORTED_CONFIG_VERSION, cfg.plugin.config_version)
    check("默认音色在预置清单内", DEFAULT_VOICE_DISPLAY in PRESET_VOICES)

    # ── 2. 文本切分 ──
    check("短文本不切分", _split_sentences("你好呀", 150) == ["你好呀"])
    long_text = "第一句话。第二句话！第三句话？第四句话。" * 10
    segs = _split_sentences(long_text, 20)
    check(
        "长文本按标点切分且每段不超上限",
        len(segs) > 1 and all(len(s) <= 20 for s in segs),
        str([len(s) for s in segs]),
    )
    no_punct = "啊" * 45
    segs_np = _split_sentences(no_punct, 20)
    check("无标点文本硬切且无丢字", "".join(segs_np) == no_punct and all(len(s) <= 20 for s in segs_np))
    check("空文本返回空列表", _split_sentences("   ", 10) == [])

    # ── 3. 音色解析 ──
    check("显示名解析为 voice_type", _resolve_voice_type("小何 2.0") == PRESET_VOICES["小何 2.0"])
    check("原始 ID 透传", _resolve_voice_type("S_RcCsDWbd2") == "S_RcCsDWbd2")
    check("空值回退默认音色", _resolve_voice_type("") == PRESET_VOICES[DEFAULT_VOICE_DISPLAY])

    # ── 4. 概率模式 ──
    pl, logger, send, maisaka = make_plugin(auto_voice_mode="off")
    check("off 模式不启用概率", pl._probability_enabled() is False)
    pl, *_ = make_plugin(auto_voice_mode="probability", auto_voice_probability=0.0)
    check("概率 0 视为关闭", pl._probability_enabled() is False)

    pl, logger, send, maisaka = make_plugin(auto_voice_mode="probability", auto_voice_probability=1.0)
    msg = {"session_id": "s1", "processed_plain_text": "你好", "is_command": False, "is_notify": False}
    asyncio.run(pl._on_inbound_message(message=msg))
    check("概率命中后给会话打标", pl._is_pending_stream("s1") is True)
    asyncio.run(pl._on_inbound_message(message=msg))
    check("同会话重复入站会重置标记", pl._is_pending_stream("s1") is True)
    pl._pending_voice.clear()
    cmd_msg = {"session_id": "s1", "processed_plain_text": "/说 你好", "is_command": True, "is_notify": False}
    asyncio.run(pl._on_inbound_message(message=cmd_msg))
    check("命令消息不掷骰", "s1" not in pl._pending_voice)
    pl._pending_voice["old"] = time.time() - 400
    check("过期标记作废并清除", pl._is_pending_stream("old") is False and "old" not in pl._pending_voice)

    # ── 5. 出站替换钩子 ──
    async def fake_synth_ok(text, overrides=None):
        return True, b"AUDIO-" + text.encode("utf-8"), "voice_id"

    pl, logger, send, maisaka = make_plugin(auto_voice_mode="probability", follow_segmentation=False)
    out = asyncio.run(pl._on_before_send(message={"session_id": "no-mark", "raw_message": [{"type": "text", "data": "hi"}]}))
    check("未标记会话直接放行", out == {"action": "continue"})

    pl, logger, send, maisaka = make_plugin(auto_voice_mode="probability", follow_segmentation=False)
    pl._pending_voice["s1"] = time.time()
    out = asyncio.run(
        pl._on_before_send(
            message={
                "session_id": "s1",
                "raw_message": [{"type": "text", "data": "你好世界，这是一条测试语音回复"}],
                "processed_plain_text": "你好世界，这是一条测试语音回复",
            }
        )
    )
    check("未配 Key 时保持文字放行", out == {"action": "continue"})
    check("未配 Key 有 warning 提示", any("API Key" in t for t in logger.texts("warning")))

    pl, logger, send, maisaka = make_plugin(auto_voice_mode="probability", follow_segmentation=False)
    pl._pending_voice["s1"] = time.time()
    pl._plugin_config_instance.doubao = DoubaoSectionConfig(api_key="sk-test")  # noqa: SLF001
    pl._synthesize_one = fake_synth_ok  # noqa: SLF001
    out = asyncio.run(
        pl._on_before_send(
            message={
                "session_id": "s1",
                "raw_message": [{"type": "text", "data": "你好世界"}],
                "processed_plain_text": "你好世界",
            }
        )
    )
    comps = (out.get("modified_kwargs") or {}).get("message", {}).get("raw_message", [])
    check(
        "概率语音：文本组件原地替换为 voice 组件",
        out.get("action") == "continue"
        and len(comps) == 1
        and comps[0].get("type") == "voice"
        and comps[0].get("data") == ""
        and comps[0].get("binary_data_base64"),
        str(comps)[:120],
    )
    check("不跟随分段时标记被消费", "s1" not in pl._pending_voice)
    check("语音原文写回对话上下文", bool(maisaka.appends) and "你好世界" in maisaka.appends[0].get("visible_text", ""))

    # 跟随分段：标记保留
    pl, logger, send, maisaka = make_plugin(auto_voice_mode="probability", follow_segmentation=True)
    pl._pending_voice["s2"] = time.time()
    pl._plugin_config_instance.doubao = DoubaoSectionConfig(api_key="sk-test")  # noqa: SLF001
    pl._synthesize_one = fake_synth_ok  # noqa: SLF001
    out = asyncio.run(
        pl._on_before_send(
            message={"session_id": "s2", "raw_message": [{"type": "text", "data": "分段一"}], "processed_plain_text": "分段一"}
        )
    )
    check("跟随分段时标记保留给后续分段", "s2" in pl._pending_voice and "modified_kwargs" in out)

    # 合成失败：原文照发
    async def fake_synth_fail(text, overrides=None):
        return False, b"", "火山业务错误"

    pl, logger, send, maisaka = make_plugin(auto_voice_mode="probability")
    pl._pending_voice["s3"] = time.time()
    pl._synthesize_one = fake_synth_fail  # noqa: SLF001
    out = asyncio.run(
        pl._on_before_send(
            message={"session_id": "s3", "raw_message": [{"type": "text", "data": "失败测试"}], "processed_plain_text": "失败测试"}
        )
    )
    check("合成失败回退为文字放行", out == {"action": "continue"})

    # ── 6. 本地缓存 ──
    with tempfile.TemporaryDirectory() as tmp:
        pl, *_ = make_plugin(cache_enabled=True, cache_dir="")
        pl._ctx.paths = types.SimpleNamespace(data_dir=tmp)
        cache_dir = pl._cache_dir()
        expected = Path(tmp) / "doubao-tts-cache"
        check("默认缓存目录落在宿主数据目录下", cache_dir == expected and expected.is_dir(), str(cache_dir))

        key = pl._cache_key({"text": "你好", "speaker": "v1"})
        check("缓存键稳定且区分参数", key == pl._cache_key({"speaker": "v1", "text": "你好"}) and key != pl._cache_key({"text": "再见", "speaker": "v1"}))
        pl._cache_store(key, b"AUDIO", cache_dir)
        check("缓存写入后可命中", pl._cache_lookup(key, cache_dir) == b"AUDIO")
        check("未写入的键不命中", pl._cache_lookup(key + "x", cache_dir) is None)

        pl2, *_ = make_plugin(cache_enabled=True, cache_dir=str(Path(tmp) / "explicit"))
        check("显式 cache_dir 使用配置值", pl2._cache_dir() == Path(tmp) / "explicit")

        # 超上限清理最旧（实现有 max(10, limit) 的下限保护）
        pl3, *_ = make_plugin(cache_enabled=True, cache_dir=str(Path(tmp) / "cleanup"), cache_max_files=10)
        cdir = pl3._cache_dir()
        import os

        names = "abcdefghijkl"
        for i, name in enumerate(names):
            p = cdir / f"{name}.mp3"
            p.write_bytes(b"x")
            os.utime(p, (1000 + i, 1000 + i))
        pl3._cache_cleanup(cdir)
        remain = sorted(p.name for p in cdir.iterdir())
        check(
            "超上限按最旧优先清理",
            remain == [f"{n}.mp3" for n in names[2:]] and len(remain) == 10,
            f"剩余 {len(remain)} 个",
        )

    # ── 7. 命令 / Tool 管控 ──
    pl, logger, send, maisaka = make_plugin(command_enabled=False)
    ok, note, weight = asyncio.run(pl._cmd_say(stream_id="s1", matched_groups={"text": "你好"}))
    check("command_enabled=False 拒绝手动命令", ok is False and weight == 1, note)

    pl, logger, send, maisaka = make_plugin(command_enabled=False)
    ok, note, weight = asyncio.run(pl._cmd_help(stream_id="s1"))
    check("帮助命令输出当前音色（读 voice_tone 段）", ok is True and any("小何 2.0" in t for t, _ in send.texts) and isinstance(weight, int))
    ok, note, weight = asyncio.run(pl._cmd_help(stream_id=""))
    check("帮助命令缺 stream_id 返回失败三元组", ok is False and weight == 1)

    calls: list[tuple] = []

    async def fake_handle_speech(text, stream_id, source, overrides=None):
        calls.append((text, overrides))
        return True, "已发送 1 条语音（voice_id）"

    pl, logger, send, maisaka = make_plugin(command_enabled=False)  # auto_voice_mode 默认 llm
    pl._handle_speech = fake_handle_speech  # noqa: SLF001
    result = asyncio.run(pl._tool_speak(text="朗读这句话", stream_id="s1"))
    check("command_enabled 不影响 Tool（v1.4.4 解耦）", result.get("success") is True and len(calls) == 1, str(result))
    check(
        "成功返回携带 stop_after_execution（语音后结束 planner，不再重复 reply）",
        result.get("stop_after_execution") is True,
        str(result),
    )

    pl, logger, send, maisaka = make_plugin(auto_voice_mode="off")
    result = asyncio.run(pl._tool_speak(text="朗读这句话", stream_id="s1"))
    check("auto_voice_mode=off 拒绝 Tool 自主语音", result.get("success") is False)
    check("失败路径不带 stop_after_execution（让 LLM 告知用户）", "stop_after_execution" not in result, str(result))

    pl, logger, send, maisaka = make_plugin(emotion_mode="fixed")
    pl._handle_speech = fake_handle_speech  # noqa: SLF001
    asyncio.run(pl._tool_speak(text="固定情感", stream_id="s1", emotion="开心", emotion_scale=3))
    check("emotion_mode=fixed 忽略 LLM 传入情感", calls[-1][1] in (None, {}), str(calls[-1][1]))

    pl, logger, send, maisaka = make_plugin(emotion_mode="auto", emotion_scale_mode="auto")
    pl._handle_speech = fake_handle_speech  # noqa: SLF001
    asyncio.run(pl._tool_speak(text="自主情感", stream_id="s1", emotion="开心", emotion_scale=3))
    check(
        "emotion_mode=auto 时 LLM 情感透传",
        calls[-1][1] == {"emotion": "开心", "emotion_scale": 3},
        str(calls[-1][1]),
    )

    pl, logger, send, maisaka = make_plugin()
    result = asyncio.run(pl._tool_speak(text="  ", stream_id="s1"))
    check("Tool 空文本返回失败", result.get("success") is False)
    result = asyncio.run(pl._tool_speak(text="你好", stream_id=""))
    check("Tool 缺 stream_id 返回失败", result.get("success") is False)

    # ── 7.5 命令 pattern 锚定 + 冷却限流（AI 审查 #661/#663 标准） ──
    import re as _re

    say_pat = getattr(plugin_module.DoubaoTTSPlugin._cmd_say, "_cmd_pattern", "")
    help_pat = getattr(plugin_module.DoubaoTTSPlugin._cmd_help, "_cmd_pattern", "")
    check(
        "命令 pattern 以 ^ 锚定",
        say_pat.startswith("^") and help_pat.startswith("^"),
        f"{say_pat!r} / {help_pat!r}",
    )
    check(
        "pattern 不误吃句中/句尾触发",
        _re.search(say_pat, "你好 /说 你好呀") is None
        and _re.search(help_pat, "hello tts帮助") is None,
    )
    check(
        "pattern 整条消息正常命中",
        _re.search(say_pat, "/说 你好呀") is not None
        and _re.search(help_pat, "/语音帮助") is not None,
    )

    async def fake_speech_ok(text, stream_id, overrides=None):  # noqa: ANN001, ANN202
        return True, "已发送 1 条语音"

    pl, logger, send, maisaka = make_plugin(command_cooldown_seconds=10)
    pl._plugin_config_instance.doubao = DoubaoSectionConfig(api_key="sk-test")  # noqa: SLF001
    pl._speech = fake_speech_ok  # noqa: SLF001
    ok1 = asyncio.run(pl._handle_speech("第一次", "s1", "命令"))
    ok2 = asyncio.run(pl._handle_speech("第二次", "s1", "命令"))
    ok3 = asyncio.run(pl._handle_speech("换个会话", "s2", "命令"))
    check(
        "冷却窗口内同会话被拦截、其他会话不受影响",
        ok1[0] is True and ok2 == (False, "触发限流") and ok3[0] is True,
        f"{ok1} / {ok2} / {ok3}",
    )
    check("被拦截的命令向用户发冷却提示", any("冷却" in t for t, _ in send.texts))

    n_before = len(send.texts)
    blocked_tool = asyncio.run(pl._handle_speech("工具调用", "s1", "Tool"))
    check(
        "Tool 来源限流只返回失败不发提示",
        blocked_tool == (False, "触发限流") and len(send.texts) == n_before,
    )

    pl, logger, send, maisaka = make_plugin(command_cooldown_seconds=0)
    pl._plugin_config_instance.doubao = DoubaoSectionConfig(api_key="sk-test")  # noqa: SLF001
    pl._speech = fake_speech_ok  # noqa: SLF001
    ok1 = asyncio.run(pl._handle_speech("一", "s1", "命令"))
    ok2 = asyncio.run(pl._handle_speech("二", "s1", "命令"))
    check("冷却设为 0 时不限流", ok1[0] is True and ok2[0] is True)

    # ── 7.6 失败提示去重 + 失败路径的 planner 结束策略 ──
    pl, logger, send, maisaka = make_plugin()  # 未配 Key，send_error_prompt 默认开
    r1 = asyncio.run(pl._tool_speak(text="你好", stream_id="s9"))
    r2 = asyncio.run(pl._tool_speak(text="你好", stream_id="s9"))
    check(
        "失败提示 30 秒内去重（LLM 重试不刷屏）",
        len(send.texts) == 1,
        str(send.texts),
    )
    check(
        "已提示过用户的失败结束 planner（提示 + reply 只留一条）",
        r1.get("stop_after_execution") is True and r2.get("stop_after_execution") is True,
        f"{r1} / {r2}",
    )

    pl, logger, send, maisaka = make_plugin(command_cooldown_seconds=10)
    pl._plugin_config_instance.doubao = DoubaoSectionConfig(api_key="sk-test")  # noqa: SLF001

    async def fake_speech_ok2(text, stream_id, overrides=None):  # noqa: ANN001, ANN202
        return True, "已发送 1 条语音"

    pl._speech = fake_speech_ok2  # noqa: SLF001
    pl._last_speech_at["s5"] = time.time()  # 直接置为冷却中
    r = asyncio.run(pl._tool_speak(text="你好", stream_id="s5"))
    check(
        "冷却静默失败且未提示过 → 不结束 planner（LLM 可转告），并引导勿重试",
        r.get("stop_after_execution") is None and "限流" in r.get("message", "") and "不要连续重试" in r.get("message", ""),
        str(r),
    )

    # ── 8. 生命周期 ──
    pl, logger, send, maisaka = make_plugin()
    asyncio.run(pl.on_load())
    check("未配 Key 时 on_load 输出 warning", any("API Key" in t for t in logger.texts("warning")))
    pl2, logger2, *_ = make_plugin()
    pl2._plugin_config_instance.doubao = DoubaoSectionConfig(api_key="sk-test")
    asyncio.run(pl2.on_load())
    check("已配 Key 时 on_load 无 warning", logger2.texts("warning") == [])
    asyncio.run(pl.on_unload())
    check("on_unload 正常收尾", any("已卸载" in t for t in logger.texts("info")))

    # ── 9. _synthesize_one 前置校验 ──
    pl, logger, send, maisaka = make_plugin()
    ok, audio, info = asyncio.run(pl._synthesize_one("你好"))
    check("未配 Key 合成直接失败并给提示", ok is False and audio == b"" and "API Key" in info, info)

    print()
    if failures:
        print(f"自检未通过：{len(failures)} 项 — {failures}")
        return 1
    print("全部自检通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
