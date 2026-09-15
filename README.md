# 豆包语音 TTS（maibot-plugin-doubao-tts）

> 让麦麦把文字变成语音说出口——调用**火山引擎·豆包语音合成大模型**（新版控制台鉴权），内置官方预置音色库，也支持你自己的复刻音色。

- 插件 ID：`github.elmeir.doubao-tts`
- 宿主要求：MaiBot ≥ 1.2.4（maibot-plugin-sdk ≥ 2.0）
- 依赖：`aiohttp` ≥ 3.8.0（自动安装）
- 作者：[Elmeir](https://github.com/Elmeir)（fork 自 [ZhaoCang-QWQ/doubao-tts](https://github.com/ZhaoCang-QWQ/doubao-tts)）｜ License：MIT
- 版本：见 [_manifest.json](_manifest.json) ｜ 变更见 [CHANGELOG.md](CHANGELOG.md)

## 功能

- **手动命令**：`/说 文本` / `/语音 文本` / `/speak text`，随时让麦麦发声
- **麦麦自主语音**：`llm`（LLM 自主判断何时用语音）/ `probability`（按概率把文字回复转语音）/ `off`（仅手动）
- **音色与情感**：官方预置音色库 + 复刻音色；情感 11 种、强度 1~5
- **稳态设计**：合成失败自动降级文字；同会话冷却防刷费用；本地合成缓存；语音原文写回上下文；改配置热更新

## 适用边界（安装前必读）

- 需要火山引擎账号、开通豆包语音服务并拿到 **API Key**（控制台 → 豆包语音 → API Key 管理）；合成在云端进行（服务器需可访问 `openspeech.bytedance.com`），按字符计费（有免费额度）
- 鉴权用**新版控制台**的 `X-Api-Key`，不需要旧版 App ID / Access Token / 集群 ID
- 语音经麦麦 `send.custom("voice")` 通道发出，平台适配器不支持语音消息时无法发声
- ⚠️ **音色与资源 ID 必须匹配**（最常见报错）：预置音色配 `seed-tts-2.0`；复刻音色（`S_` 开头 ID）配 `seed-icl-2.0`。配错报 `resource ID is mismatched with speaker related resource`

## 安装

```bash
cd /你的部署目录/plugins        # 例如 /opt/MaiBot/plugins
git clone https://github.com/Elmeir/maibot-plugin-doubao-tts.git
# 重启麦麦（systemctl restart xxx / docker compose restart / 重启 bot.py）
```

> 目录名保持 `maibot-plugin-doubao-tts`：配置存在插件目录内的 `config.toml`，换目录名会丢配置。

日志出现 `[豆包TTS] 插件已加载` 即成功；依赖自动安装失败时手动 `pip install -r requirements.txt`。

## 配置（唯一必填：api_key）

```toml
[doubao]
api_key = "你的火山新版控制台 API Key"   # ← 唯一必填
resource_id = "seed-tts-2.0"           # 预置音色；复刻音色改 seed-icl-2.0

[voice_tone]
voice = "小何 2.0"                      # 音色名或 voice_type ID
emotion = "none"                        # 固定情感：开心/伤心/生气/害怕/惊讶/讨厌/哭泣/抱歉/平静/播音/讲故事
emotion_scale = 1.0                     # 情感强度 1~5

[speed_loud]
speech_rate = 0                         # 语速 -50~100（连续数值，仅手动）
loudness = 0                            # 音量 -50~100（连续数值，仅手动）

[behavior]
command_enabled = true                  # /说 /语音 命令开关
command_cooldown_seconds = 10           # 同会话语音最小间隔（秒），0=不限流
auto_voice_mode = "llm"                 # 麦麦自主语音：llm / probability / off
auto_voice_probability = 0.1            # 概率模式触发概率 0~1（仅 probability）
emotion_mode = "fixed"                  # 情感来源：fixed / auto（麦麦自主语音时 LLM 现场挑）
sync_chat_context = true                # 语音原文写回麦麦上下文（推荐开启）
context_prefix = "[语音]"                # 写回上下文时加的前缀
cache_enabled = false                   # 本地合成缓存（同文本同音色复用音频）
```

全部配置项在 WebUI 插件面板均有中文标签与说明（含跟随分段、缓存目录等次要项），改配置即生效。`config.toml` 含密钥请勿上传，仓库只提供 `config.example.toml`。

### 预置音色（seed-tts-2.0，填音色名即可）

| 音色名 | 风格 |
| --- | --- |
| 小何 2.0（默认） | 自然亲切女声 |
| Vivi 2.0 | 多语种、情感丰富 |
| 爽快思思 2.0 | 爽朗活泼 |
| 甜美小源 / 甜美桃子 2.0 | 甜美 |
| 邻家女孩 / 清新女声 / 流畅女声 2.0 | 亲切 / 清新 / 流畅 |
| 魅力女友 2.0 | 魅力成熟 |
| 云舟 / 小天 / 刘飞 2.0 | 男声 |

更多音色：`voice` 直接填 voice_type ID 即可使用（如 `zh_female_vv_uranus_bigtts`），不必在表内。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 发 `/说` 后收到的是文字（没语音） | 合成失败已降级为文字。最常见是音色与资源不匹配（见"适用边界"），改对后日志显示 `合成成功` |
| HTTP 401 / 鉴权失败 | api_key 填错，或账号未开通豆包语音服务 |
| 说话没声音/失败 | 平台适配器不支持语音消息通道；看日志 `[豆包TTS]` 具体错误 |
| 语音失败后反复报错 | 失败提示同一会话 30 秒只发一次；冷却期内（默认 10 秒）重试必定失败，属预期 |

## 致谢

fork 自昭沧QWQ 的 [ZhaoCang-QWQ/doubao-tts](https://github.com/ZhaoCang-QWQ/doubao-tts) 继续维护，感谢原作者；功能组织参考 [xuqian13/tts_voice_plugin](https://github.com/xuqian13/tts_voice_plugin)。

## 许可证

[MIT](LICENSE)
