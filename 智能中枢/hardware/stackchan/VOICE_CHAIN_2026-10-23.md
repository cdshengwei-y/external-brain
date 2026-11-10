# StackChan 语音链路分析（2026-10-23）

分析对象：`/Users/jh-05/Downloads/StackChan-项目资料-20261020`（M5Stack StackChan / CoreS3）
对应源码：`m5stack/StackChan @ 1b576559`（2026-08-19）+ 其引用的 `78/xiaozhi-esp32 v2.2.4`
目的：搞清这条链路的每一段参数与开关，判断哪些能直接搬到"录音豆 / 桌宠"上，哪些是坑。

---

## 0. 一句话结论

StackChan 的语音是一条**成熟的"官方驱动 + 双 AFE + 24k 采集/24k 播放 + 上行统一 16k Opus"**链路，
它最大的价值不是某个参数，而是**用 `esp_codec_dev` 让驱动按 (MCLK, 采样率) 自己查寄存器表**——
这正是录音豆目前手写 ES8311 寄存器表踩坑的地方。

但它也有三处明显代价：**对话阶段 AEC 是关的**、**采集增益被改成 60 dB**、
**设备端 24k 全部要重采样到 16k 才能上行**。

---

## 1. 硬件层

源自 `firmware/main/hal/board/config.h`（与 xiaozhi 官方 `boards/m5stack-core-s3/config.h` 完全一致）：

| 项目 | 值 | 说明 |
| --- | --- | --- |
| 主控 | ESP32-S3（CoreS3 模组） | 16MB Flash / 8MB PSRAM |
| 采集 codec | **ES7210**（I2C `0x40`） | 4 通道 ADC，本工程选 `MIC1\|MIC2\|MIC3` |
| 播放 | **AW88298**（I2C `0x34`） | I2S 输入 + 内置 DSP/功放，1W 喇叭 |
| 麦克风 | 双 MEMS | 官方称 "dual microphones" |
| 采样率 | **输入 24000 / 输出 24000** | `AUDIO_INPUT/OUTPUT_SAMPLE_RATE` |
| 参考通道 | `AUDIO_INPUT_REFERENCE true` | 输入按 2 通道读：ch0=麦，ch1=参考 |
| I2S MCLK | GPIO **0** | 同时也是 BOOT 脚（strap），上电瞬间别乱拉 |
| I2S BCLK / WS | GPIO **34** / **33** | |
| I2S DIN / DOUT | GPIO **14** / **13** | |
| codec I2C | SDA **12** / SCL **11** | 两个 codec 共用一条总线 |
| 显示 | 320×240 ILI9342 | 背光不走 GPIO |

时钟换算：`24 kHz × 256 = 6.144 MHz` MCLK。
**注意这个 256 倍频**——它不是随便选的，ES7210/AW88298 的寄存器系数表就是按 256× 标定的。

---

## 2. 驱动层：自己实现 `AudioCodec`，但底层走 `esp_codec_dev`

文件：`firmware/main/hal/board/cores3_audio_codec.cc`（246 行）

关键点：

1. **单 I2S 口双工**：`I2S_NUM_0` 同时开 TX/RX。
   - TX：`i2s_std`，16bit，**立体声** slot，`I2S_MCLK_MULTIPLE_256`（:109-140）
   - RX：`i2s_tdm`，16bit，**4 slot 全开**，`bclk_div = 8`（:142-176）
2. **输入按 2 通道打开**：`channel = 2`，`channel_mask = BIT0 | BIT1`（:195-206），
   即 ch0 当麦克风、ch1 当 AEC 参考。
3. **输出按 1 通道打开**（:219-232），TTS 是单声道。
4. **增益**：`input_gain_ = 60;`（:17）→ `esp_codec_dev_set_in_channel_gain(dev, BIT0, 60)`（:206）

   > ⚠️ 官方 CoreS3 板级实现这里是 **30**。diff 结果：仅此一行不同。
   > `esp_codec_dev` 的 in-gain 单位是 dB，ES7210 的 PGA 档位只有 0–37.5 dB（3 dB 一档）。
   > 写 60 要么被夹在上限、要么落到未定义档位——**和录音豆上 0x16 写 0x24 是同一类错误**，
   > 只是这次错在一个看起来"官方"的文件里。

5. AW88298 的硬件增益：`pa_voltage 5.0V`、`codec_dac_voltage 3.3V`、`pa_gain 1`（:42-50），
   软件音量默认 70（`hal_bridge::board_get_speaker_volume()`，`stackchan.cc` 尾部）。

**这一层的要点**：MCLK 只出现在 I2S 配置里，ES7210/AW88298 的寄存器（时钟分频、偏置、高通、
采样率系数）全部由 `esp_codec_dev` 内部按 (MCLK, rate) 查表写。
**没有一处手写寄存器表**，所以不存在"表对不上时钟"这一类问题。

