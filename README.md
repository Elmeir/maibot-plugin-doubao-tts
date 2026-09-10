# 豆包语音TTS（新版控制台）

> 让麦麦把文字变成语音说出口——调用**火山引擎·豆包语音合成大模型**（新版控制台鉴权），内置官方预置音色库，也支持你自己的复刻音色。

- 插件 ID：`github.elmeir.doubao-tts`
- 版本：1.4.0
- 类型：扩展插件
- 最低麦麦版本：1.2.0（使用 maibot-plugin-sdk v2）
- 依赖：`aiohttp`（≥3.8.0，自动安装）
- 作者：Elmeir（fork 自昭沧QWQ 的 [ZhaoCang-QWQ/doubao-tts](https://github.com/ZhaoCang-QWQ/doubao-tts)）
- License：MIT

---

## 一、适用边界（安装前请先读）

### 依赖"豆包语音"云端服务

本插件是**火山引擎豆包语音合成大模型（Doubao TTS）的客户端**：
- 需要你有**火山引擎账号**并在新版控制台开通豆包语音服务、拿到 **API Key**；
- 合成在火山云端进行，需要服务器能访问外网（`openspeech.bytedance.com`）；
- 语音合成按字符**计费**（火山侧有免费额度，超出按量付费，详见火山计费说明）。

### 鉴权方式（重要）

插件使用火山**新版控制台**的简化鉴权——请求头只需一个 **`X-Api-Key`**。
**不需要**旧版控制台的 App ID / Access Token / 集群 ID（旧版控制台已逐步下线）。

### 声音能发到哪里

语音经麦麦 `send.custom("voice")` 通道发出，跟随麦麦当前的可用通道（NapCat/QQ 等平台适配器）。平台不支持语音消息时则无法发声（会提示失败）。

### ⚠️ 音色与资源模型必须匹配（最常见报错）

火山要求**音色类型和资源 ID 一一对应**，配错了会报 `resource ID is mismatched with speaker related resource`：

| 音色类型 | Resource ID（`[doubao] resource_id`） | 音色长什么样 |
| --- | --- | --- |
| **官方预置音色**（如 小何/Vivi/云舟…） | `seed-tts-2.0` | 音色名是"中文+2.0"（如 `小何 2.0`），voice_type 以 `_uranus_bigtts` 结尾 |
| **复刻音色**（自己克隆的声音） | `seed-icl-2.0` | voice 填复刻音色 ID，形如 `S_RcCsDWbd2`（控制台"声音复刻"里创建） |

> 判断方法：`voice` 填的是 `S_...` 开头的复刻 ID → `resource_id` 必须用 `seed-icl-2.0`；填预置音色名/ID → 用 `seed-tts-2.0`。改音色时同步核对 resource_id，两者不可混用。

---

## 二、工作原理

1. **触发**：你发 `/说 文本`（或麦麦通过 Tool 自主决定用语音）；
2. **合成**：插件把文本 POST 给火山豆包语音接口，流式接收音频（mp3）；
3. **发送**：音频 base64 后经 `send.custom("voice")` 发到当前会话，无需写临时文件；
4. **兜底**：合成失败时可按配置降级为文字发出，不让对话干等。

---

## 三、安装

1. 将整个 `doubao_tts_plugin` 文件夹放入麦麦的 `plugins/` 目录；
2. 麦麦自动加载（或重启麦麦）。若自动安装依赖失败，手动补装：`pip install -r requirements.txt`（或 `python -m pip install aiohttp`）；
3. 日志出现 `[豆包TTS] 插件已加载` 即成功。

## 四、配置（必须填 API Key）

配置按用途分为多个页签（WebUI）：**豆包语音**（连接）、**音色与情感**、**语速与音量**、**行为**。config.toml 结构对应如下：

```toml
[doubao]                            # 页签：豆包语音（连接）
api_key = "你的火山新版控制台 API Key"   # ← 唯一必填
resource_id = "seed-tts-2.0"        # 预置音色；复刻音色改 seed-icl-2.0（也可填其他资源 ID）

[voice_tone]                        # 页签：音色与情感
voice = "小何 2.0"                    # 音色名或 voice_type ID
emotion = ""                          # 情感固定值（可空）；见下方"参数来源"
emotion_scale = 1.0                   # 情感强度固定值 1~5

[speed_loud]                        # 页签：语速与音量
speech_rate = 0                       # ⚠️ 语速 -50~100：火山连续数值，仅手动，麦麦不可自主
loudness = 0                          # ⚠️ 音量 -50~100：火山连续数值，仅手动，麦麦不可自主

[behavior]                          # 页签：行为
auto_voice_mode = "llm"               # 麦麦自主语音：llm / probability / off（下拉）
emotion_mode = "fixed"                # 情感来源：fixed=用 [voice_tone] emotion / auto=麦麦现场挑（下拉）
emotion_scale_mode = "fixed"          # 情感强度来源：fixed / auto（下拉）
```

**API Key 在哪拿**：火山引擎控制台 → 豆包语音 → **API Key 管理** → 新建/复制。需先开通"语音合成大模型"服务（控制台 → 开通管理）。

### 参数来源：固定 vs 麦麦自主（按参数独立选择）

插件遵循"**能选的才给麦麦自主，连续数值只手动**"的原则：

| 参数 | 有无档位 | 能否麦麦自主 | 怎么设置 |
| --- | --- | --- | --- |
| 情感 `emotion` | 有（11 种枚举） | ✅ 可 | 下拉选：默认"无（正常语气）"；`emotion_mode=fixed` 用它，`auto` 时麦麦自主语音现场挑 |
| 情感强度 `emotion_scale` | 有（1~5） | ✅ 可 | 默认 1（最淡）；`emotion_scale_mode=fixed` 用它，`auto` 时麦麦按情感挑 |
| 语速 `speech_rate` | **无（连续 -50~100）** | ❌ 仅手动 | 填 `[speed_loud] speech_rate`；**默认 0 = 正常语速**，正数加快、负数放慢 |
| 音量 `loudness` | **无（连续 -50~100）** | ❌ 仅手动 | 填 `[speed_loud] loudness`；**默认 0 = 正常音量**，正数大声、负数小声 |

> 说明：
> - **麦麦自主仅在 `auto_voice_mode="llm"` 时生效**（麦麦自己决定语音时才谈得上现场挑情感）；`probability` 模式不经 LLM 判断，一律用固定值；手动 `/说` 命令也始终用固定值。
> - 语速/音量是火山接口的连续调节量，没有"档位"，交给 LLM 填数字容易怪、且不可预期，故设计为**仅手动**——这也是参考同类插件后的取舍。
> - 改配置即生效（热更新），无需重启。

> ⚠️ `config.toml` 含密钥，**请勿随插件分发/上传仓库**。本仓库只提供 `config.example.toml`。

## 五、预置音色表（seed-tts-2.0，选填音色名即可）

| 音色名（配置里填这个） | voice_type ID | 风格 |
| --- | --- | --- |
| 小何 2.0（默认） | `zh_female_xiaohe_uranus_bigtts` | 自然亲切女声 |
| Vivi 2.0 | `zh_female_vv_uranus_bigtts` | 多语种、情感丰富 |
| 爽快思思 2.0 | `zh_female_shuangkuaisisi_uranus_bigtts` | 爽朗活泼 |
| 甜美小源 2.0 | `zh_female_tianmeixiaoyuan_uranus_bigtts` | 甜美 |
| 甜美桃子 2.0 | `zh_female_tianmeitaozi_uranus_bigtts` | 甜美 |
| 邻家女孩 2.0 | `zh_female_linjianvhai_uranus_bigtts` | 亲切邻家 |
| 清新女声 2.0 | `zh_female_qingxinnvsheng_uranus_bigtts` | 清新淡雅 |
| 流畅女声 2.0 | `zh_female_liuchangnv_uranus_bigtts` | 流畅自然 |
| 魅力女友 2.0 | `zh_female_meilinvyou_uranus_bigtts` | 魅力成熟 |
| 云舟 2.0 | `zh_male_m191_uranus_bigtts` | 通用男声 |
| 小天 2.0 | `zh_male_taocheng_uranus_bigtts` | 年轻男声 |
| 刘飞 2.0 | `zh_male_liufei_uranus_bigtts` | 通用男声 |

**更多音色**：火山音色库还提供大量其他预置音色。配置 `voice` 直接填 `voice_type` ID 即可使用（不一定在表内）。

**复刻音色**：使用自己训练/复刻的音色时，把 `resource_id` 改为 `seed-icl-2.0`，`voice` 填复刻音色的 ID（如 `S_xxxxxxxx`）。

> 注意：预置音色（seed-tts-2.0）与复刻音色（seed-icl-2.0）**不可混用**，改 `voice` 时要同步核对 `resource_id`。

## 六、情感与效果（可选）

`emotion` 取值：开心 / 伤心 / 生气 / 害怕 / 惊讶 / 讨厌 / 哭泣 / 抱歉 / 平静 / 播音 / 讲故事。
`emotion_scale` 1~5 调节强度。语速/音量在 -50~100 间调整，0 为正常。

## 七、使用

- **手动测试**：`/说 你好呀`、`/语音 今天天气真好`、`/speak hello`——任何时候都能用，麦麦把文本转语音发出来。
- **帮助**：`/语音帮助`（看当前状态、可用音色）
- **麦麦自主语音方式**（`[behavior] auto_voice_mode`，三选一）：
  - `llm`（默认）：插件注册「speak_text」工具，麦麦在合适场景（如你要求"用语音说"，或它觉得该活泼一下）**自行判断**是否用语音，最自然、不打扰；
  - `probability`：**概率模式**——麦麦每收到一条普通消息按 `auto_voice_probability` 掷骰子（0.1=平均每 10 轮约 1 轮语音），命中则本轮文字回复自动转成语音，与 LLM 判断无关，适合想要"稳定地偶尔说话"；
  - `off`：麦麦不自主语音，只有手动 `/说`/`/语音` 才会发声。
- **情感/强度来源（可选）**：
  - `[behavior] emotion_mode=fixed`（默认）：情感用 `[doubao] emotion` 固定值；
  - `[behavior] emotion_mode=auto`：麦麦自主语音（llm 模式）时由 LLM 按对话氛围现场挑情感，更生动。手动 `/说` 命令始终用固定设置。
  - `emotion_scale_mode` 同理控制情感强度（1~5）。语速/音量仅手动（见上表）。
- **换音色/改模式**：改配置即生效（热更新），无需重启。

## 八、常见问题

| 现象 | 原因与处理 |
| --- | --- |
| **发 `/说` 后收到的是文字回复**（没语音） | 说明**合成失败了，插件按设置把文字降级发出**（`fallback_to_text=true`）。最常见原因：**音色与资源不匹配**——`S_` 开头的复刻音色必须配 `seed-icl-2.0`，预置音色必须配 `seed-tts-2.0`（详见上方案例）。改对后日志应显示 `合成成功` |
| 日志 `[豆包TTS] API Key 未配置` | 配置里还没填 api_key |
| HTTP 401 / 鉴权失败 | api_key 填错，或账号未开通豆包语音服务 |
| HTTP 业务错误 `resource ID is mismatched with speaker related resource` | 音色与资源不匹配：预置音色配 `seed-tts-2.0`，复刻音色（`S_` 开头）配 `seed-icl-2.0` |
| 请求超时 | 服务器到火山网络不稳，可增大 `timeout_seconds` |
| 说话没声音/失败 | 平台适配器不支持语音消息通道；或看日志中 `[豆包TTS]` 具体错误 |
| 概率模式不触发 | 检查 `auto_voice_mode="probability"` 且 `auto_voice_probability` 在 0~1 之间（默认 0.1）；命中后麦麦下一条文字回复才会转语音 |

## 九、致谢

- 本插件 fork 自昭沧QWQ 的 [ZhaoCang-QWQ/doubao-tts](https://github.com/ZhaoCang-QWQ/doubao-tts)，在其基础上继续维护，感谢原作者的工作。
- 参考了靓仔开发的 [xuqian13/tts_voice_plugin](https://github.com/xuqian13/tts_voice_plugin)（多后端 TTS 插件）的功能组织思路，本插件聚焦豆包语音单一后端、精简为新版控制台鉴权。

## 十、卸载

删除 `plugins/doubao_tts_plugin` 文件夹即可（或把 `config.toml` 里 `[plugin] enabled` 改为 `false`）。
