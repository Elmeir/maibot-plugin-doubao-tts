# 更新日志

## v1.4.10 (2026-09-11) 修复：合成失败 + LLM 重试导致错误提示刷屏

现象推演：合成失败且 `send_error_prompt=true`（默认）时，插件给用户发一条
"语音合成失败了"；planner 不结束，LLM 看到失败**重试工具** → 每次重试再发一条
提示 → N 条错误消息刷屏。

修复：

1. **失败提示 30 秒去重**：同一会话 30 秒内只发一次失败提示（`_maybe_error`），
   LLM 无论重试多少次都只提示一次。
2. **已提示过用户的失败结束 planner**：失败返回时若该会话在去重窗口内已收到提示
   （提示 + reply 只会重复），同样携带 `stop_after_execution` 结束本轮。
3. 冷却限流的静默失败（未提示过）仍不结束 planner，但返回 message 明确引导
   LLM"冷却期内重试仍会失败，直接转告用户，不要连续重试"。
4. self_test 新增 3 项断言（提示去重、已提示失败结束 planner、冷却失败引导）。

## v1.4.9 (2026-09-11) 修复：语音发出后 planner 继续跑、reply 反复发文字

现象：麦麦自主语音（Tool `doubao_tts_speak`）成功发出语音后，planner 不会结束，
LLM 接着调用 reply 再发一遍文字，内容与语音重复，表现为"反复发送 reply"。

修复：工具**成功返回**时携带 `"stop_after_execution": True`（返回 dict 键，
SDK 2.8.0 的 @Tool 装饰器无此参数，键随返回值透传宿主）——本批工具执行完即结束
planner，语音之后不再生成文字回复。

- 仅成功路径携带该键：失败时（未发声、或插件已发降级/错误提示之外的场景）让 LLM
  自行告知用户，不吞掉交互。
- self_test 新增 2 项断言（成功带键、失败不带键）。
- 顺带修正 README 中工具名笔误（speak_text → doubao_tts_speak）。

## v1.4.8 (2026-09-11) 收录前审查加固（对照插件中心 AI 审查标准）

对照插件中心（plugin-repo）AI 审查（maisakagithub bot）的实测标准自查后的两处加固：

1. **命令 pattern 加 `^` 锚定**：`/说`、`/语音帮助` 此前允许句中触发
   （"你好 /说 xx" 也会命中），宿主对 pattern 走 `search` 匹配，
   不锚定会误吃句中/句尾子串（审查案例 #661 的点名标准）。
   现在只响应整条消息即命令的形态。
2. **新增冷却限流** `[behavior] command_cooldown_seconds`（默认 10 秒，0=不限流）：
   同一会话两次语音合成的最小间隔，防止刷屏与刷费用——TTS 按字符计费，
   公开命令无限流属于审查关注的滥用面（#663 标准）。
   对手动命令与麦麦自主 Tool 调用一并生效；命令被拦时向用户提示冷却，
   Tool 被拦时仅向 LLM 返回失败结果。概率模式由宿主驱动、每轮最多一次，不受影响。
3. self_test 新增 8 项断言（pattern 锚定、冷却拦截/放行/关闭、Tool 静默限流）。

## v1.4.7 (2026-09-11) 规范符合性修复

1. **宿主兼容下限抬到 1.2.4**：插件挂载的 `chat.receive.after_process` /
   `send_service.before_send` 钩点在 1.2.0~1.2.3 上不存在（钩点缺失 = 插件注册
   直接失败），manifest 的 `host_application.min_version` 如实抬到 1.2.4。
2. 修复 `/语音帮助` 的"当前音色"读错配置段（`[doubao] voice` → `[voice_tone] voice`），
   此前永远显示默认值。
3. 本地缓存目录默认值改为宿主注入的插件数据目录（`ctx.paths.data_dir`）下的
   `doubao-tts-cache/`，不再写 MaiBot 启动目录；显式配置 `cache_dir` 不受影响，
   拿不到宿主目录时退回相对路径旧行为。
4. 移除游离于宿主日志体系的模块级 `logging.getLogger`（规范要求统一走 `ctx.logger`）。
5. Command 返回值 weight 由 bool 改为 int（运行时行为不变，类型对齐 SDK 规范）。
6. 新增 `self_test.py` 本地自检（无需宿主与网络）；config.example.toml 与 README
   补齐 1.4.3~1.4.6 新增的 6 个配置项（上下文同步/跟随分段/本地缓存）。

## v1.4.6 (2026-09-11) 上下文标记默认开启 + 配置页说明修复

1. `behavior.context_prefix` 默认值从空改为 **`[语音]`**——
   写回对话上下文时自动带这个前缀，麦麦能直接看出哪句话是语音说的；
   不想要前缀仍可手动清空。
2. 修复"配置里的 description 看不到"：MaiBot 的 WebUI 配置页**只渲染
   `label` 和 `hint`，不渲染 `description`**（`dashboard/src/routes/plugin-config.tsx`），
   所以原插件大多数字段在配置页是没有任何说明的。
   本次给所有可见字段补上了 `hint`（说明文字取自 description），
   `auto_voice_mode`、`emotion_mode`、`emotion_scale_mode` 三个下拉也补了 hint
   ——下拉选项里的 description 同样不被渲染。

## v1.4.5 (2026-09-11) 修复：概率模式导致文字回复"失败"并被反复发送

