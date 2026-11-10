# RK3588 网关与中枢适配

## 架构边界

外部硬件是入口和执行对象，RK3588 是当前服务器与硬件路由节点，链路是双向的：

```text
外部硬件 / 上位机 / 桌宠
        ⇅ 请求、语音、状态、事件
RK3588 Server
        ⇅ 结构化计划、确认、执行结果
AgentOS（后续由同事接入）
        ⇅
设备执行适配层 / HTTP-MQTT 兼容桥
        ⇅
录音豆、手机及其他硬件
```

当前本仓只负责验证 AgentOS 接入前的基线：

```text
上位机 → RK3588 → HTTP-MQTT 兼容桥 → MQTT → 录音豆
录音豆 → MQTT ACK / 状态 → 兼容桥 → RK3588 / 上位机
```

后续 AgentOS 插入 RK3588 和设备执行适配层之间；本仓不在本轮代替同事实现 AgentOS 接入。

上位机不再把中文直接转换成硬件命令。所有用户消息先由 `RK3588AgentClient.route_message()` 发送到 RK3588 的 `task=auto` 自动路由；只有路由结果为硬件命令且返回结构化 `plan_id` 时，才进入用户确认和执行流程。

当前支持：

- 开始录音；
- 停止录音；
- 查询状态；
- 查看设备。

2026-10-16 复验结果：RK3588 健康检查、设备目录、自动路由和“开始录音”结构化计划均正常；真实执行尚未通过，因为录音豆当前 MQTT `availability` 为 `offline`，兼容桥按设计拒绝下发命令。设备重新上线后，先验证 `status.read`，再验证 `record.start` 和 `record.stop` 的真实 ACK。

桌面端回复语音当前使用 macOS 系统 TTS（`say` + `afplay`）按需生成和播放，便于先验证界面交互。RK3588 的 TTS 适配器已保留，后续可在服务速度稳定后切换。

上位机入口位于项目根目录的 `外接大脑上位机.app`。当前 RK3588 的 `dispatch=false` 规划接口仍需同事完成稳定性联调；接口未返回合法 `plan_id` 时，上位机会失败提示，不会绕过规划直接控制硬件。

随身设备接口已经提供第一版文字和音频入口：

```text
POST /api/v1/portable/v1/media
POST /api/v1/portable/v1/requests
GET  /api/v1/portable/v1/requests/{request_id}
POST /api/v1/portable/v1/requests/{request_id}/confirm
```

音频请求会从 SQLite 读取已上传的音频，转换为千问 ASR 服务所需的 `multipart/form-data`，调用 `http://192.168.0.153:18005/transcribe` 的 `file` 字段获取文字，再把文字送入 RK3588 的 `POST /api/route` 自动路由。转写结果会保存到请求的 `transcript` 字段；未配置音频理解时会明确返回 `AUDIO_UNDERSTANDING_NOT_CONFIGURED`，不会执行硬件命令。

已确认的千问 ASR 服务地址为 `http://192.168.0.153:18005`，提供：

```text
GET  /health
POST /transcribe     multipart 字段：file、language
```

已确认的 RK3588 测试中枢服务地址为 `http://192.168.0.140:8095`，提供：

```text
GET  /api/health
GET  /api/mode
POST /api/route
POST /api/asr
POST /api/tts
```

项目中的 `RK3588AgentClient` 统一使用 `protocol_version`、`request_id`、`conversation_id`、`target_device_id`、`plan_id` 和 `source`；硬件命令仍然必须交给 `HubService` 做能力、权限、队列和 MQTT 校验。

桌面聊天入口必须使用带 V1 上下文的 `/api/route` 自动路由，不应在桌面端自行判断聊天和命令。RK3588 自动路由返回聊天时直接展示，返回硬件计划时再交给硬件网关接口执行。旧版 `/api/agent` 仅作为历史通用智能体接口，不作为当前硬件联调入口：

```text
GET  /api/hub/v1/health
GET  /api/hub/v1/devices
POST /api/hub/v1/devices
POST /api/hub/v1/commands
POST /api/hub/v1/commands/{request_id}/confirm
```

硬件网关接口支持 `dispatch=false` 的先规划、`dispatch=true` 的执行和高风险动作确认。`RK3588AgentClient` 已经封装这组接口，并支持可选的 `X-Hub-Key`。

当前真实录音豆仍然使用 MQTT 协议。RK3588 网关下发的 HTTP `/command` 通过中枢进程中的 HTTP-MQTT 兼容桥转换为现有 `HubService` 命令，再由 MQTT 发送给录音豆，不修改录音豆固件协议。启动方式见项目根目录 `README.md` 的兼容桥章节。

## 意图路由层（第③步「判定意图」）

`portable.py` 的第③步不再写死在关键词里，拆成三层：