---

## 3. 算法层：两个 AFE 实例，AEC 只开在唤醒那一路

这一段最容易看漏，因为两个 AFE 在代码里长得几乎一样，但开关相反。

### 3.1 唤醒 AFE（`audio/wake_words/afe_wake_word.cc:66-72`）

```cpp
afe_config_t* afe_config = afe_config_init(input_format.c_str(), models_, AFE_TYPE_SR, AFE_MODE_HIGH_PERF);
afe_config->aec_init = codec_->input_reference();   // ← StackChan = true，AEC 开
afe_config->aec_mode = AEC_MODE_SR_HIGH_PERF;
afe_config->afe_perferred_core = 1;
```

→ 唤醒这一路**有 AEC**，参考通道真的在用。所以理论上可以"边放音边唤醒"。

### 3.2 对话 AFE（`audio/processors/afe_audio_processor.cc:40-65`）

```cpp
afe_config->aec_mode  = AEC_MODE_VOIP_HIGH_PERF;
afe_config->vad_mode  = VAD_MODE_0;
afe_config->vad_min_noise_ms = 100;
afe_config->agc_init  = false;
#ifdef CONFIG_USE_DEVICE_AEC
    afe_config->aec_init = true;  afe_config->vad_init = false;
#else
    afe_config->aec_init = false; afe_config->vad_init = true;   // ← StackChan 走这条
#endif
```

而 `firmware/main/Kconfig.projbuild:765-774` 的 `USE_DEVICE_AEC` 依赖列表里**没有**
`BOARD_TYPE_M5STACK_STACK_CHAN`；`sdkconfig.defaults` 也没打开它。

**结论：对话阶段的 AEC 是关的，VAD 是开的。**
后果：
- 模型说话时，麦克风录到的是"自己的声音 + 环境声"，只能靠服务端或 VAD 兜；
- 运行时 `SetAecMode(kAecOnDeviceSide)` 会打印 `Device AEC is not supported`（`afe_audio_processor.cc:195`）；
- "说一句就打断机器人"这种 barge-in 体验，在这份配置下不可靠。

NS 用 `afenet`（`AFE_NS_MODE_NET`），AGC 关闭，内存分配在 PSRAM。

---

## 4. 编码与传输层：上行固定 16k Opus / 60ms

设备采集是 24k，但**上行被强制拉回 16k**（`audio/audio_service.cc`）：

| 环节 | 参数 | 位置 |
| --- | --- | --- |
| 读麦克风 | 每次请求 10ms@16k → 实际读 240 帧@24k | `:270-272` |
| 重采样 | `esp_ae_rate_cvt` 24k→16k，**2 通道交织一起重采样** | `:86-93`, `:196-207` |
| 编码 | Opus **mono 16 kHz**，帧长 **60 ms** | `:80-83`，`audio_service.h:OPUS_FRAME_DURATION_MS 60` |
| 下行 | Opus 解码到 codec 输出率（24k）；服务端参数不同则用 `output_resampler` 兜底 | `:370-378`, `:471-478` |
| 测试录音 | 同样 16k/60ms，只取左声道 | `:246-263` |

**这意味着**：24 kHz 只对播放侧有意义（TTS 更亮一点），
采集侧那 8 kHz 带宽在进 AFE 之前就被丢掉了，而且每 10 ms 要跑一次双声道重采样，白白吃 CPU。

---

## 5. 服务端与第二条通道

- 固件连的是 M5Stack 自建 Go 服务（默认 `:12800`），设备管理 + `/stackChan/ws` Avatar 通道；
  小智云端负责 ASR/LLM/TTS（`server/README.MD` 5.8 节：用 `secret_key` 换 developer token）。
- Avatar 通道自己也定义了音频消息：`0x01 Opus`、`0x18 OnAudio`、`0x19 OffAudio`
  （`server/README.MD` 11.2 节）。
- **但固件里这条音频路是空实现**：`hal_ws_avatar.cpp` 中 `DataType::Opus` 分支被注释
  （注释写着 "Opus handled in OnData Fast Path"，而该文件里没有 OnData），
  `StartAudioStream` / `StopAudioStream` 两个 case 直接 `break`。

→ 现在设备上的语音**只走小智协议一条路**；Avatar 通道只跑画面、动作、通话信令。
想自己做"手机端听设备声音"，这条通道是半成品，得补实现。

---

## 6. 与录音豆（sparkbot）的对照

