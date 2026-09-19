"""豆包/MiMo 语音合成插件（Doubao & MiMo TTS）

让麦麦能用豆包语音（火山引擎 · 语音合成大模型）或小米 MiMo 语音（音色复刻）说话。

- 手动触发：发送 ``/说 文本`` / ``/语音 文本`` / ``/speak 文本``，麦麦把文本转语音发到当前会话；
- 麦麦自主触发：插件注册一个 Tool「doubao_tts_speak」，LLM 在合适时机（用户要求用语音时）调用它；
- 双引擎：配置主页「语音引擎」下拉切换——
  * doubao：火山官方预置音色清单（按名字选即可），也支持直接填 voice_type 音色 ID 或复刻音色 ID；
  * mimo：小米 MiMo ``mimo-v2.5-tts-voiceclone`` 音色复刻，把 wav/mp3 音源放进插件 ``src/`` 即可开口；
- 配置分页：主页只放开关与下拉选择；豆包 / MiMo 各一个页签改连接与音色；数值与路径归「高级」页；
- 鉴权：豆包用火山引擎新版控制台 API Key（X-Api-Key 单头鉴权）；MiMo 用开放平台 API Key（Bearer）。

独立实现；功能组织参考了 xuqian13/tts_voice_plugin，代码不共用（致谢见 README）。
"""

import asyncio
import base64
import hashlib
import json
import random
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import aiohttp
from pydantic import field_validator

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParamType, ToolParameterInfo

SUPPORTED_CONFIG_VERSION = "1.6.0"
ERROR_PROMPT_DEDUPE_SECONDS = 30.0
"""同一会话失败提示的去重窗口（秒）：LLM 对失败有重试倾向，去重防提示刷屏。"""
PLUGIN_DIR = Path(__file__).resolve().parent
PLUGIN_SRC_DIR = PLUGIN_DIR / "src"

# ─── 火山引擎 API ────────────────────────────────────────────────────────────
DOUBAO_TTS_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
DOUBAO_RESOURCE_PRESET = "seed-tts-2.0"   # 预置音色（语音合成模型 2.0）
DOUBAO_RESOURCE_CLONE = "seed-icl-2.0"    # 复刻音色（声音复刻模型 2.0）

# ─── 小米 MiMo API（音色复刻）────────────────────────────────────────────────
MIMO_TTS_URL = "https://api.xiaomimimo.com/v1/chat/completions"
MIMO_MODEL = "mimo-v2.5-tts-voiceclone"   # 音色复刻模型
MIMO_VOICE_SAMPLE_DEFAULT = "dxl.wav"     # 默认音源：插件 src/ 目录下的 dxl.wav
MIMO_MAX_SAMPLE_BYTES = 10 * 1024 * 1024  # 官方限制：音源样本 ≤ 10MB
MIMO_SAMPLE_MIME = {".wav": "audio/wav", ".mp3": "audio/mpeg"}

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

# MiMo 风格标签：主页「情感」下拉值 → (风格) 标签（MiMo 支持自定义风格，未列出的原样透传）
MIMO_EMOTION_TAGS: Dict[str, str] = {
    "开心": "开心",
    "伤心": "悲伤",
    "生气": "愤怒",
    "害怕": "恐惧",
    "惊讶": "惊讶",
    "讨厌": "冷漠",
    "哭泣": "哽咽",
    "抱歉": "愧疚",
    "平静": "平静",
    "播音": "播音",
    "讲故事": "讲故事",
}

# 主页「情感」「情感强度」下拉的特殊选项值（SDK 的下拉直接显示 Literal 值，故用中文）
EMOTION_AUTO = "麦麦自主"  # LLM 按对话氛围现场挑
EMOTION_NONE = "无"        # 不带情感（正常语气）


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


def detect_language(text: str) -> str:
    """按字符占比粗略判断文本是否以日语/韩语为主，返回火山 ``explicit_language`` 取值。

    返回 ``""`` 表示"不指定"（交给火山默认处理）。仅覆盖 ja/ko 两类：

      · 日语文本 **不传** explicit_language → 发音明显退化，须传 ``ja``（上游实测）；
      · 韩语文本同理传 ``ko``；
      · 中英文（不传）→ 官方默认处理正常，无需指定；其他语种罕见，不做检测。

    ⚠️ 官方文档警告：启用 explicit_language 后"仅朗读指定语种的文本，其他语种的
    内容会被跳过或合成失败"。所以这里必须判断**主体语种**而不是"是否含有"——
    中文里夹几个假名/谚文时绝不能整段指定该语种，否则中文内容会被跳过。
    """
    if not text:
        return ""

    def count(lo: str, hi: str) -> int:
        return sum(1 for ch in text if lo <= ch <= hi)

    han = count("\u4e00", "\u9fff")      # 中日韩汉字（中文与日语汉字共用）
    kana = count("\u3040", "\u30ff")     # 平假名 / 片假名
    hangul = count("\uac00", "\ud7af")   # 谚文

    # 日语：存在假名，且假名数量超过汉字（日语语法成分全是假名，正文假名必然多于汉字；
    # 中文夹少量假名时汉字远多于假名，不会误判）。判不出时退回默认处理，安全。
    if kana > 0 and kana > han:
        return "ja"
    # 韩语：谚文字符多于汉字即视为以韩语为主
    if hangul > 0 and hangul > han:
        return "ko"
    return ""


# ─── 配置模型 ────────────────────────────────────────────────────────────────