| 层 | 位置 | 职责 |
| --- | --- | --- |
| 快通道 | `portable.py` 的 `_is_wake_phrase` / `_is_exit_phrase` / `_is_noise_transcript` / `_music_action` | 唤醒、退下、噪声、播放中控制；0 延迟、断网可用 |
| 主通道 | `intent.py` 的 `IntentRouter` + `swbot.SwBotIntentClassifier` | 一次 bot 调用同时给出 `intent` + `slots` + `reply` |
| 插件 | `portable.py` 的 `ChatPlugin` / `FountainPlugin` / `MusicPlugin` / `SleepPlugin` | 决定 `session_action` / `device_action` / 是否合成语音 |

分类器返回的不是 JSON、超时或抛错时，`IntentRouter.resolve()` 返回 `None`，
调用方自动退回改造前的关键词分支，行为与改造前一致。

新增一种能力只要两步：

1. 写一个 `Plugin` 子类，声明 `intents` 和 `catalog_lines()`，在 `handle()` 里返回
   `IntentResult(session_action=..., reply=...)`；
2. 在 `PortableRequestService._build_intent_router()` 里注册。

不需要动第③步的 if 链，也不需要改固件 —— 只要复用已有的 `session_action`。

### 相关环境变量

```text
SWBOT_BASE_URL=https://bot.vx123.xin
SWBOT_EMAIL=...            # 缺这两项时 --swbot 不启用
SWBOT_PASSWORD=...
SWBOT_BOT=硬件             # 目标 Bot 名，留空用第一个
SWBOT_VOICE_PREFIX=...     # 闲聊时的「口语回答」前缀；意图路由会自动跳过
PET_INTENT_RULES_FIRST=0   # 0=bot 先判意图（默认）；1=关键词优先，动作 0 延迟
```

启动：`python run.py --port 18787 --swbot --local-tts`
（`--swbot` 与 `--demo-agent` / `--agent` 互斥）

### 尚未接入的插件

硬件动作（灯、舵机、RK3588 命令）还没做成 plugin —— 需要先决定
`--swbot` 模式下 MCP 客户端（`McpStdioClient`）由谁持有：bot 只给意图，
实际下命令仍然必须过 `HubService` 的能力与权限校验。

## 设备插件：查状态 / 改设置（device_query、device_set）

用户问「现在音量多少」时，模型原本只能现编 —— 中枢根本不知道设备状态，
于是出现「不知道音量，但可以说调大调小」。这里补上设备影子：

| 层 | 位置 | 职责 |
| --- | --- | --- |
| 设备影子 | `device.py` 的 `DeviceRegistry` | 每台设备的说话音量、音乐档位、屏幕、喷泉、电量；落盘 `~/.sparkbot/devices.json` |
| 状态注入 | `intent.py` 的 `build_intent_prompt(device_context=...)` | 一行实时状态进提示词，模型回答「多少」时有据可依 |
| 插件 | `portable.py` 的 `DevicePlugin` | `device_query` 用真实值作答；`device_set` 改档位并给出 `device_action` |
| 生效点 | `portable.py` 的 `get_audio` | 下发前按档位缩放 PCM，改完下一句立刻听得出来 |

### 两条不能破的规则

1. **说话音量以中枢为准**。设备上报里的 `volume_step` 会被忽略，否则中枢
   刚调完、设备一条旧状态回来就把它覆盖了。设备只能改自己的音乐档位。
2. **数字只有一个来源**。查询的回复由 `DeviceRegistry` 生成确定文案，
   不采用模型写的那句 —— 提示词注入只是为了让它理解上下文，不是让它报数。

### 说话音量 5 档

| 档位 | 增益 | 听感 |
| --- | --- | --- |
| 1 | 0.35 | 很小 |
| 2 | 0.50 | 偏小 |
| 3 | 0.65 | 正常 |
| 4 | 0.80 | 偏大（默认） |
| 5 | 1.00 | 最大（TTS 原始电平，不放大） |

增益在 `get_audio` 里做，所以 TTS 缓存不必按档位拆分，改完音量连已经生成
好的音频也会跟着变。每次下发会打一行
`[AUDIO] 说话音量 2/5 档 gain=0.50 peak 22126->11063`，看日志就知道生效没生效。

出厂默认档位用环境变量 `PET_DEFAULT_VOLUME=2`（写进 `config/plugin.env`），
只对「影子里还没有这台设备」生效；已经调过的档位以 `devices.json` 为准。

不想说话改档位时，用命令行（走 HTTP，不会和运行中的中枢抢文件）：

```bash
python3 tools/pet_volume.py            # 看当前档位
python3 tools/pet_volume.py 2          # 设为 2 档
python3 tools/pet_volume.py --delta -1 # 降一档
```

