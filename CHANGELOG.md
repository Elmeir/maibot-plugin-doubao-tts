# 更新日志

## v1.4.0 (2026-09-03)

- 配置页按用途拆分（WebUI 页签）：**豆包语音**（API Key/Resource ID 连接）、**音色与情感**（voice/emotion/emotion_scale）、**语速与音量**（speech_rate/loudness）、**行为**（自主方式/概率/来源开关）。
- 配置段结构调整：voice/emotion/emotion_scale → `[voice_tone]`；speech_rate/loudness → `[speed_loud]`；api_key/resource_id 仍留 `[doubao]`（老配置的 key 不受影响）。

## v1.3.0 (2026-09-03)

- 配置界面化：`auto_voice_mode` 改为下拉三选一（llm/probability/off）；`resource_id` 保留文本框并提示常用两值，支持填任意资源 ID。
- 「麦麦自主 vs 手动固定」按参数独立：
  - **情感（emotion）**、**情感强度（emotion_scale）**：各配一个来源开关（`fixed` 用配置值 / `auto` 麦麦自主语音时由 LLM 现场挑）——它们是有明确档位的参数；
  - **语速（speech_rate）、音量（loudness）**：火山固定连续数值，**麦麦不可自主**，仅手动填写（字段已标注）。
- speak 工具参数精简为 text/emotion/emotion_scale（语速音量不进工具参数）。
- `resource_id` 增加占位提示与说明。

## v1.2.0 (2026-09-03)

- 新增「麦麦自主语音方式」开关（`[behavior] auto_voice_mode`）：`llm`（默认，麦麦自行判断何时用语音）/ `probability`（按概率偶尔语音，`auto_voice_probability` 可调，0.1=平均每 10 轮约 1 轮）/ `off`（仅手动命令）。
- 概率模式实现：入站钩子按概率掷骰打标 → 出站钩子把麦麦文字回复转成语音（不依赖 LLM 自觉）。
- `[behavior] auto_voice_probability`：概率模式的触发概率 0~1。
- 移除试验中的"关键词强制语音"设计（改纯概率，避免硬拦截误伤）。

## v1.1.0 (2026-09-03)

- 新增「麦麦自主选情感」开关（`[behavior] auto_emotion`）：
  - **开**：麦麦自主语音（Tool）时，LLM 根据对话氛围现场选情感（开心/伤心/生气/…），每次可不同；
  - **关**（默认）：一律使用 `[doubao]` 里配置的固定情感/语速/音量；
  - 手动 `/说` 命令**始终**使用固定配置（不受此开关影响）。
- Tool `doubao_tts_speak` 增加可选 `emotion` 参数（枚举），供自主情感模式使用。

## v1.0.0 (2026-09-03)

- 首个版本：豆包语音合成（火山引擎豆包语音合成大模型）。
- 新版控制台鉴权（X-Api-Key 单头），无需旧版 App ID / Access Token。
- 内置官方预置音色库（seed-tts-2.0），配置按名选用；也支持自定义/复刻音色（seed-icl-2.0）。
- 触发方式：`/说 文本` `/语音 文本` `/speak 文本` 手动命令 + LLM 可调用的语音 Tool。
- 发送：`send.custom("voice")` base64 直发，无需落盘。
- 健壮性：超时/网络/业务错误分级处理；失败可降级为文字；长文本按句切分多条发送。