| 维度 | StackChan | 录音豆（现状） | 结论 |
| --- | --- | --- | --- |
| codec | ES7210 + AW88298 | ES8311（单芯片 mic+spk） | 不同芯片，但都该走 esp_codec_dev |
| 采集率 | 24 kHz | 16 kHz | **16k 更省，够 ASR 用** |
| MCLK | 24k × 256 = 6.144 MHz | 应为 16k × 256 = **4.096 MHz** | 倍数就锁 256，别上 768 |
| 寄存器表 | 驱动查表，无人手写 | 手写表，与时钟对不上 | **这是录音豆的病根** |
| 增益 | 60 dB（官方 30，超 PGA 范围） | 曾写 0x24（非法档位） | **同一类错误** |
| 上行编码 | Opus 16k / 60ms | HTTP 上传整段 WAV | 交互式要改用流式 |
| 唤醒 | wn9_histackchan_tts3 = **"Hi, Stack Chan"** | wn9_jarvis = **"Jarvis"** | 两边都是英文词，喊中文不会醒 |
| AEC | 唤醒开 / 对话关 | 无 | 想打断必须补 |
| 参考通道 | 有硬件参考（ch1），对话阶段没接进 AEC | 无 | — |

---

## 7. 可以直接搬的四条经验

1. **别手写寄存器表**。`idf_component.yml` 加 `espressif/esp_codec_dev ~1.5.4`，
   实现一个 `Es8311AudioCodec : AudioCodec` 就够，MCLK 交给 I2S 配置、系数交给驱动。
2. **MCLK 倍数固定 256**。24k 就是 6.144 MHz，16k 就是 4.096 MHz。
   用倍数去"凑"采样重复率，只会把语音换成噪声（这也是 768 倍频失败的真正原因）。
3. **采集增益留在 PGA 合法区间**。ES8311/ES7210 的 mic PGA 都以 dB 计、上限约 37–42 dB，
   官方默认 30 dB 是留了余量的值；靠 60 dB 提灵敏度，提上来的是失真。
4. **采集率和上行率解耦**。设备按硬件最舒服的率采，AFE/编码前重采样到 16k，
   不要为了凑 16k 去改硬件时钟。

---

## 8. 复现与取证书

```bash
# 资料包内（19 个摘录文件）
03-官方源码摘录/source-notes/firmware/main/hal/board/config.h
03-官方源码摘录/source-notes/firmware/sdkconfig.defaults
03-官方源码摘录/source-notes/firmware/main/Kconfig.projbuild      # 765-774: USE_DEVICE_AEC 依赖
03-官方源码摘录/source-notes/firmware/main/hal/hal_ws_avatar.cpp  # 0x01/0x18/0x19 空实现

# 上游完整实现（资料包未含，本次按 commit 拉取核对）
m5stack/StackChan @1b576559
  firmware/main/hal/board/cores3_audio_codec.cc   # :17 增益 60、:114 256×、:142-176 TDM
  firmware/main/hal/audio.cpp                     # 3 秒自录自放 + 128 点波形
  firmware/main/apps/app_setup/workers/audio.cpp  # 装机自检的麦克风测试入口
78/xiaozhi-esp32 @v2.2.4
  main/audio/audio_service.cc                     # :80 16k、:86 重采样、:253/270 帧长
  main/audio/processors/afe_audio_processor.cc     # :40-65 AEC/VAD 开关
  main/audio/wake_words/afe_wake_word.cc          # :66-72 唤醒 AEC 打开
```

`cores3_audio_codec.cc` 与官方 `boards/m5stack-core-s3/cores3_audio_codec.cc` 逐行 diff：
**只有 `input_gain_` 一行不同（30 → 60）**，其余完全一致。板卡本身由 StackChan 自己的
`hal/board/stackchan.cc` 提供（`DECLARE_BOARD(M5StackCoreS3Board)`，:658），不依赖 xiaozhi 的板卡目录。

---

## 9. 如果要接你们的中枢

StackChan 说的是**小智 WebSocket 协议**（hello / listen / stt / tts / opus 二进制帧），
而中枢现在是 `/api/v1/portable/v1/{media,requests,audio}` 这套 HTTP + 整段 PCM。
两者不兼容，三条路：

| 方案 | 改动量 | 代价 |
| --- | --- | --- |
| 固件改协议（把 xiaozhi protocol 换成自有 HTTP） | 大，且失去 OTA/唤醒/Avatar 生态 | 不推荐 |
| **在 Mac/服务器上写一个"小智协议 → 中枢 HTTP"桥** | 中，固件零改动 | 推荐；语音仍是 16k Opus 流 |
| 只借 StackChan 当"嘴和耳朵"，业务走 MCP | 小，用 `hal_mcp.cpp` 现成工具链 | 需要中枢提供 MCP 工具 |

第二条路和现有 `agent_gateway` 的定位最贴：桥接层同时握有 Opus 解码和中枢的请求/确认/TTS 接口，
不用动固件，也不用改中枢的 HTTP 语义。