对应接口：`GET /api/v1/portable/v1/devices/{id}`、
`POST /api/v1/portable/v1/devices/{id}/volume`（`{"step":2}` 或 `{"delta":-1}`）。

**默认音量只覆盖说话（TTS）。** 音乐音量是设备播放器自己的 4 档
（`kVolumeDbList`），中枢只记录、不缩放，这是两套独立的值。

### 设备上报协议（可选字段）

请求体里加 `device_state` 即可，中枢缺字段就保留旧值：

```json
{
  "portable_device_id": "sparkbot_wearable_001",
  "device_state": {
    "music_step": 1,
    "music": "playing",
    "fountain": false,
    "screen": "on",
    "battery": 78
  }
}
```

固件侧在 `submit_audio_request()` 里拼这段，取值来自
`music_player::volume_step_value()` / `music_player::active()` /
`fountain::active()` / `g_display_on`。

### 本地快通道

「现在音量多少」「大点声」「音量调到 3」这类不经过模型：答案和值都在
`DeviceRegistry` 里，排一次 3~7 秒的模型队列没有意义。实测 0.8 秒（含 TTS）。

播放中的「大点声」仍由 `MusicPlugin` 接走，改的是设备音乐档位 ——
同一个词在两种上下文里含义不同，顺序不能反。

## 语音门控：区分「跟桌宠说话」和「房间里的闲谈」

没有唤醒词（KWS）时，VAD 只要听到人声就当指令上传，中枢一接就回一句
「嗯，我听着呢」—— 听起来像它在插话。实测两分钟里 8 句里有 5 句是环境闲聊。

`voice_gate_allows()` 的规则（`PET_VOICE_GATE=1`，默认开）：

| 情况 | 行为 |
| --- | --- |
| 句子里有意图词（放/听/歌/音量/喷泉/退下/几点/录音/灯…） | 应答，并刷新会话窗口 |
| 是完整问句（有疑问词，或≥6 字且带问号） | 应答，并刷新会话窗口 |
| 距上次交互 < `PET_VOICE_WINDOW`（默认 45s） | 应答（连续对话不被打断） |
| 其他 | 静默丢弃：`response_text=""`、不合成语音、不打断音乐 |

「完整问句」这条是 2026-11-06 补的：设备录音修好后 ASR 给出完全正确的
「成都今天的温度怎么样？」，但句子里没有「天气」「几度」这类意图词，被门控
当环境音丢掉 —— 用户看到的就是「说了没反应」。继续加意图词只能补一次漏，
换成认问句形态一次覆盖一整类。带问号的语气词（「是吧？」）字数不够仍会丢。

只对 `source=wearable` 且 `input_type=audio` 生效 —— 文本指令不能丢。
日志里会打 `[GATE] 忽略环境语音：...`，方便回看误杀。

## 空录音：别送 ASR

VAD 门限低，咳嗽、翻身、风扇都会触发一次上传。这类录音送进 ASR 不会返回空，
而是「嗯。」「咳。」甚至「朝鲜族女性。」（同一段底噪三次识别三句不同的话）。

`audio_silence_note()` 在送 ASR 之前先算整段 RMS：低于 -31 dBFS 直接跳过，
日志打 `[MIC] 跳过识别：整段只有底噪…`。判断依据必须是 RMS 而不是峰值 ——
固件把峰值归一化到 0.7（≈ -3 dBFS）之后，纯底噪也顶得到 -3 dBFS。
实测正常说话 RMS 在 -14 ~ -25 dBFS，只有底噪的在 -34 dBFS 左右，留了 6 dB 余量。

这不是终极方案：真正稳的是 ESP-SR WakeNet 唤醒词门控，门控只是不引入新模型
时的过渡。`PET_VOICE_GATE=0` 可退回「什么都回」。

## 点歌查询：先把口语收敛成「歌名 歌手」

用户点歌会夹一堆说明：「我要听单字一个枫，枫叶的枫，周杰伦的枫」。
整句拿去做 query，网易云只会返回《枫 - 全网找歌君》这类翻唱。

`_song_query()` 的做法：

1. 按标点分句、去掉「单字一个/歌名是/的音乐/吧」这类填充；
2. 取最短的一句当歌名（`枫`）；
3. 从「周杰伦的枫」里取歌手（`枫` ← `周杰伦`），并把书名号引号清掉；
4. 输出 `枫 周杰伦`。

点歌时若点名了歌手，先按歌手收敛候选（`_artist_matches`，含 Jay/周董等别名）。
原唱拿不到时**明说**：「网上没找到周杰伦的原唱，先放《枫 - 全网找歌君》。」
不再把翻唱当原唱播报。

时间/日期也已走本地快通道（`_try_local_facts`）：问「现在几点」从 6.5s 降到 2.7s，
且不会回「我这边看不到实时时间」。

## 唤醒词：靠尾音兜底，不靠逐字匹配

