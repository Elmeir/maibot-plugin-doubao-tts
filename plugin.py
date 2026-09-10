"""豆包语音合成插件（Doubao TTS）

让麦麦能用豆包语音（火山引擎 · 语音合成大模型）说话。

- 手动触发：发送 ``/说 文本`` / ``/语音 文本`` / ``/speak 文本``，麦麦把文本转语音发到当前会话；
- 麦麦自主触发：插件注册一个 Tool「speak_text」，LLM 在合适时机（用户要求用语音时）调用它；
- 音色：内置火山官方预置音色清单（按名字选即可），也支持直接填 voice_type 音色 ID 或复刻音色 ID；
- 鉴权：火山引擎**新版控制台** API Key（X-Api-Key 单头鉴权），无需旧版 App ID / Access Token。

独立实现；功能组织参考了 xuqian13/tts_voice_plugin，代码不共用（致谢见 README）。
"""

import asyncio
import base64
import hashlib
import json
import logging
import random
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import aiohttp
from pydantic import field_validator

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParamType, ToolParameterInfo

logger = logging.getLogger("plugin.doubao_tts")

SUPPORTED_CONFIG_VERSION = "1.4.0"
# ─── 火山引擎 API ────────────────────────────────────────────────────────────
DOUBAO_TTS_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
DOUBAO_RESOURCE_PRESET = "seed-tts-2.0"   # 预置音色（语音合成模型 2.0）
DOUBAO_RESOURCE_CLONE = "seed-icl-2.0"    # 复刻音色（声音复刻模型 2.0）

# 官方预置音色（voice_type 以 _uranus_bigtts 结尾，配 seed-tts-2.0）
# 配置里可直接写“显示名”，也可直接填任意 voice_type 音色 ID
PRESET_VOICES: Dict[str, str] = {
    "小何 2.0": "zh_female_xiaohe_uranus_bigtts",
    "Vivi 2.0": "zh_female_vv_uranus_bigtts",
    "爽快思思 2.0": "zh_female_shuangkuaisisi_uranus_bigtts",
    "甜美小源 2.0": "zh_female_tianmeixiaoyuan_uranus_bigtts",
    "甜美桃子 2.0": "zh_female_tianmeitaozi_uranus_bigtts",
    "邻家女孩 2.0": "zh_female_linjianvhai_uranus_bigtts",
    "清新女声 2.0": "zh_female_qingxinnvsheng_uranus_bigtts",
    "流畅女声 2.0": "zh_female_liuchangnv_uranus_bigtts",
    "魅力女友 2.0": "zh_female_meilinvyou_uranus_bigtts",
    "云舟 2.0": "zh_male_m191_uranus_bigtts",
    "小天 2.0": "zh_male_taocheng_uranus_bigtts",
    "刘飞 2.0": "zh_male_liufei_uranus_bigtts",
}

# 情感参数（火山支持的 emotion 取值）
PRESET_EMOTIONS: Dict[str, str] = {
    "开心": "happy",
    "伤心": "sad",
    "生气": "angry",
    "害怕": "scare",
    "惊讶": "surprise",
    "讨厌": "hate",
    "哭泣": "tear",
    "抱歉": "sorry",
    "平静": "pleased",
    "播音": "narrator",
    "讲故事": "storytelling",
}

# 默认音色
DEFAULT_VOICE_DISPLAY = "小何 2.0"


def _resolve_voice_type(voice: str) -> str:
    """把配置里的“显示名”或“原始 ID”解析成 voice_type。"""
    voice = (voice or "").strip()
    if not voice:
        return PRESET_VOICES[DEFAULT_VOICE_DISPLAY]
    if voice in PRESET_VOICES:
        return PRESET_VOICES[voice]
    return voice  # 直接当作 voice_type ID（预置 ID / 复刻音色 ID）