class PluginSectionConfig(PluginConfigBase):
    """插件主页配置：只放开关与下拉选择，具体数值/密钥到各引擎页签改。"""

    __ui_label__ = "插件主页"
    __ui_icon__ = "package"
    __ui_order__ = 0

    # ── 兼容旧配置的归一化（下拉值已全中文化，旧英文值自动映射）─────────
    @field_validator("tts_engine", mode="before")
    @classmethod
    def _normalize_tts_engine(cls, v: Any) -> Any:
        """旧配置 doubao/mimo 英文值 → 中文选项值。"""
        s = str(v or "").strip().lower()
        if "mimo" in s:
            return "MiMo 语音"
        return "豆包语音"

    @field_validator("emotion", mode="before")
    @classmethod
    def _normalize_emotion(cls, v: Any) -> Any:
        """旧配置空串/"none" → "无"；"auto" → "麦麦自主"。"""
        s = str(v or "").strip()
        if s in ("", "none"):
            return EMOTION_NONE
        if s == "auto":
            return EMOTION_AUTO
        return v

    @field_validator("emotion_scale", mode="before")
    @classmethod
    def _normalize_emotion_scale(cls, v: Any) -> Any:
        """旧配置数值档位 → 字符串；"auto" → "麦麦自主"。"""
        if v is None:
            return EMOTION_AUTO
        if isinstance(v, (int, float)):
            return str(int(v)) if 1 <= int(v) <= 5 else EMOTION_AUTO
        s = str(v).strip()
        return EMOTION_AUTO if s in ("", "auto") else s

    @field_validator("auto_voice_mode", mode="before")
    @classmethod
    def _normalize_auto_voice_mode(cls, v: Any) -> Any:
        """旧配置 llm/probability/off 英文值 → 中文选项值。"""
        s = str(v or "").strip().lower()
        if not s or "llm" in s:
            return "LLM 自行判断"
        if "概率" in s or s == "probability":
            return "概率触发"
        if "手动" in s or "关闭" in s or s == "off":
            return "仅手动"
        return v

    @field_validator("voice_content_source", mode="before")
    @classmethod
    def _normalize_voice_content_source(cls, v: Any) -> Any:
        """手填英文值 reply/replyer → 回复生成；其余 → 工具文本。"""
        s = str(v or "").strip().lower()
        return "回复生成" if ("reply" in s or "回复" in s) else "工具文本"

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
    tts_engine: Literal["豆包语音", "MiMo 语音"] = Field(
        default="豆包语音",
        description="语音引擎：豆包语音=火山（官方预置音色库 + 复刻音色）；MiMo 语音=小米音色复刻（mimo-v2.5-tts-voiceclone，音源放插件 src/）",
        json_schema_extra={
            "label": "语音引擎",
            "hint": "切换引擎后，到对应页签填 API Key 等连接配置",
        },
    )
    emotion: Literal["麦麦自主", "无", "开心", "伤心", "生气", "害怕", "惊讶", "讨厌", "哭泣", "抱歉", "平静", "播音", "讲故事"] = Field(
        default="麦麦自主",
        description="情感：麦麦自主=LLM 按对话氛围现场挑（推荐）；选具体情感=固定用它（手动 /说 与自主语音一致）；无=正常语气。豆包走情感参数；MiMo 转成 (风格) 标签",
        json_schema_extra={
            "label": "情感",
            "hint": "麦麦自主=LLM 现场挑｜无=正常语气｜其余=固定情感",
        },
    )
    command_enabled: bool = Field(
        default=True,
        description=(
            "是否启用 /说 /语音 手动命令。"
            "只影响手动命令；麦麦自主语音由「自主语音方式」（auto_voice_mode）控制"
        ),
        json_schema_extra={"label": "手动命令", "hint": "只影响 /说 /语音 手动命令；麦麦自主语音由下方「自主语音方式」控制"},
    )
    auto_voice_mode: Literal["LLM 自行判断", "概率触发", "仅手动"] = Field(
        default="LLM 自行判断",
        description="麦麦自主语音方式：LLM 自行判断=由 LLM 决定何时用语音（推荐）；概率触发=每条消息按概率掷骰，命中则本轮回复转语音（频率在「高级」页的概率值控制）；仅手动=只有 /说 /语音 命令才发声",
        json_schema_extra={
            "label": "自主语音方式",
            "hint": "LLM 自行判断（推荐）｜概率触发｜仅手动",
        },
    )
    voice_content_source: Literal["工具文本", "回复生成"] = Field(
        default="工具文本",
        description=(
            "麦麦自主语音（doubao_tts_speak 工具）的内容来源："
            "工具文本=LLM 调用工具时把要说的内容作为 text 传入，插件直接朗读（现状）；"
            "回复生成=LLM 只决定“本轮用语音回复”，具体说什么由麦麦的 reply 流程生成，"
            "插件在发送时自动把整条回复逐段转成语音"
        ),
        json_schema_extra={
            "label": "工具语音内容来源",
            "hint": "工具文本=LLM 传 text 直接朗读（现状）｜回复生成=LLM 只决定用语音，说什么由 reply 生成后自动转语音",
        },
    )
    emotion_scale: Literal["麦麦自主", "1", "2", "3", "4", "5"] = Field(
        default="麦麦自主",
        description="情感强度 1~5（豆包专用）：麦麦自主=LLM 按情感挑档位（推荐，LLM 未给时不下发）；固定档位=1 最淡…5 最浓",
        json_schema_extra={
            "label": "情感强度（豆包）",
            "hint": "麦麦自主=LLM 挑｜1=最淡 … 5=最浓；仅「情感」生效时随情感一起下发",
        },
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
            "把合成结果缓存到本地：同样的文本+音色+情感直接复用音频，"
            "不重复调用接口（省钱、省等待）"
        ),
        json_schema_extra={"label": "本地合成缓存", "hint": "同文本同音色直接复用本地音频，不再调 TTS 接口；缓存目录等在「高级」页"},
    )