唤醒链路是「设备录 3s → 上传 → Qwen ASR → 关键词匹配」，**没有本地 KWS**。
Qwen3-ASR 服务只接受 `file` + `language`，没有热词/上下文参数，所以纠正只能
放在中枢侧。

实测（`portable_requests` + `portable_media` 里的原始 wav）：

| 时间 | 录音 | peak | ASR 结果 |
| --- | --- | --- | --- |
| 16:18:56 | 2.37s | 3029（凑近喊） | 查维斯。 |
| 16:18:31 | 2.94s | 933（房间噪声） | 嗯。 |
| 16:19:24 | 2.88s | 884（房间噪声） | 嗯。 |

结论：**采集和 ASR 都没问题，是关键词表漏了**。同一个 wav 重跑 ASR 稳定
返回「查维斯。」，可复现。

`_is_wake_phrase()` 现在的规则，按顺序：

1. 先剥标点（ASR 给中文短句一律带句号，`"贾维斯。"` 必须等价于 `"贾维斯"`）；
2. 完整别名表（贾维斯 / 贾文思 / 贾维 / 查维 / Jarvis）；
3. **尾音兜底**：出现 `维斯/维思/威斯/威思/维丝/维司/伟斯/维基` 就认 —— 这组
   尾音在中文里没有别的语义，首字错成查/杰/雅/扎都不影响；
4. 尾字被吃掉（只剩「贾维」「查维」）：有「维」且首字在近音集合里就认。

**只认「贾维斯」**（2026-11-02 按用户要求）：原来的「你好小智 / 小智同学 /
晓智 / 小志」一族已全部删除，包括为它准备的「你好」「在吗」短招呼兜底。
现在这些词只是普通聊天内容，不再唤醒桌宠。

`PET_WAKE_WORDS=小豆,豆豆` 可加自定义词，不用改代码。
每次唤醒都会打一行 `[WAKE] 转写=... -> 唤醒/未命中`，失败也能追。

**表情态（亮屏）也认唤醒词**：以前「贾维斯」在闲聊分支里没有意图词，会被
语音门控当环境音静默丢掉，用户感觉是「喊了不理」；现在明确回一句「我在」。

**局限**：三个字的专名对 ASR 本身就吃力（会转成「查维斯」），两字词更稳。
要彻底摆脱 ASR，需要固件上 ESP-SR WakeNet 做离线唤醒，那是下一步。

### 让「贾维斯」认得更准：只送最响的那一簇

同一段真机录音（16k / 2.37s / peak 1729），只改送进去的那一段，结果完全不同：

| 送进 ASR 的音频 | 结果 | 是否唤醒 |
| --- | --- | --- |
| 整段（含前后房间底噪） | 咋回事？ | 否 |
| 剪首尾静音，保留 1.9s 跨度 | 也為。 | 否 |
| **只留最响的一簇（±0.2/0.3s）** | **查维斯。** | **是** |

关键在于「取哪一段」而不是「剪不剪静音」：房间底噪、关门声、远处说话都在
别的位置，整段送进去等于把噪声一起喂给模型。`agent_gateway/wake_audio.py`：

* `loudest_span()`：按 25ms 窗口求峰值，从全局最响的窗口向两侧扩张，
  门限 `max(峰值*35%, 200)`，允许簇内 50ms 掉线（字间停顿），
  帧边界再加前 0.2s / 后 0.3s 气口；整段峰值低于 200 视为纯底噪，返回 None。
* `prepare_wake_wav()`：裁完把峰值归一化到 0.7 满量程（ASR 对电平一致性敏感）。
  纯 Python，2.9s 录音约 2~7ms。
* `wav_peak()`：读整段峰值，用来判断「是不是有人凑近说话」。

`_transcribe_wake_request()` 最多三遍，任一命中即唤醒：

1. `wake-trim`：最响簇 + 前 0.2 / 后 0.3 秒气口（大部分情况一次就中）；
2. `wake-wide`：气口放到 0.3 / 0.45 秒，兜住「裁剪削掉音头」；
3. `wake-raw`：原音频，兜住裁剪逻辑本身不适用。

2、3 只在前面没命中且 `peak ≥ 1200`（房间底噪约 800）时才跑，噪声片段不会
白花两三次 ASR 时间。

实测重放 16:43:26 那条（改前转写「咋回事？」→ 不唤醒）：`wake-trim` 命中
`查维斯。`，`session_action=awake`，全程 326ms。

**仍是风险点**：设备待机采集上限是 3 秒（`capture_until_silence(3, …)`）。
如果「贾维斯」正好落在这一轮的第 2.5 秒之后，尾音会被切断，靠裁剪救不回来 ——
需要固件把「采集长度」改成「从语音起点开始计」，并裁掉多余的前置缓冲。