def _split_sentences(text: str, max_len: int) -> List[str]:
    """把文本切成不超过 max_len 的若干段，优先在标点处断。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_len:
        return [text]

    ends = "。！？!?…；;"
    parts: List[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if len(buf) >= max_len:
            # 到硬上限：若末尾是标点直接断，否则在最后一个标点处断
            cut = -1
            if buf[-1] not in ends:
                for i in range(len(buf) - 2, -1, -1):
                    if buf[i] in ends:
                        cut = i + 1
                        break
            if cut <= 0:
                parts.append(buf)
                buf = ""
            else:
                parts.append(buf[:cut])
                buf = buf[cut:]
    if buf:
        parts.append(buf)
    return [p.strip() for p in parts if p.strip()]


# ─── 配置模型 ────────────────────────────────────────────────────────────────


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用插件（总开关；关闭=插件彻底卸载、命令消失）",
        json_schema_extra={"label": "插件总开关", "hint": "关掉 = 整个插件卸载，语音功能与命令全部消失"},
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本（勿改）",
        json_schema_extra={"hidden": True, "disabled": True},
    )


class DoubaoSectionConfig(PluginConfigBase):
    """豆包语音 API 连接配置。"""

    __ui_label__ = "豆包语音"
    __ui_icon__ = "plug"
    __ui_order__ = 1

    api_key: str = Field(
        default="",
        description="火山引擎新版控制台 API Key（控制台→豆包语音→API Key 管理）。敏感信息，勿随插件分发",
        json_schema_extra={"label": "API Key", "secret": True, "hint": "火山引擎新版控制台 → 豆包语音 → API Key 管理；敏感信息，勿外传"},
    )
    resource_id: str = Field(
        default=DOUBAO_RESOURCE_PRESET,
        description="资源 ID：seed-tts-2.0=预置音色（默认）；seed-icl-2.0=复刻音色。可直接填这两个常用值，也支持填其他资源 ID",
        json_schema_extra={
            "label": "Resource ID",
            "placeholder": "seed-tts-2.0 或 seed-icl-2.0",
            "example": "seed-tts-2.0",
            "hint": "常用值：seed-tts-2.0（官方预置音色）｜seed-icl-2.0（复刻音色）。想用其他资源 ID 直接填入即可",
        },
    )
    audio_format: str = Field(
        default="mp3",
        description="音频编码格式：mp3（推荐）/ ogg_opus",
        json_schema_extra={"label": "音频格式", "hidden": True},
    )
    sample_rate: int = Field(
        default=24000,
        description="采样率 Hz（默认 24000）",
        json_schema_extra={"label": "采样率", "hidden": True},
    )


class VoiceToneSectionConfig(PluginConfigBase):
    """音色与情感配置。"""

    __ui_label__ = "音色与情感"
    __ui_icon__ = "music"
    __ui_order__ = 2

    @field_validator("emotion", mode="before")
    @classmethod
    def _normalize_emotion(cls, v: Any) -> Any:
        """兼容旧配置：把空字符串归一化为 'none'（下拉不允许空串选项，历史保存值可能为 ""）。"""
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return "none"
        return v

    voice: str = Field(
        default=DEFAULT_VOICE_DISPLAY,
        description="音色：预置音色显示名（见 README 音色表）或直接填 voice_type 音色 ID",
        json_schema_extra={
            "label": "音色",
            "placeholder": "小何 2.0 / Vivi 2.0 / 云舟 2.0 或 voice_type ID",
            "hint": "常用预置音色：小何 2.0、Vivi 2.0、爽快思思 2.0、甜美小源 2.0、云舟 2.0…完整列表见 README",
        },
    )
    emotion: Literal["none", "开心", "伤心", "生气", "害怕", "惊讶", "讨厌", "哭泣", "抱歉", "平静", "播音", "讲故事"] = Field(
        default="none",
        description="情感语气：选一个固定情感（默认“无”=正常语气）。若行为页 emotion_mode=auto，则麦麦自主语音时由 LLM 现场挑，本值仅用于手动 /说 与 fixed 模式",
        json_schema_extra={
            "label": "情感（可自主）",
            "options": {
                "none": {"label": "无（正常语气）", "description": "不带情感，最自然的播报语气"},
                "开心": {"label": "开心", "description": "欢乐上扬的语气"},
                "伤心": {"label": "伤心", "description": "低落难过的语气"},
                "生气": {"label": "生气", "description": "不满或愤怒的语气"},
                "害怕": {"label": "害怕", "description": "紧张害怕的语气"},
                "惊讶": {"label": "惊讶", "description": "吃惊意外的语气"},
                "讨厌": {"label": "讨厌", "description": "嫌弃反感的语气"},
                "哭泣": {"label": "哭泣", "description": "带着哭腔"},
                "抱歉": {"label": "抱歉", "description": "歉意诚恳的语气"},
                "平静": {"label": "平静", "description": "沉稳温和、适合安慰"},
                "播音": {"label": "播音", "description": "字正腔圆的播音腔"},
                "讲故事": {"label": "讲故事", "description": "娓娓道来、适合朗读故事"},
            },
            "hint": "固定情感（默认“无”）；行为页「情感来源」设为 auto 时，麦麦自主语音会自己挑",
        },
    )
    emotion_scale: float = Field(
        default=1.0,
        ge=1.0,
        le=5.0,
        description="情感强度 1~5（配合情感使用，1=最淡）。若行为页 emotion_scale_mode=auto，麦麦自主时由 LLM 按情感挑",
        json_schema_extra={"label": "情感强度（可自主）", "hint": "1~5，1=最淡；行为页「情感强度来源」设为 auto 时麦麦自己挑"},
    )


class SpeedLoudSectionConfig(PluginConfigBase):
    """语速与音量配置（火山固定连续数值，仅手动设置）。"""

    __ui_label__ = "语速与音量"
    __ui_icon__ = "gauge"
    __ui_order__ = 3

    speech_rate: float = Field(
        default=0.0,
        description="语速 -50~100（0=正常）。火山固定连续数值，麦麦不可自主，仅手动设置",
        json_schema_extra={"label": "语速（仅手动）", "hint": "-50~100，0=正常；此值麦麦不能自主调节"},
    )
    loudness: float = Field(
        default=0.0,
        description="音量 -50~100（0=正常）。火山固定连续数值，麦麦不可自主，仅手动设置",
        json_schema_extra={"label": "音量（仅手动）", "hint": "-50~100，0=正常；此值麦麦不能自主调节"},
    )


class BehaviorSectionConfig(PluginConfigBase):
    """行为配置。"""

    __ui_label__ = "行为"
    __ui_icon__ = "sliders"
    __ui_order__ = 4

    command_enabled: bool = Field(
        default=True,
        description=(
            "是否启用 /说 /语音 手动命令。"
            "只影响手动命令；麦麦自主语音由「自主语音方式」（auto_voice_mode）控制"
        ),
        json_schema_extra={"label": "手动命令", "hint": "只影响 /说 /语音 手动命令；麦麦自主语音由下方「自主语音方式」控制"},
    )
    auto_voice_mode: Literal["llm", "probability", "off"] = Field(
        default="llm",
        description="麦麦自主语音方式：llm=由 LLM 自行判断何时用语音（推荐）；probability=按概率偶尔语音（不依赖 LLM）；off=麦麦不自主，仅手动命令",
        json_schema_extra={
            "label": "自主语音方式",
            "options": {
                "llm": {"label": "LLM 自行判断（推荐）", "description": "麦麦自己决定何时用语音：你叫它说、或它觉得适合时都会用"},
                "probability": {"label": "概率触发", "description": "麦麦不靠判断，每收一条消息按概率掷骰，命中则本轮回复转语音（频率由下方概率值精确控制）"},
                "off": {"label": "关闭（仅手动）", "description": "麦麦从不主动语音，只有 /说 /语音 命令才会发声"},
            },
            "hint": "llm=麦麦自己决定何时语音｜probability=每条消息掷骰，命中则回复转语音｜off=仅手动 /说",
        },
    )
    auto_voice_probability: float = Field(
        default=0.1,
        description="概率模式的触发概率 0~1（0.1=平均每 10 轮约 1 轮语音；0=关）。仅 auto_voice_mode=probability 时生效",
        json_schema_extra={"label": "语音概率", "hint": "0~1；0.1=平均每 10 轮约 1 轮语音，仅「概率触发」模式下生效"},
    )
    emotion_mode: Literal["fixed", "auto"] = Field(
        default="fixed",
        description="情感参数来源：fixed=用 [doubao] emotion 固定值（默认）；auto=麦麦自主语音时由 LLM 现场挑情感（手动 /说 仍用固定值）",
        json_schema_extra={
            "label": "情感来源",
            "options": {
                "fixed": {"label": "固定（手动设置）", "description": "始终用上方 [doubao] emotion 的值"},
                "auto": {"label": "麦麦自主", "description": "麦麦自主语音（llm 模式）时由 LLM 结合氛围现场挑"},
            },
            "hint": "fixed=手动 /说 用上方固定情感｜auto=麦麦自主语音时自己挑情感",
        },
    )
    emotion_scale_mode: Literal["fixed", "auto"] = Field(
        default="fixed",
        description="情感强度来源：fixed=用 [doubao] emotion_scale 固定值；auto=麦麦自主时由 LLM 按情感档位挑（1~5）",
        json_schema_extra={
            "label": "情感强度来源",
            "options": {
                "fixed": {"label": "固定（手动设置）", "description": "始终用上方 [doubao] emotion_scale 的值"},
                "auto": {"label": "麦麦自主", "description": "麦麦自主语音时由 LLM 按情感挑强度 1~5"},
            },
            "hint": "fixed=用上方固定强度｜auto=麦麦自主语音时自己挑强度（1~5）",
        },
    )
    timeout_seconds: float = Field(
        default=30.0,
        description="请求火山接口超时（秒）",
        json_schema_extra={"label": "超时（秒）", "hint": "单次合成请求的最长等待时间"},
    )
    max_text_length: int = Field(
        default=150,
        description="单条语音最大文本长度（超过按句切分多条发送）",
        json_schema_extra={"label": "单条最大字数", "hint": "超过就按句子切成多条语音依次发"},
    )
    fallback_to_text: bool = Field(
        default=True,
        description="合成失败时把文本以文字形式发出（避免用户干等）",
        json_schema_extra={"label": "失败降级发文字", "hint": "合成失败时自动改发文字，避免用户干等"},
    )
    send_error_prompt: bool = Field(
        default=True,
        description="合成失败时向用户发一句提示",
        json_schema_extra={"label": "失败提示", "hint": "合成失败时额外发一句“语音合成失败了”之类的说明"},
    )
    sync_chat_context: bool = Field(
        default=True,
        description=(
            "发送语音后把原文写回麦麦的对话上下文。"
            "关掉后麦麦下一轮不知道自己说过什么（宿主默认不同步语音消息）"
        ),
        json_schema_extra={
            "label": "同步对话上下文",
            "hint": "发完语音把原文补进对话历史，麦麦才知道自己说过什么；建议保持开启",
        },
    )
    follow_segmentation: bool = Field(
        default=False,
        description=(
            "跟随分段发送：开启后本轮回复的每一段都各自转语音，"
            "配合宿主的后处理分段或「智能分段插件」使用。"
            "关闭时只把本轮第一条回复转成语音（可能丢失后续分段）"
        ),
        json_schema_extra={
            "label": "跟随分段发语音",
            "hint": "开=配合宿主分段或智能分段插件，每段各自转语音；关=只转第一条",
        },
    )
    cache_enabled: bool = Field(
        default=False,
        description=(
            "把合成结果缓存到本地：同样的文本+音色+情感+语速直接复用音频，"
            "不重复调用火山接口（省钱、省等待）"
        ),
        json_schema_extra={"label": "本地合成缓存", "hint": "同文本同音色直接复用本地音频，不再调火山接口"},
    )
    cache_dir: str = Field(
        default="",
        description="缓存目录；留空 = MaiBot 启动目录下的 doubao-tts-cache/。建议填绝对路径",
        json_schema_extra={"label": "缓存目录", "hint": "留空 = MaiBot 启动目录下的 doubao-tts-cache/，建议填绝对路径"},
    )
    cache_max_files: int = Field(
        default=500,
        description="缓存文件数量上限，超出后按最旧优先清理",
        json_schema_extra={"label": "缓存上限（条）", "hint": "文件数超过上限时按最旧优先清理"},
    )
    context_prefix: str = Field(
        default="[语音]",
        description=(
            "写回对话上下文时加在原文前面的标记，用来表明这是语音。"
            "默认「[语音]」；清空则只写原文"
        ),
        json_schema_extra={
            "label": "上下文标记",
            "hint": "写回对话上下文时加在原文前，让麦麦知道这句是语音说的；清空则只写原文",
        },
    )


class DoubaoTTSRootConfig(PluginConfigBase):
    """插件根配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig, json_schema_extra={"label": "插件"})
    doubao: DoubaoSectionConfig = Field(default_factory=DoubaoSectionConfig, json_schema_extra={"label": "豆包语音"})
    voice_tone: VoiceToneSectionConfig = Field(default_factory=VoiceToneSectionConfig, json_schema_extra={"label": "音色与情感"})
    speed_loud: SpeedLoudSectionConfig = Field(default_factory=SpeedLoudSectionConfig, json_schema_extra={"label": "语速与音量"})
    behavior: BehaviorSectionConfig = Field(default_factory=BehaviorSectionConfig, json_schema_extra={"label": "行为"})