class DoubaoSectionConfig(PluginConfigBase):
    """豆包语音（火山引擎）连接与音色配置。"""

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
    voice: str = Field(
        default=DEFAULT_VOICE_DISPLAY,
        description="音色：预置音色显示名（见 README 音色表）或直接填 voice_type 音色 ID",
        json_schema_extra={
            "label": "音色",
            "placeholder": "小何 2.0 / Vivi 2.0 / 云舟 2.0 或 voice_type ID",
            "hint": "常用预置音色：小何 2.0、Vivi 2.0、爽快思思 2.0、甜美小源 2.0、云舟 2.0…完整列表见 README",
        },
    )
    speech_rate: float = Field(
        default=0.0,
        description="语速 -50~100（0=正常，豆包专用）。火山固定连续数值，麦麦不可自主，仅手动设置",
        json_schema_extra={"label": "语速（豆包·仅手动）", "hint": "-50~100，0=正常；此值麦麦不能自主调节"},
    )
    loudness: float = Field(
        default=0.0,
        description="音量 -50~100（0=正常，豆包专用）。火山固定连续数值，麦麦不可自主，仅手动设置",
        json_schema_extra={"label": "音量（豆包·仅手动）", "hint": "-50~100，0=正常；此值麦麦不能自主调节"},
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


class MimoSectionConfig(PluginConfigBase):
    """小米 MiMo 语音（音色复刻）连接与音源配置。"""

    __ui_label__ = "MiMo 语音"
    __ui_icon__ = "mic"
    __ui_order__ = 2

    api_key: str = Field(
        default="",
        description="MiMo 开放平台 API Key（mimo.mi.com → API Key 管理）。敏感信息，勿随插件分发、勿提交到仓库",
        json_schema_extra={
            "label": "API Key",
            "secret": True,
            "placeholder": "sk-...",
            "hint": "获取：MiMo 开放平台（mimo.mi.com）→ API Key；敏感信息，勿外传",
        },
    )
    voice_sample: str = Field(
        default=MIMO_VOICE_SAMPLE_DEFAULT,
        description="复刻音源文件：默认 dxl.wav，相对路径自动在插件 src/ 目录下找；也可填绝对路径。仅支持 wav/mp3，≤10MB",
        json_schema_extra={
            "label": "复刻音源文件",
            "placeholder": "dxl.wav",
            "example": "dxl.wav",
            "hint": "相对路径默认在插件 src/ 文件夹里找（默认 dxl.wav）；换音色就把 wav/mp3 放进 src/ 或填绝对路径",
        },
    )
    style: str = Field(
        default="",
        description="自然语言风格指令（可选）：如「语速稍快，声音明亮，语气亲切自然」，作为 user 指令传给 MiMo 调整语气",
        json_schema_extra={
            "label": "风格指令（可选）",
            "placeholder": "例：语速稍快，声音明亮，语气亲切自然",
            "hint": "留空=不传；主页「情感」下拉会转成 (风格) 标签叠加生效",
        },
    )
    audio_format: str = Field(
        default="wav",
        description="音频编码格式（MiMo 固定 wav，勿改）",
        json_schema_extra={"label": "音频格式", "hidden": True},
    )


class BehaviorSectionConfig(PluginConfigBase):
    """高级配置：数值与路径类（主页只放开关与下拉，这里收纳其余细项）。"""

    __ui_label__ = "高级（数值与路径）"
    __ui_icon__ = "sliders"
    __ui_order__ = 3

    command_cooldown_seconds: float = Field(
        default=10.0,
        ge=0.0,
        description=(
            "同一会话两次语音合成的最小间隔（秒），0=不限流。"
            "防止刷屏与刷费用；对手动命令与麦麦自主 Tool 调用一并生效"
        ),
        json_schema_extra={"label": "语音冷却（秒）", "hint": "同一会话两次合成语音的最小间隔，0=不限流；命令与自主语音都受限"},
    )
    auto_voice_probability: float = Field(
        default=0.1,
        description="概率模式的触发概率 0~1（0.1=平均每 10 轮约 1 轮语音；0=关）。仅主页「自主语音方式」=probability 时生效",
        json_schema_extra={"label": "语音概率", "hint": "0~1；0.1=平均每 10 轮约 1 轮语音，仅「概率触发」模式下生效"},
    )
    timeout_seconds: float = Field(
        default=30.0,
        description="请求 TTS 接口超时（秒，豆包/MiMo 通用）",
        json_schema_extra={"label": "超时（秒）", "hint": "单次合成请求的最长等待时间"},
    )
    max_text_length: int = Field(
        default=150,
        description="单条语音最大文本长度（超过按句切分多条发送）",
        json_schema_extra={"label": "单条最大字数", "hint": "超过就按句子切成多条语音依次发"},
    )
    cache_dir: str = Field(
        default="",
        description="缓存目录；留空 = 宿主注入的插件数据目录下的 doubao-tts-cache/。建议填绝对路径",
        json_schema_extra={"label": "缓存目录", "hint": "留空 = 宿主插件数据目录下的 doubao-tts-cache/，建议填绝对路径"},
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


class DebugSectionConfig(PluginConfigBase):
    """调试配置。"""

    __ui_label__ = "调试"
    __ui_icon__ = "terminal"
    __ui_order__ = 4

    enabled: bool = Field(
        default=False,
        description="输出诊断日志（工具与命令调用细节）",
        json_schema_extra={
            "label": "诊断日志",
            "hint": "开=工具/命令调用时输出参数摘要（日志搜「豆包TTS·调试」）；排查触发问题时打开",
        },
    )


class ToolInfoBaseConfig(PluginConfigBase):
    """工具信息基类（只读展示：LLM 视角的工具定义，加载时自动写入）。

    WebUI 对字段的显示值取自配置值本身（schema.default 会被空配置值覆盖），
    展示文本由 on_load 写入 config.toml 对应段；读取处忽略这些字段（纯展示）。
    """

    __ui_icon__ = "wrench"
    __ui_order__ = 10

    visibility: str = Field(
        default="",
        description="工具对 LLM 的可见性（运行时生成，只读）",
        json_schema_extra={
            "label": "可见性",
            "hint": "visible = 始终提供给 LLM；deferred = 按需发现（可被 tool_search 搜到）",
            "disabled": True,
            "rows": 2,
        },
    )
    description: str = Field(
        default="",
        description="LLM 看到的工具描述（运行时生成，只读）",
        json_schema_extra={
            "label": "描述",
            "hint": "LLM 实际看到的工具描述；每次插件加载时自动刷新",
            "disabled": True,
            "rows": 5,
        },
    )
    parameters: str = Field(
        default="",
        description="工具参数清单（运行时生成，只读）",
        json_schema_extra={
            "label": "参数",
            "hint": "每个参数一行：名称（类型，必填/可选）：说明",
            "disabled": True,
            "rows": 5,
        },
    )


class ToolDoubaoTtsSpeakConfig(ToolInfoBaseConfig):
    """语音工具（doubao_tts_speak）。"""

    __ui_label__ = "doubao_tts_speak"


class CommandInfoBaseConfig(PluginConfigBase):
    """命令信息基类（只读展示：命令描述与匹配模式）。"""

    __ui_icon__ = "terminal"
    __ui_order__ = 11

    description: str = Field(
        default="",
        description="命令描述（运行时生成，只读）",
        json_schema_extra={
            "label": "描述",
            "hint": "命令的说明文本；每次插件加载时自动刷新",
            "disabled": True,
            "rows": 2,
        },
    )
    pattern: str = Field(
        default="",
        description="命令匹配模式（运行时生成，只读）",
        json_schema_extra={
            "label": "匹配模式",
            "hint": "触发该命令的正则模式",
            "disabled": True,
            "rows": 2,
        },
    )


class CommandDoubaoTtsSayConfig(CommandInfoBaseConfig):
    """说话命令（doubao_tts_say）。"""

    __ui_label__ = "doubao_tts_say"


class CommandDoubaoTtsHelpConfig(CommandInfoBaseConfig):
    """帮助命令（doubao_tts_help）。"""

    __ui_label__ = "doubao_tts_help"


def _collect_tool_info(handler: Any) -> Dict[str, str]:
    """从组件声明生成单个工具的展示字段（可见性 / 描述 / 参数）。"""
    info = getattr(handler, "__maibot_component_info__", None)
    if info is None:
        return {}
    metadata = getattr(info, "metadata", None)
    visibility = ""
    if isinstance(metadata, dict):
        visibility = str(metadata.get("visibility") or "").strip()
    description = str(
        getattr(info, "brief_description", "") or getattr(info, "description", "") or ""
    ).strip() or "（无描述）"
    parameters = getattr(info, "parameters", None) or []
    param_lines: List[str] = []
    for param in parameters:
        param_name = str(getattr(param, "name", "") or "")
        param_type = getattr(param, "param_type", None)
        type_text = (
            getattr(param_type, "value", None)
            or getattr(param_type, "name", None)
            or "string"
        )
        required = "必填" if bool(getattr(param, "required", False)) else "可选"
        param_desc = str(getattr(param, "description", "") or "")
        param_lines.append(f"{param_name}（{type_text}，{required}）: {param_desc}")
    return {
        "visibility": visibility or "deferred（未显式声明时的宿主默认）",
        "description": description,
        "parameters": "\n".join(param_lines) if param_lines else "（无参数）",
    }


def _collect_command_info(handler: Any) -> Dict[str, str]:
    """从组件声明生成单个命令的展示字段（描述 / 匹配模式）。"""
    info = getattr(handler, "__maibot_component_info__", None)
    if info is None:
        return {}
    return {
        "description": str(getattr(info, "description", "") or "").strip()
        or "（无描述）",
        "pattern": str(getattr(info, "command_pattern", "") or "").strip() or "（无）",
    }


def _collect_all_component_info() -> Dict[str, Dict[str, str]]:
    """收集全部组件的展示字段（段名 → 字段字典）。"""
    return {
        "tool_doubao_tts_speak": _collect_tool_info(DoubaoTTSPlugin._tool_speak),
        "command_doubao_tts_say": _collect_command_info(DoubaoTTSPlugin._cmd_say),
        "command_doubao_tts_help": _collect_command_info(DoubaoTTSPlugin._cmd_help),
    }


def _sync_component_info_sections(
    values: Dict[str, Dict[str, Any]], config_path: Optional[Path] = None
) -> None:
    """把只读展示字段写入 config.toml 对应段（每段内容有变化才写）。

    实现与 reply-control 一致：段内容完全由本函数管理（整段重写），文本用
    JSON 转义（TOML 基础字符串兼容）；失败静默（不影响插件运行）。
    """
    if not values:
        return
    try:
        target = config_path or (Path(__file__).parent / "config.toml")
        if not target.exists():
            return
        content = target.read_text(encoding="utf-8")
        original = content
        for section, fields in values.items():
            if not fields:
                continue
            body = [
                f"{name} = " + json.dumps(value, ensure_ascii=False)
                for name, value in fields.items()
            ]
            content = _replace_section_body(content, section, body)
        if content != original:
            target.write_text(content, encoding="utf-8")
    except Exception:
        pass  # 展示同步失败不影响插件运行


def _replace_section_body(content: str, section: str, body: List[str]) -> str:
    """重写 TOML 指定段的段体（段不存在时追加）；内容未变化时原样返回。"""
    lines = content.splitlines()
    start: Optional[int] = None
    end = len(lines)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == f"[{section}]":
            start = index
            continue
        if start is not None and stripped.startswith("[") and stripped.endswith("]"):
            end = index
            break
    trailing = "\n" if content.endswith("\n") else ""
    if start is None:
        suffix = "" if content.endswith("\n") else "\n"
        return f"{content}{suffix}\n[{section}]\n" + "\n".join(body) + "\n"
    current = [line for line in lines[start + 1 : end] if line.strip()]
    if current == body:
        return content  # 未变化：不写盘、不触发配置事件
    if end >= len(lines):
        return "\n".join([*lines[: start + 1], *body]) + trailing
    return "\n".join([*lines[: start + 1], *body, "", *lines[end:]]) + trailing


class DoubaoTTSRootConfig(PluginConfigBase):
    """插件根配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig, json_schema_extra={"label": "插件主页"})
    doubao: DoubaoSectionConfig = Field(default_factory=DoubaoSectionConfig, json_schema_extra={"label": "豆包语音"})
    mimo: MimoSectionConfig = Field(default_factory=MimoSectionConfig, json_schema_extra={"label": "MiMo 语音"})
    behavior: BehaviorSectionConfig = Field(default_factory=BehaviorSectionConfig, json_schema_extra={"label": "高级"})
    debug: DebugSectionConfig = Field(default_factory=DebugSectionConfig)
    tool_doubao_tts_speak: ToolDoubaoTtsSpeakConfig = Field(default_factory=ToolDoubaoTtsSpeakConfig)
    command_doubao_tts_say: CommandDoubaoTtsSayConfig = Field(default_factory=CommandDoubaoTtsSayConfig)
    command_doubao_tts_help: CommandDoubaoTtsHelpConfig = Field(default_factory=CommandDoubaoTtsHelpConfig)


# ─── 主插件 ──────────────────────────────────────────────────────────────────


class DoubaoTTSPlugin(MaiBotPlugin):
    """豆包/MiMo 语音合成插件：文字转语音，让麦麦开口说话。"""

    config_model = DoubaoTTSRootConfig

    # ── WebUI 配置页标签布局 ────────────────────────────────────────────
    # SDK 默认 layout=auto（所有 section 堆叠在一页）；这里覆盖 build_config_schema，
    # 把 layout 改写为 tabs——主页/豆包语音/MiMo 语音/高级 各占一个可切换页签。
    # WebUI 端按 schema.layout.type === "tabs" 渲染 Tabs（见 dashboard plugin-config.tsx）。
    @classmethod
    def build_config_schema(
        cls,
        *,
        plugin_id: str = "",
        plugin_name: str = "",
        plugin_version: str = "",
        plugin_description: str = "",
        plugin_author: str = "",
    ) -> Dict[str, Any]:
        schema = super().build_config_schema(
            plugin_id=plugin_id,
            plugin_name=plugin_name,
            plugin_version=plugin_version,
            plugin_description=plugin_description,
            plugin_author=plugin_author,
        )
        if isinstance(schema, dict) and schema.get("sections"):
            schema["layout"] = {
                "type": "tabs",
                "tabs": [
                    {"id": "main", "title": "主页", "sections": ["plugin"], "order": 0},
                    {"id": "doubao", "title": "豆包语音", "sections": ["doubao"], "order": 1},
                    {"id": "mimo", "title": "MiMo 语音", "sections": ["mimo"], "order": 2},
                    {"id": "advanced", "title": "高级", "sections": ["behavior"], "order": 3},
                    {
                        "id": "debug",
                        "title": "调试",
                        "sections": [
                            "debug",
                            "tool_doubao_tts_speak",
                            "command_doubao_tts_say",
                            "command_doubao_tts_help",
                        ],
                        "order": 4,
                    },
                ],
            }
            # 组件信息卡：字段 default 注入（双保险；框内值由 on_load 写入配置值）
            sections = schema.get("sections")
            for section_name, info_fields in _collect_all_component_info().items():
                if not isinstance(sections, dict):
                    break
                section = sections.get(section_name)
                if not isinstance(section, dict):
                    continue
                section_fields = section.get("fields")
                if not isinstance(section_fields, dict):
                    continue
                for field_name, value in info_fields.items():
                    field = section_fields.get(field_name)
                    if isinstance(field, dict):
                        field["default"] = value
        return schema

    def __init__(self) -> None:
        super().__init__()
        # 会话 → 命中时间戳（该会话麦麦下一条文字回复将转语音）：
        # 来源有两处——概率模式掷骰命中，或「工具语音内容来源=回复生成」时 Tool 打的标
        self._pending_voice: Dict[str, float] = {}
        # 会话 → 本次待语音的合成覆盖参数（emotion/emotion_scale）：
        # 仅 Tool「回复生成」模式会写入；存在该键即表示标记来自回复生成模式
        self._pending_voice_overrides: Dict[str, Dict[str, Any]] = {}
        # 会话集合：插件自身发的提示/帮助/降级文本，不参与"本轮转语音"替换
        self._suppress_convert_streams: set = set()
        # 防递归：正在处理"待语音"消息的标记
        self._sending_pending_voice: bool = False
        # 会话 → 上次语音合成时间戳（冷却限流，防刷费用/刷屏）
        self._last_speech_at: Dict[str, float] = {}
        # 会话 → 上次失败提示时间戳（30 秒去重，防 LLM 重试导致提示刷屏）
        self._last_error_prompt_at: Dict[str, float] = {}
        # MiMo 音源缓存：(文件路径, mtime_ns, dataURL)——文件没变就不重复读盘/编码
        self._mimo_sample_cache: Optional[Tuple[str, int, str]] = None

    # ── 配置读取 ────────────────────────────────────────────────────────

    def _get(self, section: str, key: str, default: Any = None) -> Any:
        try:
            return getattr(getattr(self.config, section, None), key, default)
        except Exception:
            return default

    def _engine(self) -> str:
        """当前语音引擎：doubao（默认）/ mimo。兼容中英文配置值。"""
        try:
            raw = str(self._get("plugin", "tts_engine", "") or "").strip().lower()
        except Exception:
            return "doubao"
        return "mimo" if "mimo" in raw else "doubao"

    def _engine_name(self) -> str:
        return "MiMo" if self._engine() == "mimo" else "豆包"

    def _doubao_api_key(self) -> str:
        return str(self._get("doubao", "api_key", "") or "").strip()

    def _mimo_api_key(self) -> str:
        return str(self._get("mimo", "api_key", "") or "").strip()

    def _active_api_key(self) -> str:
        """当前引擎的 API Key。"""
        return self._mimo_api_key() if self._engine() == "mimo" else self._doubao_api_key()

    # ── MiMo 音色复刻音源 ───────────────────────────────────────────────

    def _mimo_voice_data_url(self) -> Tuple[Optional[str], str]:
        """把配置的音源文件读成 DataURL（data:audio/xxx;base64,...）。

        相对路径依次在插件 src/ 目录、插件根目录、当前工作目录下找；
        绝对路径直接用。带 mtime 缓存，文件没变不重复读盘。

        Returns:
            (dataURL 或 None, 音源文件名或错误说明)
        """
        raw = str(self._get("mimo", "voice_sample", MIMO_VOICE_SAMPLE_DEFAULT) or "").strip() or MIMO_VOICE_SAMPLE_DEFAULT
        p = Path(raw)
        candidates = [p] if p.is_absolute() else [PLUGIN_SRC_DIR / raw, PLUGIN_DIR / raw, p]
        path = next((c for c in candidates if c.is_file()), None)
        if path is None:
            return None, f"{raw}（把音源文件放进插件 src/ 目录，或填绝对路径）"
        mime = MIMO_SAMPLE_MIME.get(path.suffix.lower())
        if not mime:
            return None, f"{path.name}（不支持的格式 {path.suffix}，仅支持 wav/mp3）"
        try:
            stat = path.stat()
            cached = self._mimo_sample_cache
            if cached and cached[0] == str(path) and cached[1] == stat.st_mtime_ns:
                return cached[2], path.name
            data = path.read_bytes()
        except Exception as exc:  # noqa: BLE001
            return None, f"{path.name}（读取失败: {exc}）"
        if len(data) > MIMO_MAX_SAMPLE_BYTES:
            return None, f"{path.name}（超过官方 10MB 上限，请裁剪音源）"
        data_url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        self._mimo_sample_cache = (str(path), stat.st_mtime_ns, data_url)
        return data_url, path.name

    # ── 冷却限流 ────────────────────────────────────────────────────────

    def _cooldown_seconds(self) -> float:
        try:
            return max(0.0, float(self._get("behavior", "command_cooldown_seconds", 10) or 0))
        except (TypeError, ValueError):
            return 10.0

    def _cooldown_block(self, stream_id: str) -> bool:
        """冷却限流：同一会话在冷却窗口内拒绝再次合成（防刷费用/刷屏）。

        返回 True=本次被拦截。通过检查时会记录本次时间戳。
        对手动命令与麦麦自主 Tool 调用一并生效（概率模式由宿主驱动、每轮最多一次，不在此列）。
        """
        cd = self._cooldown_seconds()
        if cd <= 0:
            return False
        now = time.time()
        last = self._last_speech_at.get(stream_id)
        if last is not None and now - last < cd:
            return True
        self._last_speech_at[stream_id] = now
        if len(self._last_speech_at) > 256:  # 防累积：清掉早已冷却完毕的会话
            cutoff = now - max(cd, 60.0)
            self._last_speech_at = {k: v for k, v in self._last_speech_at.items() if v >= cutoff}
        return False

    # ── 概率自主语音 ────────────────────────────────────────────────────

    def _auto_voice_mode(self) -> str:
        """麦麦自主语音方式：llm / probability / off（兼容中英文配置值）。"""
        try:
            raw = str(self._get("plugin", "auto_voice_mode", "") or "").strip().lower()
        except Exception:
            return "llm"
        if "概率" in raw or raw == "probability":
            return "probability"
        if "手动" in raw or "关闭" in raw or raw == "off":
            return "off"
        return "llm"

    def _fixed_emotion(self, overrides: Dict[str, Any]) -> str:
        """解析本次合成使用的情感（空串=不带固定情感）。

        LLM 覆盖（overrides.emotion）优先；否则取主页「情感」固定值，
        仅当它是具体情感（非"麦麦自主"/"无"）时返回。
        """
        emotion = str(overrides.get("emotion") or "").strip()
        if not emotion or emotion == EMOTION_NONE:
            fixed = str(self._get("plugin", "emotion", "") or "").strip()
            if fixed and fixed not in (EMOTION_NONE, EMOTION_AUTO, "none", "auto"):
                return fixed
            return ""
        return emotion

    def _fixed_emotion_scale(self, overrides: Dict[str, Any]) -> Optional[float]:
        """解析本次合成使用的情感强度 1~5（豆包专用）。

        LLM 覆盖（overrides.emotion_scale）优先；否则取主页「情感强度」固定档位；
        "麦麦自主"且 LLM 未给时返回 None=不下发该参数。
        """
        raw = overrides.get("emotion_scale")
        if raw is None:
            raw = str(self._get("plugin", "emotion_scale", "") or "").strip()
            if not raw or raw == EMOTION_AUTO:
                return None
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return None
        return val if 1.0 <= val <= 5.0 else None

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

    def _voice_content_source(self) -> str:
        """工具语音内容来源：text=LLM 传 text 直接朗读；reply=内容由 reply 生成后转语音。"""
        try:
            raw = str(self._get("plugin", "voice_content_source", "") or "").strip().lower()
        except Exception:
            return "text"
        return "reply" if ("reply" in raw or "回复" in raw) else "text"

    def _is_pending_stream(self, stream_id: str) -> bool:
        """判断某会话是否处于"待语音"状态（麦麦下一条文字回复转语音）。"""
        ts = self._pending_voice.get(stream_id)
        if ts is None:
            return False
        if time.time() - ts > 300:  # 5 分钟窗口：命中后麦麦迟迟没回复则作废
            self._pending_voice.pop(stream_id, None)
            self._pending_voice_overrides.pop(stream_id, None)
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
        description="收到普通用户消息时：清理上一轮残留的语音标记，概率模式下按概率标记该会话本轮语音",
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def _on_inbound_message(self, **kwargs: Any) -> None:
        """入站钩子：新一轮先清残留标记，再按概率掷骰打"待语音"标。"""
        message = kwargs.get("message")
        if not isinstance(message, dict):
            return
        # 仅当用户主动发消息（排除命令、通知、系统类）
        if message.get("is_command") or message.get("is_notify"):
            return
        text, session_id = await self._extract_message_text(message)
        if not text or not session_id:
            return
        # 新一轮开始：先清掉上一轮可能残留的标记。
        # 跟随分段模式下标记会保留到本轮所有分段发完，所以必须在这里归零；
        # 「回复生成」模式的标记若 planner 最终没调用 reply，也在这里作废。
        self._pending_voice.pop(session_id, None)
        self._pending_voice_overrides.pop(session_id, None)
        if not self._probability_enabled():
            return
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
        """出站钩子：麦麦要发文字且该会话被标记"待语音" → 原地换成语音。"""
        if self._sending_pending_voice:
            return {"action": "continue"}
        message = kwargs.get("message")
        if not isinstance(message, dict):
            return {"action": "continue"}
        session_id = str(message.get("session_id") or "").strip()
        if not session_id or session_id in self._suppress_convert_streams:
            return {"action": "continue"}
        if not self._is_pending_stream(session_id):
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
        # 「回复生成」模式的标记：合成覆盖参数随标记保存；该模式要求整条回复转语音，
        # 所以不受「跟随分段」开关限制（reply 分段发送时每段各自转语音），
        # 否则默认只念第一段、其余分段仍是文字，与"语音回复"的预期不符。
        by_reply_tool = session_id in self._pending_voice_overrides
        overrides = dict(self._pending_voice_overrides.get(session_id) or {})
        follow = bool(self._get("plugin", "follow_segmentation", False)) or by_reply_tool
        if not follow:
            # 不跟随分段：只转第一条，消费掉标记
            self._pending_voice.pop(session_id, None)
            self._pending_voice_overrides.pop(session_id, None)
        self.ctx.logger.info(
            "[豆包TTS] %s：会话 %s 文字回复 → 语音（%d字%s）",
            "回复生成" if by_reply_tool else "概率语音",
            session_id, len(text), "，跟随分段" if follow else "",
        )
        self._sending_pending_voice = True
        try:
            # 统一用「原地替换」而不是中止原消息（v1.4.5）。
            # 旧做法 abort 会让麦麦的 reply 工具拿到"发送失败"，planner/LLM 便会重试发送，
            # 语音和文字就会反复出现；替换则让发送链正常走完，一次成功。
            return await self._replace_with_voice(message, text, kwargs, overrides=overrides)
        finally:
            self._sending_pending_voice = False

    async def _replace_with_voice(
        self,
        message: Dict[str, Any],
        text: str,
        kwargs: Dict[str, Any],
        overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """把一条待发的文字消息原地换成语音消息。

        与「中止原消息 + 自己另发」不同，这里让原消息继续走完发送流程：
        分段插件的补发、宿主的后处理都不会被打断，语音也就跟着文本一段一段发。
        合成失败时返回 continue，让原文照常发出，不会丢内容。

        overrides: 本次语音的合成覆盖参数（emotion/emotion_scale），
                   来自 Tool「回复生成」时 planner 指定的语气；缺省=用配置固定值。
        """
        if not self._active_api_key():
            self.ctx.logger.warning("[豆包TTS] 未配置 API Key，本条保持文字")
            return {"action": "continue"}

        max_len = max(1, int(self._get("behavior", "max_text_length", 150) or 150))
        segments = _split_sentences(text, max_len)
        if not segments:
            return {"action": "continue"}

        session_id = str(message.get("session_id") or "").strip()
        voice_segments: List[Dict[str, Any]] = []
        for seg in segments:
            success, audio, info = await self._synthesize_one(seg, overrides=overrides)
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
        if self._get("plugin", "sync_chat_context", True):
            await self._append_chat_context(session_id, text)
        return {"action": "continue", "modified_kwargs": {**kwargs, "message": new_message}}

    # ── 生命周期 ────────────────────────────────────────────────────────

    async def on_load(self) -> None:
        engine = self._engine()
        key_state = "已配置" if self._active_api_key() else "未配置"
        if engine == "mimo":
            _, vinfo = self._mimo_voice_data_url()
            detail = f" | 复刻音源: {vinfo}"
        else:
            detail = (
                f" | 音色: {self._get('doubao', 'voice', DEFAULT_VOICE_DISPLAY)}"
                f" | resource: {self._get('doubao', 'resource_id', DOUBAO_RESOURCE_PRESET)}"
            )
        self.ctx.logger.info(
            "[豆包TTS] 插件已加载 | 引擎: %s | API Key: %s%s",
            self._engine_name(), key_state, detail,
        )
        if self._voice_content_source() == "reply":
            self.ctx.logger.info(
                "[豆包TTS] 工具语音内容来源=回复生成：LLM 调用工具后由 reply 生成内容，发送时自动整条转语音"
            )
        if not self._active_api_key():
            self.ctx.logger.warning(
                "[豆包TTS] 尚未配置 API Key：请在插件配置「%s」页填写",
                "MiMo 语音" if engine == "mimo" else "豆包语音",
            )
        # 组件信息只读展示：WebUI 取值依赖配置值本身，加载时同步一次
        # （内容有变化才写盘；纯展示字段，读取处忽略）
        _sync_component_info_sections(_collect_all_component_info())

    async def on_unload(self) -> None:
        self.ctx.logger.info("[豆包TTS] 插件已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        self.ctx.logger.info("[豆包TTS] 配置更新 scope=%s version=%s", scope, version)

    # ── 本地合成缓存 ────────────────────────────────────────────────────

    def _cache_dir(self) -> Optional[Path]:
        """缓存目录；未开启或建不出来时返回 None（等于禁用缓存）。

        留空时默认用宿主注入的持久化数据目录（ctx.paths.data_dir）下的
        doubao-tts-cache/——运行时数据不写启动目录；拿不到该目录时退回
        相对路径旧行为。
        """
        if not bool(self._get("plugin", "cache_enabled", False)):
            return None
        raw = str(self._get("behavior", "cache_dir", "") or "").strip()
        if raw:
            base = Path(raw)
        else:
            data_dir = getattr(getattr(self.ctx, "paths", None), "data_dir", None)
            base = Path(str(data_dir)) / "doubao-tts-cache" if data_dir else Path("doubao-tts-cache")
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
        if self._engine() == "mimo":
            return "wav"
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
        """合成一段音频：按主页「语音引擎」分流到豆包 / MiMo。

        Args:
            text: 要合成的文本
            overrides: 调用方按参数独立覆盖（key 可选 emotion/emotion_scale；
                       value=None 或缺省=用配置固定值）。供"麦麦自主"时 LLM 现场填。

        Returns:
            (ok, audio_bytes 或 b"", 错误信息或音色说明)
        """
        text = (text or "").strip()
        if not text:
            return False, b"", "文本为空"
        if self._engine() == "mimo":
            return await self._synthesize_mimo(text, overrides)
        return await self._synthesize_doubao(text, overrides)

    async def _synthesize_doubao(
        self, text: str, overrides: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, bytes, str]:
        """调用火山新版接口合成一段音频（引擎=doubao）。

        Args:
            text: 要合成的文本
            overrides: 调用方按参数独立覆盖（key 可选 emotion/emotion_scale；
                       value=None 或缺省=用配置固定值）。供"麦麦自主"时 LLM 现场填。

        Returns:
            (ok, audio_bytes 或 b"", 错误信息或音色ID)
        """
        api_key = self._doubao_api_key()
        if not api_key:
            return False, b"", "API Key 未配置（插件配置 → 豆包语音 → api_key）"

        ov = overrides if isinstance(overrides, dict) else {}

        resource_id = str(self._get("doubao", "resource_id", DOUBAO_RESOURCE_PRESET)).strip()
        voice_type = _resolve_voice_type(str(self._get("doubao", "voice", DEFAULT_VOICE_DISPLAY)))
        audio_format = str(self._get("doubao", "audio_format", "mp3")).strip() or "mp3"
        sample_rate = int(self._get("doubao", "sample_rate", 24000) or 24000)
        if audio_format == "ogg_opus":
            # 官方文档：ogg_opus 仅支持 48000 采样率
            sample_rate = 48000
        timeout = float(self._get("behavior", "timeout_seconds", 30.0) or 30.0)

        audio_params: Dict[str, Any] = {"format": audio_format, "sample_rate": sample_rate}
        req_params: Dict[str, Any] = {
            "text": text,
            "speaker": voice_type,
            "audio_params": audio_params,
        }
        # ⚠️ v1.6.2 修复（移植上游 v1.4.1）：官方接口要求 emotion / emotion_scale /
        #    speech_rate / loudness_rate 全部放在 req_params.audio_params 内。旧版误放在
        #    req_params 顶层、且语速/音量用了旧接口的字段名（speed_ratio / volume_ratio）
        #    与倍率值，会被服务端静默忽略 → 设置无效。
        # 情感：LLM 覆盖优先，否则用主页「情感」固定值（"麦麦自主"/"无"=不带固定情感）
        emotion_cfg = self._fixed_emotion(ov)
        if emotion_cfg:
            audio_params["emotion"] = PRESET_EMOTIONS.get(emotion_cfg, emotion_cfg)
            # 情感强度（豆包专用）：LLM 覆盖优先，否则主页「情感强度」固定档位
            scale = self._fixed_emotion_scale(ov)
            if scale is not None:
                audio_params["emotion_scale"] = scale
        # 语速：-50~100 的整数（0=正常，100=2.0 倍速）——官方字段名 speech_rate
        rate = float(self._get("doubao", "speech_rate", 0.0) or 0.0)
        if rate:
            audio_params["speech_rate"] = int(max(-50, min(100, round(rate))))
        # 音量：-50~100 的整数（0=正常，100=2.0 倍）——官方字段名 loudness_rate
        vol = float(self._get("doubao", "loudness", 0.0) or 0.0)
        if vol:
            audio_params["loudness_rate"] = int(max(-50, min(100, round(vol))))
        # additions（官方要求是 JSON 字符串）：
        #  · disable_markdown_filter：过滤麦麦回复里的 **加粗**、# 标题等，否则会被逐字念出来；
        #  · explicit_language：文本是日语等非中英语种时必须显式指定，否则发音明显退化（实测结论）。
        additions: Dict[str, Any] = {"disable_markdown_filter": True}
        detected_lang = detect_language(text)
        if detected_lang:
            additions["explicit_language"] = detected_lang
        req_params["additions"] = json.dumps(additions, ensure_ascii=False)

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
        self.ctx.logger.info(
            "[豆包TTS] 合成请求: %d字 | %s | %s | 情感=%s | 语速=%s | 音量=%s | 语种=%s",
            len(text), resource_id, voice_type,
            audio_params.get("emotion", "-"),
            audio_params.get("speech_rate", 0),
            audio_params.get("loudness_rate", 0),
            detected_lang or "默认（不指定）",
        )

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

    async def _synthesize_mimo(
        self, text: str, overrides: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, bytes, str]:
        """调用小米 MiMo 音色复刻接口合成一段音频（引擎=mimo）。

        接口为 OpenAI 兼容的 chat/completions：
        - ``assistant`` 消息 = 要合成的文本（可带 (风格) 标签）；
        - ``user`` 消息 = 可选的自然语言风格指令；
        - ``audio.voice`` = 音源样本 DataURL（每次请求随样本复刻，无需预注册）。

        Returns:
            (ok, audio_bytes 或 b"", 错误信息或音源名)
        """
        api_key = self._mimo_api_key()
        if not api_key:
            return False, b"", "API Key 未配置（插件配置 → MiMo 语音 → api_key）"
        voice_url, vinfo = self._mimo_voice_data_url()
        if not voice_url:
            return False, b"", f"复刻音源不可用: {vinfo}"

        ov = overrides if isinstance(overrides, dict) else {}

        # 情感 → (风格) 标签前缀：LLM 覆盖优先，否则用主页「情感」固定值
        emotion = self._fixed_emotion(ov)
        synth_text = text
        if emotion:
            tag = MIMO_EMOTION_TAGS.get(emotion, emotion)
            synth_text = f"({tag}){text}"

        style = str(self._get("mimo", "style", "") or "").strip()
        timeout = float(self._get("behavior", "timeout_seconds", 30.0) or 30.0)

        # 本地缓存：同样的文本+音源+风格直接复用上次的音频（键不含样本内容，含文件路径）
        cache_params: Dict[str, Any] = {
            "engine": "mimo",
            "model": MIMO_MODEL,
            "voice_sample": vinfo,
            "style": style,
            "emotion": emotion,
            "text": text,
        }
        cache_dir = self._cache_dir()
        cache_key = self._cache_key(cache_params) if cache_dir is not None else ""
        if cache_dir is not None and cache_key:
            cached = self._cache_lookup(cache_key, cache_dir)
            if cached:
                self.ctx.logger.info("[MiMoTTS] 命中本地缓存（%d 字节），跳过 API 调用", len(cached))
                return True, cached, vinfo

        messages: List[Dict[str, str]] = []
        if style:
            messages.append({"role": "user", "content": style})
        messages.append({"role": "assistant", "content": synth_text})
        payload = {
            "model": MIMO_MODEL,
            "messages": messages,
            "audio": {"format": "wav", "voice": voice_url},
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        self.ctx.logger.info(
            "[MiMoTTS] 合成请求: %d字 | 音源=%s | 情感=%s | 风格=%s",
            len(text), vinfo, emotion or "-", style[:30] or "-",
        )

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    MIMO_TTS_URL,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status != 200:
                        body = (await resp.text(errors="replace")).strip()[:300]
                        self.ctx.logger.error("[MiMoTTS] HTTP %s: %s", resp.status, body)
                        return False, b"", f"MiMo 接口 HTTP {resp.status}: {body}"

                    data = await resp.json(content_type=None)
                    err = data.get("error") if isinstance(data, dict) else None
                    if err:
                        msg = err.get("message") if isinstance(err, dict) else str(err)
                        self.ctx.logger.error("[MiMoTTS] 业务错误: %s", msg)
                        return False, b"", f"MiMo 业务错误: {msg}"
                    audio_b64 = ""
                    try:
                        audio_b64 = str(data["choices"][0]["message"]["audio"]["data"] or "")
                    except (KeyError, TypeError, IndexError, AttributeError):
                        pass
                    if not audio_b64:
                        return False, b"", "MiMo 未返回音频数据（检查 API Key 与音源文件）"
                    audio = base64.b64decode(audio_b64)
                    if not audio:
                        return False, b"", "MiMo 返回空音频"
                    self.ctx.logger.info("[MiMoTTS] 合成成功 %d 字节", len(audio))
                    if cache_dir is not None and cache_key:
                        self._cache_store(cache_key, audio, cache_dir)
                    return True, audio, vinfo
        except asyncio.TimeoutError:
            self.ctx.logger.error("[MiMoTTS] 请求超时（%ss）", timeout)
            return False, b"", f"请求超时（{timeout:.0f}s）"
        except aiohttp.ClientError as exc:
            self.ctx.logger.error("[MiMoTTS] 网络错误: %s", exc)
            return False, b"", f"网络错误: {type(exc).__name__}"
        except Exception as exc:  # noqa: BLE001 兜底
            self.ctx.logger.error("[MiMoTTS] 异常: %s", exc, exc_info=True)
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

        if ok and text and self._get("plugin", "sync_chat_context", True):
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

        if not self._active_api_key():
            msg = f"{self._engine_name()}语音 API Key 未配置，请先在插件配置里填写"
            await self._maybe_error(stream_id, msg)
            return False, "API Key 未配置"

        if self._cooldown_block(stream_id):
            cd = self._cooldown_seconds()
            self.ctx.logger.info("[豆包TTS] 触发冷却限流(%s)：stream=%s", source, stream_id)
            if source == "命令":
                await self._maybe_error(stream_id, f"语音合成冷却中，请 {cd:.0f} 秒后再试")
            return False, "触发限流"

        ov = overrides if isinstance(overrides, dict) else {}
        self.ctx.logger.info(
            "[豆包TTS] %s 触发语音: %d字 覆盖=%s", source, len(text), json.dumps({k: v for k, v in ov.items() if v is not None}, ensure_ascii=False) or "-"
        )
        ok, note = await self._speech(text, stream_id, overrides=ov)
        if ok:
            return True, note
        # 失败处理
        self.ctx.logger.warning("[豆包TTS] 语音合成失败(%s): %s", source, note)
        if self._get("plugin", "fallback_to_text", True):
            if await self._send_plain_text(text, stream_id):
                # 降级成文字时同样要知道自己说过什么，行为才一致
                if self._get("plugin", "sync_chat_context", True):
                    await self._append_chat_context(stream_id, text)
                return True, "语音合成失败，已改为文字回复"
        await self._maybe_error(stream_id, "语音合成失败了，请稍后再试")
        return False, note

    def _recently_prompted(self, stream_id: str) -> bool:
        """该会话最近是否已收到过失败提示（去重窗口内）。"""

        last = self._last_error_prompt_at.get(stream_id)
        return last is not None and time.time() - last < ERROR_PROMPT_DEDUPE_SECONDS

    async def _send_plain_text(self, text: str, stream_id: str) -> bool:
        """发送插件自身的提示/帮助/降级文本：直发文字，不被本轮的语音标记截获。

        否则「回复生成 / 跟随分段」的标记会把错误提示、降级文本也当成麦麦的回复
        再去合成一遍语音（白花接口调用，提示还变成了不合适的语音）。
        """
        self._suppress_convert_streams.add(stream_id)
        try:
            return bool(await self.ctx.send.text(text, stream_id))
        except Exception:  # noqa: BLE001 提示发不出去不该影响主流程
            return False
        finally:
            self._suppress_convert_streams.discard(stream_id)

    async def _maybe_error(self, stream_id: str, msg: str) -> bool:
        """向用户发失败提示；同一会话 30 秒内只发一次，防 LLM 重试刷屏。

        Returns:
            bool: 是否真的发出了提示。
        """
        if not self._get("plugin", "send_error_prompt", True):
            return False
        if self._recently_prompted(stream_id):
            return False
        self._last_error_prompt_at[stream_id] = time.time()
        return await self._send_plain_text(msg, stream_id)

    # ── Command：手动 ───────────────────────────────────────────────────

    @Command(
        "doubao_tts_say",
        description="用豆包语音把指定文本说出来",
        pattern=r"^/(说|语音|speak)\s+(?P<text>.+)\s*$",
        permission="operator",
    )
    async def _cmd_say(self, stream_id: str = "", matched_groups: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Tuple[bool, str, int]:
        """/说 文本 / 语音 文本 / speak 文本"""
        del kwargs
        if not self._get("plugin", "command_enabled", True):
            return False, "手动命令已禁用", 1
        if not stream_id:
            return False, "缺少 stream_id", 1
        groups = matched_groups or {}
        text = (groups.get("text") or "").strip()
        if not text:
            await self._maybe_error(stream_id, "用法：/说 要转成语音的文本")
            return False, "缺少文本", 1
        ok, note = await self._handle_speech(text, stream_id, "命令")
        return ok, note, 1

    @Command(
        "doubao_tts_help",
        description="查看豆包语音帮助",
        pattern=r"^/(语音帮助|说帮助|tts帮助)\s*$",
        permission="operator",
    )
    async def _cmd_help(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, str, int]:
        """/语音帮助"""
        del kwargs
        if not stream_id:
            return False, "缺少 stream_id", 1
        emotion_list = "、".join(PRESET_EMOTIONS.keys())
        if self._engine() == "mimo":
            _, vinfo = self._mimo_voice_data_url()
            text = (
                "【MiMo 语音（音色复刻）】\n"
                "- 用法：/说 文本 或 /语音 文本\n"
                f"- API Key：{'已配置' if self._mimo_api_key() else '未配置'}\n"
                f"- 复刻音源：{vinfo}\n"
                f"- 情感（可选）：{emotion_list}\n"
                "- 换音源/风格：WebUI 插件配置「MiMo 语音」页修改"
            )
        else:
            voice_list = "、".join(PRESET_VOICES.keys())
            text = (
                "【豆包语音】\n"
                "- 用法：/说 文本 或 /语音 文本\n"
                f"- API Key：{'已配置' if self._doubao_api_key() else '未配置'}\n"
                f"- 当前音色：{self._get('doubao', 'voice', DEFAULT_VOICE_DISPLAY)}\n"
                f"- 预置音色：{voice_list}\n"
                f"- 情感（可选）：{emotion_list}\n"
                "- 换音色/情感：WebUI 插件配置里修改即可"
            )
        await self._send_plain_text(text, stream_id)
        return True, "已发送帮助", 1

    # ── Tool：麦麦自主 ──────────────────────────────────────────────────

    @Tool(
        "doubao_tts_speak",
        brief_description="用语音说话（豆包/MiMo TTS）",
        # visibility="visible"：让宿主把本工具直接放进 LLM 每轮可见的工具列表
        # （默认插件工具是 deferred，LLM 需先调 tool_search 搜索发现后才能用）。
        # 见麦麦 src/maisaka/reasoning_engine.py 的 _build_action_tool_definitions。
        visibility="visible",
        detailed_description=(
            "当用户明确要求“用语音/说话/朗读/语音回复”时使用。"
            "若插件配置「工具语音内容来源=工具文本」（默认）：用 text 给出要朗读的文本，"
            "宜为一句完整的话（5~80字）；若内容很长（>150字），只取其中最想强调的一句话来朗读，其余仍用文字。"
            "若配置为「回复生成」：text 不必填（内容由麦麦的 reply 流程生成），"
            "本工具只表示“本轮用语音回复”，可以单独调用、下一轮再调用 reply，"
            "也可以与 reply 在同一条消息里一起调用（本工具在前更稳）以省去一轮思考；"
            "只要本轮还没生成回复，就调用 reply（若已同时调用过则不要重复），"
            "插件会在发送时自动把这条回复整条转成语音，不要再自己写一遍要说的内容。"
            "可选按对话氛围调节语气：emotion（情感，如安慰时平静、玩闹时开心）、emotion_scale（强度1~5，仅豆包生效）。"
            "每个参数可单独给，未给的使用插件配置里的固定值；拿不准就都省略，用默认语气即可。"
        ),
        parameters=[
            ToolParameterInfo(
                name="text",
                param_type=ToolParamType.STRING,
                description=(
                    "要转成语音朗读的文本（一句完整的话）。"
                    "「工具语音内容来源=工具文本」（默认）时必填，留空本轮不会发声；"
                    "=「回复生成」时可省略（内容由 reply 生成，给了会被忽略）"
                ),
                required=False,
            ),
            ToolParameterInfo(
                name="emotion",
                param_type=ToolParamType.STRING,
                description="情感语气（按对话氛围选一个）：开心/伤心/生气/害怕/惊讶/讨厌/哭泣/抱歉/平静/播音/讲故事",
                required=False,
                enum_values=list(PRESET_EMOTIONS.keys()),
            ),
            ToolParameterInfo(
                name="emotion_scale",
                param_type=ToolParamType.INTEGER,
                description="情感强度 1~5（1=最淡），配合 emotion；超出范围会被忽略",
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
        if self._get("debug", "enabled", False):
            self.ctx.logger.info(
                "[豆包TTS·调试] 工具调用: text=%s | engine=%s",
                str(text or "")[:40],
                self._engine_name(),
            )
        stream_id = str(kwargs.get("stream_id") or kwargs.get("chat_id") or "")
        del kwargs
        if not stream_id:
            return {"success": False, "message": "缺少 stream_id，无法发送语音"}
        # 麦麦自主语音由主页「自主语音方式」管控（off=麦麦不自主）；
        # 「手动命令」开关只管 /说 手动命令，不该挡这里（修 v1.4.4：原版把它错用在 Tool 上，
        # 关掉手动命令会让麦麦每次调用本工具都失败）。
        if self._auto_voice_mode() == "off":
            return {"success": False, "message": "麦麦自主语音已关闭（自主语音方式=off），请使用 /说 手动命令"}
        # LLM 传入的情感/强度直接透传；主页选了固定值且 LLM 未给时，合成层自动改用固定值
        overrides: Dict[str, Any] = {}
        if (emotion or "").strip():
            overrides["emotion"] = (emotion or "").strip()
        if emotion_scale is not None:
            overrides["emotion_scale"] = emotion_scale
        # 「回复生成」模式：工具只当"本轮用语音回复"的开关，内容等 reply 生成后再转语音
        if self._voice_content_source() == "reply":
            if (text or "").strip():
                self.ctx.logger.info(
                    "[豆包TTS] 回复生成模式：忽略 LLM 传入的 text（%d字），内容以 reply 生成为准",
                    len(text.strip()),
                )
            return await self._tool_request_voice_reply(stream_id, overrides)
        if not (text or "").strip():
            # text 现为选填（回复生成模式不需要），默认模式下模型可能漏填——
            # 明确告诉它该补什么，避免一次无谓的失败重试。
            return {
                "success": False,
                "message": (
                    "text 为空，本轮未发声。当前「工具语音内容来源=工具文本」，"
                    "请把要朗读的文本（一句完整的话）放进 text 重新调用一次。"
                ),
            }
        ok, note = await self._handle_speech(text, stream_id, "Tool", overrides=overrides or None)
        if ok:
            # stop_after_execution：语音已通过 send.custom 直发到会话，本批工具执行完
            # 即结束 planner——否则 LLM 会再调 reply 发一遍文字，内容与语音重复。
            # 仅在成功路径停止；失败时让 LLM 自行告知用户（插件侧已发降级/错误提示的除外）。
            return {"success": True, "message": note, "stop_after_execution": True}
        # 失败路径默认不结束 planner，让 LLM 转告用户；但若插件刚给用户发过失败提示
        # （去重窗口内），再 reply 只会与提示重复，此时同样结束 planner。
        msg = f"语音失败：{note}"
        if "限流" in note:
            msg += "；冷却期内重试仍会失败，请直接转告用户稍后再试，不要连续重试"
        result: Dict[str, Any] = {"success": False, "message": msg}
        if self._recently_prompted(stream_id):
            result["stop_after_execution"] = True
        return result

    async def _tool_request_voice_reply(
        self, stream_id: str, overrides: Dict[str, Any]
    ) -> Dict[str, Any]:
        """「回复生成」模式：只给会话打"本轮回复转语音"标，内容交给 reply 生成。

        语音不在工具里合成，而是等 planner 随后调用 reply、replyer 生成回复文本后，
        由 send_service.before_send 钩子把那条文字原地换成语音（整条回复逐段转）。
        所以这里**不能**带 stop_after_execution——planner 必须继续调用 reply 生成内容。

        失败（未配 Key / 冷却中）时不打标，reply 照常以文字发出，不会丢内容。
        """
        if not self._active_api_key():
            msg = f"{self._engine_name()}语音 API Key 未配置，请先在插件配置里填写"
            await self._maybe_error(stream_id, msg)
            return {"success": False, "message": msg}
        if self._cooldown_block(stream_id):
            cd = self._cooldown_seconds()
            self.ctx.logger.info("[豆包TTS] 触发冷却限流(回复生成)：stream=%s", stream_id)
            return {
                "success": False,
                "message": (
                    f"语音冷却中（{cd:.0f} 秒）；冷却期内重试仍会失败，"
                    "请直接转告用户稍后再试，不要连续重试"
                ),
            }
        self._pending_voice[stream_id] = time.time()
        self._pending_voice_overrides[stream_id] = dict(overrides or {})
        self.ctx.logger.info(
            "[豆包TTS] 回复生成：会话 %s 已标记，待 reply 生成内容后整条转语音", stream_id
        )
        return {
            "success": True,
            "message": (
                "已进入语音回复模式：本条回复会在发送时自动逐段转成语音。"
                "若你还没有生成回复，请调用 reply 工具正常生成内容"
                "（不必在 text 参数里重复写要说的内容）；"
                "若已在本批同时调用过 reply，则无需再调用，直接结束本轮即可。"
            ),
        }


def create_plugin() -> MaiBotPlugin:
    return DoubaoTTSPlugin()