根因（读宿主源码确认）：概率模式此前在 `send_service.before_send` 里**中止**原文字再另发语音。
宿主里 `before_send` 一旦 abort，`_send_to_target_with_message` 返回 None，
麦麦的 reply 工具（`src/maisaka/builtin_tool/reply.py:488-532`）会把本轮回复标记为
"可见回复生成成功，但发送失败"并把失败结果交回 planner —— LLM 看到发送失败便会重发，
于是语音和文字反复出现。

修复：概率模式不再中止原消息，统一改为**原地替换**——
在 `before_send` 里把待发消息的文本段换成语音段（`modified_kwargs.message`），
发送链正常走完：reply 工具拿到成功结果、不触发重试，消息照常入库并同步历史。

- `follow_segmentation` 关（默认）：只替换第一段，后续分段保持文字；
- 开：本轮每一段都各自转语音；
- 合成失败 / 未配 Key：该条保持文字直发，不会丢内容、不会重试循环。

宿主原生分段本身会把一条回复拆成多段循环发送（`reply.py:467`），中止任何一段都会让整个
reply 工具报失败——这是"反复发送"的机制根源，与智能分段插件无关。

## v1.4.4 (2026-09-11) 修复：关掉「手动命令」会把麦麦自主语音一起弄坏

原版把 `behavior.command_enabled` 的检查错放到了麦麦自主调用的 Tool（`doubao_tts_speak`）里：
关掉手动命令后，麦麦每次自主调用这个工具都会收到
`{"success": false, "message": "语音功能当前已禁用"}`——表现为"关掉开关后 BOT 调用插件就失败"。

修复：`command_enabled` 现在只管 `/说` `/语音` 手动命令；
麦麦自主语音仍由「自主语音方式」（`auto_voice_mode`，llm/probability/off）控制，互不影响。
配置项描述同步更正。自检新增解耦测试（关手动命令 → /说 被拒而 Tool 正常；off → Tool 被拒）。

## v1.4.3 (2026-09-11) 新增：本地合成缓存（默认关闭）

新增三个配置（`[behavior]`）：

- `cache_enabled`：默认**关**。开启后同样的「文本+音色+情感+强度+语速+音量+格式+采样率」
  直接复用上次的音频文件，不调火山接口（省钱、省等待）。缓存键就是对实际发出的合成参数做 SHA-256。
- `cache_dir`：缓存目录，留空 = MaiBot 启动目录下的 `doubao-tts-cache/`，建议填绝对路径。
- `cache_max_files`：文件数上限，默认 500，超出按最旧优先清理；命中的文件会刷新时间戳（热数据常驻）。

说明：宿主本身不持久化语音（入库只存图片、消息表里只有文字），
音频二进制原本只活在内存里，所以这个缓存也是唯一能把语音留在本地的机制。
缓存读写失败一律静默降级，不影响合成；目录建不出来时自动禁用缓存。

## v1.4.2 (2026-09-10) 新增：跟随分段发语音（默认关闭）

新增 `[behavior] follow_segmentation`，默认**关闭**。

- **关闭（默认）**：本轮只把第一条回复转成语音。若回复已被宿主后处理分段或「智能分段插件」
  切成多段，后续分段会保持文字——因为插件中止了原消息，宿主的补发链路不再触发。
- **开启**：本轮回复的**每一段**都各自转语音。
  实现方式是把待发消息的内容原地换成语音（`modified_kwargs.message`），
  **不中止发送**，于是 `send_service.after_send` 照常触发，分段插件的补发不受影响，
  它补发的每一段又会经过本钩子、各自变成语音。

细节：
- 语音段的 `data` 刻意留空，可见文本只会渲染成「[语音消息]」，
  不会把 base64 灌进麦麦的对话上下文；原文另外写入库与对话上下文。
- 开启后标记不再"发一条就消费"，改为在下一轮用户消息（入站）时归零，
  这样本轮所有分段都在生效期内。
- 合成失败或未配置 API Key 时，整条回退为文字直发，不会丢内容。

## v1.4.1 (2026-09-10) 本地修复版：补回对话上下文

原版发出语音后，麦麦下一轮**不知道自己说过什么**——这条链路断了两处：

1. 插件在 `send_service.before_send` 里把原文字 `abort` 掉，宿主 abort 后直接 `return None`，
   入库与通知记忆的步骤全部跳过；
2. 插件另发的语音走 `send.custom`，宿主 `sync_to_maisaka_history` 默认关闭；
   且语音组件的可见文本只会渲染成「[语音消息]」，所以就算同步了也只剩"发过语音"、没有内容。

本次改动：

- 发送语音时带上 `processed_plain_text`，让入库的语音消息带着文字（长期记忆能检索到）；
- 发送成功后用 `maisaka.context.append` 把原文写回对话历史（planner / replyer 读的就是它），
  `source_kind` 用 `guided_reply`（在宿主学习器的白名单内）；
- 合成失败降级成文字时同样回写，行为才一致；
- manifest 补声明 `maisaka.context.append`（缺了能力用不了）；
- 新增两个可选项：`[behavior] sync_chat_context`（默认开，关掉则退回原版行为）、
  `context_prefix`（写回上下文时加在原文前的标记，如「[语音]」，默认留空）。

插件 id 保持不变，升级不会丢配置。

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
