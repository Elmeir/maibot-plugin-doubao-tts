# 豆包·MiMo 语音 TTS（maibot-plugin-doubao-tts）

> 让麦麦把文字变成语音说出口——双引擎可切换：**火山引擎·豆包语音合成大模型**（内置官方预置音色库，也支持你自己的复刻音色）或**小米 MiMo 音色复刻**（`mimo-v2.5-tts-voiceclone`，丢一个 wav/mp3 进插件 `src/` 就能用你的音色说话）。

- 插件 ID：`github.elmeir.doubao-tts`
- 宿主要求：MaiBot ≥ 1.2.4（maibot-plugin-sdk ≥ 2.0）
- 依赖：`aiohttp` ≥ 3.8.0（自动安装）
- 作者：[Elmeir](https://github.com/Elmeir)（fork 自 [ZhaoCang-QWQ/doubao-tts](https://github.com/ZhaoCang-QWQ/doubao-tts)）｜ License：MIT
- 版本：见 [_manifest.json](_manifest.json) ｜ 变更见 [CHANGELOG.md](CHANGELOG.md)

## 功能

- **双引擎**：WebUI 主页「语音引擎」下拉切换——豆包（火山）/ MiMo（小米·音色复刻）
- **手动命令**：`/说 文本` / `/语音 文本` / `/speak text`，随时让麦麦发声
- **麦麦自主语音**：`llm`（LLM 自主判断何时用语音）/ `probability`（按概率把文字回复转语音）/ `off`（仅手动）
- **语音内容来源可选**：`工具文本`（LLM 直接给出要朗读的文本）或 `回复生成`（LLM 只决定"用语音回复"，说什么由麦麦的 reply 流程生成后自动转语音）
- **音色与情感**：豆包官方预置音色库 + 复刻音色；MiMo 任意音源复刻；情感 11 种、强度 1~5
- **稳态设计**：合成失败自动降级文字；同会话冷却防刷费用；本地合成缓存；语音原文写回上下文；改配置热更新

## 适用边界（安装前必读）

- **豆包**：需要火山引擎账号、开通豆包语音服务并拿到 **API Key**（控制台 → 豆包语音 → API Key 管理）；合成在云端进行（服务器需可访问 `openspeech.bytedance.com`），按字符计费（有免费额度）。鉴权用**新版控制台**的 `X-Api-Key`，不需要旧版 App ID / Access Token / 集群 ID
- **豆包音色与资源 ID 必须匹配**（最常见报错）：预置音色配 `seed-tts-2.0`；复刻音色（`S_` 开头 ID）配 `seed-icl-2.0`。配错报 `resource ID is mismatched with speaker related resource`
- **MiMo**：需要 [MiMo 开放平台](https://mimo.mi.com) 的 API Key；模型固定 `mimo-v2.5-tts-voiceclone`，音源仅支持 wav/mp3、≤10MB；服务器需可访问 `api.xiaomimimo.com`
- 语音经麦麦 `send.custom("voice")` 通道发出，平台适配器不支持语音消息时无法发声

## 安装

```bash
cd /你的部署目录/plugins        # 例如 /opt/MaiBot/plugins
git clone https://github.com/Elmeir/maibot-plugin-doubao-tts.git
# 重启麦麦（systemctl restart xxx / docker compose restart / 重启 bot.py）
```

> 目录名保持 `maibot-plugin-doubao-tts`：配置存在插件目录内的 `config.toml`，换目录名会丢配置。

日志出现 `[豆包TTS] 插件已加载` 即成功；依赖自动安装失败时手动 `pip install -r requirements.txt`。

## 配置（唯一必填：所选引擎的 api_key）

配置按页签组织：**插件主页**只放开关与下拉；**豆包语音**/**MiMo 语音**各一页改连接与音色；**高级**页收纳数值与路径；**调试**页放诊断日志开关与组件信息（LLM 视角的工具/命令定义：可见性 / 描述 / 参数 / 匹配模式，只读、随插件加载自动刷新）。

```toml
[plugin]                      # ── 主页：仅开关与下拉 ──
enabled = true                # 插件总开关
tts_engine = "豆包语音"        # 语音引擎：豆包语音 / MiMo 语音（下拉切换）
emotion = "麦麦自主"           # 情感：麦麦自主（LLM 现场挑，默认）/ 无 / 开心/伤心/生气/…=固定
emotion_scale = "麦麦自主"     # 情感强度（豆包）：麦麦自主（默认）/ 1~5 固定档位
command_enabled = true        # /说 /语音 命令开关
auto_voice_mode = "LLM 自行判断"  # 麦麦自主语音：LLM 自行判断 / 概率触发 / 仅手动
voice_content_source = "工具文本"  # 工具语音内容来源：工具文本 / 回复生成（内容由 reply 生成后转语音）
fallback_to_text = true       # 失败降级发文字
send_error_prompt = true      # 失败时提示用户
sync_chat_context = true      # 语音原文写回麦麦上下文（推荐开启）
follow_segmentation = false   # 跟随分段发语音
cache_enabled = false         # 本地合成缓存

[doubao]                      # ── 豆包语音页 ──
api_key = "你的火山新版控制台 API Key"   # 引擎=豆包语音 时必填
resource_id = "seed-tts-2.0" # 预置音色；复刻音色改 seed-icl-2.0
voice = "小何 2.0"            # 音色名或 voice_type ID
speech_rate = 0              # 语速 -50~100（仅手动）
loudness = 0                 # 音量 -50~100（仅手动）

[mimo]                        # ── MiMo 语音页 ──
api_key = ""                 # ← 引擎=MiMo 语音 时必填：MiMo 开放平台 API Key（只填 config.toml，勿提交）
voice_sample = "dxl.wav"     # 复刻音源：相对路径默认在插件 src/ 目录找；wav/mp3、≤10MB
style = ""                   # 自然语言风格指令（可选）

[behavior]                    # ── 高级页：数值与路径 ──
command_cooldown_seconds = 10 # 同会话语音最小间隔（秒），0=不限流
auto_voice_probability = 0.1  # 概率模式触发概率 0~1（仅 probability）
timeout_seconds = 30          # 请求超时（秒）
max_text_length = 150         # 单条最大字数（超过按句切分）
context_prefix = "[语音]"     # 写回上下文时的前缀
cache_dir = ""                # 缓存目录；留空 = 宿主插件数据目录下 doubao-tts-cache/
cache_max_files = 500         # 缓存文件数上限
```

全部配置项在 WebUI 插件面板均有中文标签与说明，改配置即生效。`config.toml` 含密钥请勿上传，仓库只提供 `config.example.toml`。

### 工具语音内容来源：工具文本 / 回复生成

麦麦自主语音（Tool `doubao_tts_speak`）说的话从哪来，由主页「工具语音内容来源」下拉决定：

| 取值 | 行为 |
| --- | --- |
| `工具文本`（默认） | LLM 调用工具时把要朗读的文本作为 `text` 传入，插件直接合成发出；语音发出后结束本轮 planner，不再重复发文字 |
| `回复生成` | LLM 调用工具时**不必给文本**（只表示"本轮用语音回复"），工具会要求 planner 继续调用 `reply`；麦麦的 reply 流程生成回复文本后，插件在发送前把这条回复**整条逐段**原地转成语音 |

选择 `回复生成` 时：

- 说话内容由麦麦的回复器按人设与上下文生成，不会出现"planner 现编文本"与"reply 文本"不一致；
- 成本：工具结果必须交回 LLM 继续思考，所以"工具调用 → 下一轮 reply"比普通文字回复多 1 次
  planner 请求；LLM 若把本工具与 reply 在同一条消息里一起调用，则与普通回复持平（工具描述已这样引导）。
  想彻底零额外开销，用「自主语音方式=概率触发 + 语音概率=1」让 planner 照常回复、发送时转语音即可；
- 整条回复都会转语音（不受「跟随分段发语音」开关限制；回复被后处理/智能分段拆成多段时，每段各自转语音）；
- LLM 调用工具时给的情感/强度（`emotion` / `emotion_scale`）依然生效；
- 未配 API Key、冷却中，或 planner 最终没调用 `reply`（极少见）时，保持正常文字回复，不会丢内容。

### MiMo 音色复刻（mimo-v2.5-tts-voiceclone）

1. 把要复刻的音源文件（wav/mp3，≤10MB，越干净越好）放进插件 `src/` 目录（默认文件名 `dxl.wav`），或在 WebUI「MiMo 语音」页把 `voice_sample` 改成其他文件名/绝对路径
2. 主页「语音引擎」切到 `mimo`；在「MiMo 语音」页填入你自己的 API Key（只写 `config.toml`，已被 .gitignore 排除，勿提交）
3. 可选：`style` 填一句自然语言风格指令（如「语速稍快，语气亲切自然」）；主页「情感」下拉会转成 `(风格)` 标签叠加在合成文本上
4. MiMo 每次请求都随音源实时复刻，无需预注册音色；换音源文件立即生效（自动按 mtime 重新读盘）

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
| MiMo 报 401 / 未返回音频 | api_key 未填或无效，或音源不可用；看日志 `[MiMoTTS]` 具体错误 |
| 提示"复刻音源不可用" | `voice_sample` 路径不对：默认在插件 `src/` 目录下找 `dxl.wav`，或填绝对路径；仅支持 wav/mp3、≤10MB |
| HTTP 401 / 鉴权失败 | api_key 填错，或账号未开通豆包语音服务 |
| 说话没声音/失败 | 平台适配器不支持语音消息通道；看日志 `[豆包TTS]` 具体错误 |
| 选了「回复生成」但没听到语音 | 该模式下语音要等 planner 继续调用 `reply` 才合成：日志有 `回复生成：会话 … 已标记` 后，应再看到 `回复生成：会话 … 文字回复 → 语音`；后者没出现说明 planner 没调用 reply（保持文字回复） |
| 语音失败后反复报错 | 失败提示同一会话 30 秒只发一次；冷却期内（默认 10 秒）重试必定失败，属预期 |

## 致谢

fork 自昭沧QWQ 的 [ZhaoCang-QWQ/doubao-tts](https://github.com/ZhaoCang-QWQ/doubao-tts) 继续维护，感谢原作者；功能组织参考 [xuqian13/tts_voice_plugin](https://github.com/xuqian13/tts_voice_plugin)。

## 许可证

[MIT](LICENSE)