# ─── 主插件 ──────────────────────────────────────────────────────────────────


class DoubaoTTSPlugin(MaiBotPlugin):
    """豆包语音合成插件：文字转语音，让麦麦开口说话。"""

    config_model = DoubaoTTSRootConfig

    def __init__(self) -> None:
        super().__init__()
        # 会话 → 概率掷骰命中时间戳（该会话麦麦下一条文字回复将转语音）
        self._pending_voice: Dict[str, float] = {}
        # 防递归：正在发送"概率语音"的标记
        self._sending_pending_voice: bool = False

    # ── 配置读取 ────────────────────────────────────────────────────────

    def _get(self, section: str, key: str, default: Any = None) -> Any:
        try:
            return getattr(getattr(self.config, section, None), key, default)
        except Exception:
            return default

    def _api_key(self) -> str:
        return str(self._get("doubao", "api_key", "") or "").strip()

    # ── 概率自主语音 ────────────────────────────────────────────────────

    def _auto_voice_mode(self) -> str:
        return str(self._get("behavior", "auto_voice_mode", "llm") or "llm").strip().lower()

    def _probability_enabled(self) -> bool:
        if self._auto_voice_mode() != "probability":
            return False
        prob = float(self._get("behavior", "auto_voice_probability", 0.1) or 0.0)
        return 0.0 < prob <= 1.0

    def _roll_probability(self) -> bool:
        """掷骰子：是否本轮触发概率语音。"""
        if not self._probability_enabled():
            return False
        prob = float(self._get("behavior", "auto_voice_probability", 0.1) or 0.0)
        hit = random.random() < prob
        self.ctx.logger.info("[豆包TTS] 概率掷骰 p=%.2f → %s", prob, "命中" if hit else "未命中")
        return hit

    def _is_pending_stream(self, stream_id: str) -> bool:
        """判断某会话是否处于"待语音"状态（概率命中后麦麦下一条文字转语音）。"""
        ts = self._pending_voice.get(stream_id)
        if ts is None:
            return False
        if time.time() - ts > 300:  # 5 分钟窗口：命中后麦麦迟迟没回复则作废
            self._pending_voice.pop(stream_id, None)
            return False
        return True

    async def _extract_message_text(self, message: Any) -> Tuple[str, str]:
        """从序列化消息 dict 提取 (纯文本, session_id)；失败返回 ("", "")。"""
        if not isinstance(message, dict):
            return "", ""
        session_id = str(message.get("session_id") or "").strip()
        text = str(message.get("processed_plain_text") or "").strip()
        if not text:
            parts: List[str] = []
            for comp in (message.get("raw_message") or []):
                if isinstance(comp, dict) and comp.get("type") == "text":
                    cdata = comp.get("data")
                    if isinstance(cdata, dict):
                        t = str(cdata.get("text") or "").strip()
                        if t:
                            parts.append(t)
            text = " ".join(parts).strip()
        return text, session_id

    @HookHandler(
        "chat.receive.after_process",
        mode=HookMode.OBSERVE,
        name="doubao_tts_probability_mark",
        description="概率模式：收到普通用户消息时按概率标记该会话本轮语音",
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def _on_inbound_message(self, **kwargs: Any) -> None:
        """入站钩子：概率模式下掷骰子，命中则给该会话打"待语音"标。"""
        message = kwargs.get("message")
        if not isinstance(message, dict):
            return
        # 仅当用户主动发消息（排除命令、通知、系统类）
        if message.get("is_command") or message.get("is_notify"):
            return
        text, session_id = await self._extract_message_text(message)
        if not text or not session_id:
            return
        if not self._probability_enabled():
            return
        # 新一轮开始：先清掉上一轮可能残留的标记。
        # 跟随分段模式下标记会保留到本轮所有分段发完，所以必须在这里归零。
        self._pending_voice.pop(session_id, None)
        if self._roll_probability():
            self._pending_voice[session_id] = time.time()
            self.ctx.logger.info("[豆包TTS] 概率命中：会话 %s 本轮回复将用语音", session_id)

    @HookHandler(
        "send_service.before_send",
        mode=HookMode.BLOCKING,
        name="doubao_tts_probability_speak",
        description="概率模式：待语音会话的文字回复转成语音发出",
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def _on_before_send(self, **kwargs: Any) -> Dict[str, Any]:
        """出站钩子：麦麦要发文字且该会话被标记"待语音" → 转语音并中止原文字。"""
        if self._sending_pending_voice:
            return {"action": "continue"}
        message = kwargs.get("message")
        if not isinstance(message, dict):
            return {"action": "continue"}
        session_id = str(message.get("session_id") or "").strip()
        if not session_id or not self._is_pending_stream(session_id):
            return {"action": "continue"}
        # 只处理含文本的消息（纯图片/语音等放行，避免递归）
        raw_message = message.get("raw_message") or []
        has_text = any(
            isinstance(comp, dict) and comp.get("type") == "text"
            for comp in raw_message
        )
        if not has_text:
            return {"action": "continue"}
        text, _ = await self._extract_message_text(message)
        if not text:
            return {"action": "continue"}
        follow = self._get("behavior", "follow_segmentation", False)
        if not follow:
            # 不跟随分段：只转第一条，消费掉标记
            self._pending_voice.pop(session_id, None)
        self.ctx.logger.info(
            "[豆包TTS] 概率语音：会话 %s 文字回复 → 语音（%d字%s）",
            session_id, len(text), "，跟随分段" if follow else "",
        )
        self._sending_pending_voice = True
        try:
            # 统一用「原地替换」而不是中止原消息（v1.4.5）。
            # 旧做法 abort 会让麦麦的 reply 工具拿到"发送失败"，planner/LLM 便会重试发送，
            # 语音和文字就会反复出现；替换则让发送链正常走完，一次成功。
            return await self._replace_with_voice(message, text, kwargs)
        finally:
            self._sending_pending_voice = False

    async def _replace_with_voice(
        self, message: Dict[str, Any], text: str, kwargs: Dict[str, Any]
    ) -> Dict[str, Any]:
        """把一条待发的文字消息原地换成语音消息（用于跟随分段模式）。

        与「中止原消息 + 自己另发」不同，这里让原消息继续走完发送流程：
        分段插件的补发、宿主的后处理都不会被打断，语音也就跟着文本一段一段发。
        合成失败时返回 continue，让原文照常发出，不会丢内容。
        """
        if not self._api_key():
            self.ctx.logger.warning("[豆包TTS] 未配置 API Key，本条保持文字")
            return {"action": "continue"}

        max_len = max(1, int(self._get("behavior", "max_text_length", 150) or 150))
        segments = _split_sentences(text, max_len)
        if not segments:
            return {"action": "continue"}

        session_id = str(message.get("session_id") or "").strip()
        voice_segments: List[Dict[str, Any]] = []
        for seg in segments:
            success, audio, info = await self._synthesize_one(seg)
            if not success or not audio:
                self.ctx.logger.warning("[豆包TTS] 分段合成失败，整条回退为文字: %s", info)
                return {"action": "continue"}
            voice_segments.append({
                "type": "voice",
                # data 留空：可见文本会渲染成「[语音消息]」，
                # 避免把一长串 base64 灌进麦麦的对话上下文
                "data": "",
                "hash": "",
                "binary_data_base64": base64.b64encode(audio).decode("ascii"),
            })

        new_message = dict(message)
        new_message["raw_message"] = voice_segments
        self.ctx.logger.info(
            "[豆包TTS] 会话 %s 已把该段文字换成 %d 条语音（跟随分段）", session_id, len(voice_segments)
        )
        # 语音本身不带文字，另外把原文补进对话上下文
        if self._get("behavior", "sync_chat_context", True):
            await self._append_chat_context(session_id, text)
        return {"action": "continue", "modified_kwargs": {**kwargs, "message": new_message}}

    # ── 生命周期 ────────────────────────────────────────────────────────

    async def on_load(self) -> None:
        key_state = "已配置" if self._api_key() else "未配置"
        self.ctx.logger.info(
            "[豆包TTS] 插件已加载 | API Key: %s | 音色: %s | resource: %s",
            key_state,
            self._get("voice_tone", "voice", DEFAULT_VOICE_DISPLAY),
            self._get("doubao", "resource_id", DOUBAO_RESOURCE_PRESET),
        )
        if not self._api_key():
            self.ctx.logger.warning(
                "[豆包TTS] 尚未配置 API Key：请在插件配置 [doubao] api_key 填写火山引擎新版控制台的 API Key"
            )

    async def on_unload(self) -> None:
        self.ctx.logger.info("[豆包TTS] 插件已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        self.ctx.logger.info("[豆包TTS] 配置更新 scope=%s version=%s", scope, version)

    # ── 火山合成 ────────────────────────────────────────────────────────

    # ── 本地合成缓存 ────────────────────────────────────────────────────

    def _cache_dir(self) -> Optional[Path]:
        """缓存目录；未开启或建不出来时返回 None（等于禁用缓存）。"""
        if not bool(self._get("behavior", "cache_enabled", False)):
            return None
        raw = str(self._get("behavior", "cache_dir", "") or "").strip()
        base = Path(raw) if raw else Path("doubao-tts-cache")
        try:
            base.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001 缓存失败不该影响合成
            self.ctx.logger.warning("[豆包TTS] 缓存目录不可用（%s），本次不使用缓存", exc)
            return None
        return base

    @staticmethod
    def _cache_key(req_params: Dict[str, Any]) -> str:
        """缓存键：直接对发出去的合成参数做摘要，文本/音色/情感/语速/音量天然都在里面。"""
        try:
            body = json.dumps(req_params, ensure_ascii=False, sort_keys=True)
        except Exception:  # noqa: BLE001
            return ""
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def _cache_ext(self) -> str:
        return str(self._get("doubao", "audio_format", "mp3")).strip().lstrip(".") or "mp3"

    def _cache_lookup(self, cache_key: str, cache_dir: Path) -> Optional[bytes]:
        path = cache_dir / f"{cache_key}.{self._cache_ext()}"
        try:
            if not path.is_file():
                return None
            data = path.read_bytes()
            if not data:
                return None
            path.touch()  # 命中刷新时间戳，让清理时保留热数据
            return data
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[豆包TTS] 读取缓存失败（忽略）: %s", exc)
            return None

    def _cache_store(self, cache_key: str, audio: bytes, cache_dir: Path) -> None:
        try:
            (cache_dir / f"{cache_key}.{self._cache_ext()}").write_bytes(audio)
            self._cache_cleanup(cache_dir)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[豆包TTS] 写入缓存失败（忽略）: %s", exc)

    def _cache_cleanup(self, cache_dir: Path) -> None:
        """文件数超过上限时，按最旧优先清理。"""
        try:
            limit = max(10, int(self._get("behavior", "cache_max_files", 500) or 500))
        except (TypeError, ValueError):
            limit = 500
        try:
            files = [p for p in cache_dir.iterdir() if p.is_file()]
        except Exception:  # noqa: BLE001
            return
        if len(files) <= limit:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        removed = 0
        for stale in files[: len(files) - limit]:
            try:
                stale.unlink()
                removed += 1
            except Exception:  # noqa: BLE001
                pass
        if removed:
            self.ctx.logger.info("[豆包TTS] 缓存超上限，已清理 %d 个最旧文件", removed)

    async def _synthesize_one(
        self, text: str, overrides: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, bytes, str]:
        """调用火山新版接口合成一段音频。

        Args:
            text: 要合成的文本
            overrides: 调用方按参数独立覆盖（key 可选 emotion/emotion_scale/speech_rate/loudness；
                       value=None 或缺省=用配置固定值）。供"麦麦自主"时 LLM 现场填。

        Returns:
            (ok, audio_bytes 或 b"", 错误信息或音色ID)
        """
        api_key = self._api_key()
        if not api_key:
            return False, b"", "API Key 未配置（插件配置 → 豆包语音 → api_key）"
        text = (text or "").strip()
        if not text:
            return False, b"", "文本为空"

        ov = overrides if isinstance(overrides, dict) else {}

        resource_id = str(self._get("doubao", "resource_id", DOUBAO_RESOURCE_PRESET)).strip()
        voice_type = _resolve_voice_type(str(self._get("voice_tone", "voice", DEFAULT_VOICE_DISPLAY)))
        audio_format = str(self._get("doubao", "audio_format", "mp3")).strip() or "mp3"
        sample_rate = int(self._get("doubao", "sample_rate", 24000) or 24000)
        timeout = float(self._get("behavior", "timeout_seconds", 30.0) or 30.0)

        req_params: Dict[str, Any] = {
            "text": text,
            "speaker": voice_type,
            "audio_params": {"format": audio_format, "sample_rate": sample_rate},
        }
        # 情感：调用方覆盖优先，否则用配置固定值；"none"/空 = 不带情感
        emotion_cfg = ""
        if ov.get("emotion") is not None:
            emotion_cfg = str(ov["emotion"] or "").strip()
        if not emotion_cfg or emotion_cfg == "none":
            emotion_cfg = str(self._get("voice_tone", "emotion", "") or "").strip()
        if emotion_cfg and emotion_cfg != "none":
            emotion_val = PRESET_EMOTIONS.get(emotion_cfg, emotion_cfg)
            req_params["emotion"] = emotion_val
            # 情感强度：调用方覆盖优先
            scale = None
            if ov.get("emotion_scale") is not None:
                try:
                    scale = float(ov["emotion_scale"])
                except (TypeError, ValueError):
                    scale = None
            if scale is None:
                scale = float(self._get("voice_tone", "emotion_scale", 1.0) or 1.0)
            if 1.0 <= scale <= 5.0:
                req_params["emotion_scale"] = scale
        # 语速 → ratio（调用方覆盖优先）
        rate = None
        if ov.get("speech_rate") is not None:
            try:
                rate = float(ov["speech_rate"])
            except (TypeError, ValueError):
                rate = None
        if rate is None:
            rate = float(self._get("speed_loud", "speech_rate", 0.0) or 0.0)
        if rate:
            req_params["speed_ratio"] = round(1.0 + rate / 100.0, 4)
        # 音量 → ratio（调用方覆盖优先）
        vol = None
        if ov.get("loudness") is not None:
            try:
                vol = float(ov["loudness"])
            except (TypeError, ValueError):
                vol = None
        if vol is None:
            vol = float(self._get("speed_loud", "loudness", 0.0) or 0.0)
        if vol:
            req_params["volume_ratio"] = round(1.0 + vol / 100.0, 4)

        # 本地缓存：同样的文本+音色+情感+语速直接复用上次的音频，不重复调 API
        cache_dir = self._cache_dir()
        cache_key = self._cache_key(req_params) if cache_dir is not None else ""
        if cache_dir is not None and cache_key:
            cached = self._cache_lookup(cache_key, cache_dir)
            if cached:
                self.ctx.logger.info(
                    "[豆包TTS] 命中本地缓存（%d 字节），跳过 API 调用", len(cached)
                )
                return True, cached, voice_type

        headers = {
            "Content-Type": "application/json",
            "X-Api-Key": api_key,
            "X-Api-Resource-Id": resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
        }
        payload = {"req_params": req_params}
        self.ctx.logger.info("[豆包TTS] 合成请求: %d字 | %s | %s | 情感=%s", len(text), resource_id, voice_type, req_params.get("emotion", "-"))

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    DOUBAO_TTS_URL,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status != 200:
                        body = (await resp.text(errors="replace")).strip()[:300]
                        self.ctx.logger.error("[豆包TTS] HTTP %s: %s", resp.status, body)
                        return False, b"", f"火山接口 HTTP {resp.status}: {body}"

                    # NDJSON 流：code=0 携带 base64 音频段；20000000=结束；>0=业务错误
                    chunks: List[bytes] = []
                    async for raw in resp.content:
                        line = raw.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line.decode("utf-8"))
                        except Exception:
                            continue
                        code = obj.get("code")
                        if code == 20000000:
                            break
                        if code == 0:
                            data_b64 = obj.get("data")
                            if data_b64:
                                try:
                                    chunks.append(base64.b64decode(data_b64))
                                except Exception:
                                    self.ctx.logger.warning("[豆包TTS] base64 解码失败，跳过片段")
                        else:
                            msg = obj.get("message") or obj.get("error") or f"code={code}"
                            self.ctx.logger.error("[豆包TTS] 业务错误: %s", msg)
                            return False, b"", f"火山业务错误: {msg}"

                    audio = b"".join(chunks)
                    if not audio:
                        return False, b"", "火山返回空音频（检查音色与 resource_id 是否匹配：预置音色用 seed-tts-2.0，复刻音色用 seed-icl-2.0）"
                    self.ctx.logger.info("[豆包TTS] 合成成功 %d 字节", len(audio))
                    if cache_dir is not None and cache_key:
                        self._cache_store(cache_key, audio, cache_dir)
                    return True, audio, voice_type
        except asyncio.TimeoutError:
            self.ctx.logger.error("[豆包TTS] 请求超时（%ss）", timeout)
            return False, b"", f"请求超时（{timeout:.0f}s）"
        except aiohttp.ClientError as exc:
            self.ctx.logger.error("[豆包TTS] 网络错误: %s", exc)
            return False, b"", f"网络错误: {type(exc).__name__}"
        except Exception as exc:  # noqa: BLE001 兜底
            self.ctx.logger.error("[豆包TTS] 异常: %s", exc, exc_info=True)
            return False, b"", f"错误: {exc}"

    async def _send_voice(self, audio: bytes, stream_id: str, text: str = "") -> bool:
        """把音频 base64 后经 send.custom("voice") 发到会话，并把原文写回对话上下文。

        只发语音是不够的：宿主 send_service 的 sync_to_maisaka_history 默认关闭，
        而语音组件的可见文本只会渲染成「[语音消息]」，所以麦麦下一轮既不知道自己发过语音、
        也不知道说了什么。这里补两件事：
          1) processed_plain_text 让入库的语音带上文字（长期记忆能检索到这句话）；
          2) maisaka.context.append 把原文写回对话历史（planner / replyer 读的就是它）。
        """
        try:
            b64 = base64.b64encode(audio).decode("ascii")
            ok = bool(
                await self.ctx.send.custom(
                    "voice", b64, stream_id, processed_plain_text=text
                )
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.error("[豆包TTS] 发送语音失败: %s", exc)
            return False

        if ok and text and self._get("behavior", "sync_chat_context", True):
            await self._append_chat_context(stream_id, text)
        return ok

    async def _append_chat_context(self, stream_id: str, text: str) -> None:
        """把刚说出去的话写回麦麦的对话上下文。"""

        prefix = str(self._get("behavior", "context_prefix", "") or "").strip()
        visible = f"{prefix}{text}" if prefix else text
        try:
            result = await self.ctx.maisaka.context.append(
                stream_id=stream_id,
                segments=[{"type": "text", "data": visible}],
                visible_text=visible,
                source_kind="guided_reply",
            )
            if isinstance(result, dict) and not result.get("success", True):
                self.ctx.logger.warning(
                    "[豆包TTS] 同步对话上下文失败: %s", result.get("error", "未知原因")
                )
        except Exception as exc:  # noqa: BLE001 同步失败不该影响已经发出去的语音
            self.ctx.logger.warning("[豆包TTS] 同步对话上下文异常: %s", exc)

    async def _speech(self, text: str, stream_id: str, overrides: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        """把整段文本切成多条语音逐段发送。返回 (是否全部成功, 说明)。"""
        max_len = max(1, int(self._get("behavior", "max_text_length", 150) or 150))
        segments = _split_sentences(text, max_len)
        if not segments:
            return False, "文本为空"
        total = len(segments)
        ok = 0
        last_voice = ""
        for i, seg in enumerate(segments):
            success, audio, info = await self._synthesize_one(seg, overrides=overrides)
            if not success:
                self.ctx.logger.warning("[豆包TTS] 第 %d/%d 段失败: %s", i + 1, total, info)
                continue
            if await self._send_voice(audio, stream_id, seg):
                ok += 1
                last_voice = info
            await asyncio.sleep(0.35)
        if ok == total and total > 0:
            return True, f"已发送 {total} 条语音（{last_voice}）"
        if ok > 0:
            return True, f"部分成功 {ok}/{total}"
        return False, "合成失败"

    async def _handle_speech(
        self, text: str, stream_id: str, source: str, overrides: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, str]:
        """统一入口（手动命令 / Tool 都走这里）。

        overrides: 麦麦自主时由调用方传入的按参数覆盖（emotion/emotion_scale/speech_rate/loudness，
                   每个为 None/缺省=用配置固定值）；手动命令不传=全用配置固定值。
        """
        text = (text or "").strip()
        if not text:
            await self._maybe_error(stream_id, "没有要转成语音的文本")
            return False, "空文本"
        if len(text) > 500:
            await self._maybe_error(stream_id, f"文本太长（{len(text)}字），请精简到 500 字以内")
            return False, "文本过长"

        if not self._api_key():
            msg = "豆包语音 API Key 未配置，请先在插件配置里填写"
            await self._maybe_error(stream_id, msg)
            return False, "API Key 未配置"

        ov = overrides if isinstance(overrides, dict) else {}
        self.ctx.logger.info(
            "[豆包TTS] %s 触发语音: %d字 覆盖=%s", source, len(text), json.dumps({k: v for k, v in ov.items() if v is not None}, ensure_ascii=False) or "-"
        )
        ok, note = await self._speech(text, stream_id, overrides=ov)
        if ok:
            return True, note
        # 失败处理
        self.ctx.logger.warning("[豆包TTS] 语音合成失败(%s): %s", source, note)
        if self._get("behavior", "fallback_to_text", True):
            try:
                if await self.ctx.send.text(text, stream_id):
                    # 降级成文字时同样要知道自己说过什么，行为才一致
                    if self._get("behavior", "sync_chat_context", True):
                        await self._append_chat_context(stream_id, text)
                    return True, "语音合成失败，已改为文字回复"
            except Exception:
                pass
        await self._maybe_error(stream_id, "语音合成失败了，请稍后再试")
        return False, note

    async def _maybe_error(self, stream_id: str, msg: str) -> None:
        if self._get("behavior", "send_error_prompt", True):
            try:
                await self.ctx.send.text(msg, stream_id)
            except Exception:
                pass

    # ── Command：手动 ───────────────────────────────────────────────────

    @Command(
        "doubao_tts_say",
        description="用豆包语音把指定文本说出来",
        pattern=r"(?<!\S)/(说|语音|speak)\s+(?P<text>.+)\s*$",
    )
    async def _cmd_say(self, stream_id: str = "", matched_groups: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Tuple[bool, str, bool]:
        """/说 文本 / 语音 文本 / speak 文本"""
        del kwargs
        if not self._get("behavior", "command_enabled", True):
            return False, "手动命令已禁用", True
        if not stream_id:
            return False, "缺少 stream_id", True
        groups = matched_groups or {}
        text = (groups.get("text") or "").strip()
        if not text:
            await self._maybe_error(stream_id, "用法：/说 要转成语音的文本")
            return False, "缺少文本", True
        ok, note = await self._handle_speech(text, stream_id, "命令")
        return ok, note, True

    @Command(
        "doubao_tts_help",
        description="查看豆包语音帮助",
        pattern=r"(?<!\S)/(语音帮助|说帮助|tts帮助)\s*$",
    )
    async def _cmd_help(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, str, bool]:
        """/语音帮助"""
        del kwargs
        if not stream_id:
            return False, "缺少 stream_id", True
        voice_list = "、".join(PRESET_VOICES.keys())
        emotion_list = "、".join(PRESET_EMOTIONS.keys())
        text = (
            "【豆包语音】\n"
            f"- 用法：/说 文本 或 /语音 文本\n"
            f"- API Key：{'已配置' if self._api_key() else '未配置'}\n"
            f"- 当前音色：{self._get('doubao', 'voice', DEFAULT_VOICE_DISPLAY)}\n"
            f"- 预置音色：{voice_list}\n"
            f"- 情感（可选）：{emotion_list}\n"
            "- 换音色/情感：WebUI 插件配置里修改即可"
        )
        await self.ctx.send.text(text, stream_id)
        return True, "已发送帮助", True

    # ── Tool：麦麦自主 ──────────────────────────────────────────────────

    @Tool(
        "doubao_tts_speak",
        description="用豆包语音把文本说出来（发语音消息），适合朗读或更生动的回复",
        brief_description="用语音（豆包TTS）说话",
        detailed_description=(
            "当用户明确要求“用语音/说话/朗读/语音回复”时使用。"
            "文本宜为一句完整的话（5~80字）。若内容很长（>150字），只取其中最想强调的一句话来朗读，其余仍用文字。"
            "可选按对话氛围调节语气：emotion（情感，如安慰时平静、玩闹时开心）、emotion_scale（强度1~5，配合 emotion）。"
            "每个参数可单独给，未给的使用插件配置里的固定值；拿不准就都省略，用默认语气即可。"
            "（语速、音量为固定设置，不由本工具调节。）"
        ),
        parameters=[
            ToolParameterInfo(
                name="text",
                param_type=ToolParamType.STRING,
                description="要转成语音朗读的文本（一句完整的话）",
                required=True,
            ),
            ToolParameterInfo(
                name="emotion",
                param_type=ToolParamType.STRING,
                description="可选：情感语气（按对话氛围选一个）：开心/伤心/生气/害怕/惊讶/讨厌/哭泣/抱歉/平静/播音/讲故事",
                required=False,
                enum_values=list(PRESET_EMOTIONS.keys()),
            ),
            ToolParameterInfo(
                name="emotion_scale",
                param_type=ToolParamType.INTEGER,
                description="可选：情感强度 1~5（配合 emotion 使用，1=最淡）",
                required=False,
            ),
        ],
    )
    async def _tool_speak(
        self,
        text: str = "",
        emotion: str = "",
        emotion_scale: Any = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """LLM 调用：返回结构化结果给 LLM。"""
        stream_id = str(kwargs.get("stream_id") or kwargs.get("chat_id") or "")
        del kwargs
        if not stream_id:
            return {"success": False, "message": "缺少 stream_id，无法发送语音"}
        if not (text or "").strip():
            return {"success": False, "message": "text 为空，未发送"}
        # 麦麦自主语音由 auto_voice_mode 管控（off=麦麦不自主）；
        # command_enabled 只管 /说 手动命令，不该挡这里（修 v1.4.4：原版把它错用在 Tool 上，
        # 关掉手动命令会让麦麦每次调用本工具都失败）。
        if self._auto_voice_mode() == "off":
            return {"success": False, "message": "麦麦自主语音已关闭（behavior.auto_voice_mode=off），请使用 /说 手动命令"}
        # 语速/音量为连续数值，仅支持手动固定（不在此工具参数中提供）
        # 按各参数的"来源模式"组装 overrides：auto=接受 LLM 传入；fixed=忽略、用配置固定值
        overrides: Dict[str, Any] = {}
        if self._get("behavior", "emotion_mode", "fixed") == "auto" and (emotion or "").strip():
            overrides["emotion"] = (emotion or "").strip()
        if self._get("behavior", "emotion_scale_mode", "fixed") == "auto" and emotion_scale is not None:
            overrides["emotion_scale"] = emotion_scale
        ok, note = await self._handle_speech(text, stream_id, "Tool", overrides=overrides or None)
        if ok:
            return {"success": True, "message": note}
        return {"success": False, "message": f"语音失败：{note}"}


def create_plugin() -> MaiBotPlugin:
    return DoubaoTTSPlugin()
