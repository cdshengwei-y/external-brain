# 桌宠语音问题定位报告

日期：2026-10-23
范围：ESP-SparkBot（ESP_SparkBot_MainBoard_V1.2）麦克风采集与本地唤醒
结论：**当前 12.288 MHz 方案虽然"看起来修好了"，实际上把语音变成了宽带噪声；应退回 4.096 MHz + 官方驱动。**

---

## 一、实物与官方资料核对

官方工程 <https://oshwhub.com/esp-college/esp-sparkbot>（乐鑫学院，GPL3.0）的固件在
小智 `78/xiaozhi-esp32` 仓库里有正式板卡支持：
`main/boards/espressif/esp-sparkbot/`。这是本板唯一权威的音频参数来源。

| 项目 | 官方值 | 本机记录 | 是否一致 |
|---|---|---|---|
| 主控 | ESP32-S3-WROOM-1-N16R8 | 实测 32MB Flash / 8MB PSRAM | 容量按实测为准 |
| 音频编解码 | ES8311，I2C 地址 0x18 | 一致 | 是 |
| MCLK | GPIO45 | 一致 | 是 |
| BCLK | GPIO39 | 一致 | 是 |
| LRCK/WS | GPIO41 | 一致 | 是 |
| DIN（codec→ESP） | GPIO40 | 一致 | 是 |
| DOUT（ESP→codec） | GPIO42 | 一致 | 是 |
| I2C | SDA GPIO4 / SCL GPIO5 | 一致 | 是 |
| 功放使能 | GPIO46（NS4150B CTRL） | 一致 | 是 |
| 采样率 | 16000 Hz，in=out，双工 | 一致 | 是 |

### 官方音频时钟参数（关键）

来自 `es8311_audio_codec.cc`：

```c
.mclk_multiple = I2S_MCLK_MULTIPLE_256,   // 16 kHz × 256 = 4.096 MHz
.slot_mode     = I2S_SLOT_MODE_STEREO,
.slot_mask     = I2S_STD_SLOT_BOTH,
.data_bit_width= I2S_DATA_BIT_WIDTH_16BIT,
.bit_shift     = true,
.left_align    = true,
.ws_pol        = false,
```

也就是说：**官方用的是 4.096 MHz MCLK，不是 12.288 MHz。** ES8311 的寄存器系数由
`esp_codec_dev` 的 `es8311.c` 按 (MCLK, 采样率) 自动查表写入，不是手抄固定序列。

官方麦克风增益参考值：`input_gain_ = 30`（30 dB），输出音量 70。

### 一个容易踩的硬件复用

`config.h` 里 **GPIO46 同时是功放使能 `AUDIO_CODEC_PA_PIN` 和屏幕背光
`DISPLAY_BACKLIGHT_PIN`**。官方代码因此把 `EnableOutput(false)` 做成空操作
（注释原文：*"Nothing todo because the display io and PA io conflict"*）。
换到自己的固件里如果按普通 GPIO 拉高拉低，会连带影响背光，排查播放问题时不要误判。

---

## 二、录音量化证据

用 `tools/analyze_probe_wav.py` 对 `/Users/jh-05/esp/esp_sr_probe/` 下 10 段录音统一量化：

| 文件（MCLK） | RMS | 削波% | 重复% | 底噪(100ms p5) | 动态范围 | 包络起伏 |
|---|---|---|---|---|---|---|
| 4.096 MHz 系列（raw_i2s / i2s32 / dout42 / start_after_wait / i2s_std / auto_fixed / 60s / stereo_fixed） | -7.3 ~ -8.3 | 0.15 ~ 0.21 | **64 ~ 66** | -44 ~ -51 | 39 ~ 47 | 9.8 ~ 11.8 |
| 12.288 MHz（mclk12288） | -9.8 | 0.087 | **3.2** | **-16.5** | **10.2** | 9.9 |
| 增益 0x00（gain0，时钟仍是旧配置） | -25.5 | 0.001 | 60.0 | -83.2 | 67.6 | 5.4 |

### 频段能量分布（同一段 8192 点最响窗口）

| 频段 | 4.096 MHz（raw_i2s） | 12.288 MHz（mclk12288） |
|---|---|---|
| 0–100 Hz | 30.7% | 0.9% |
| 100–300 Hz | 22.7% | 1.9% |
| 300–1000 Hz | 21.5% | 7.9% |
| 1000–3000 Hz | 19.0% | 25.5% |
| 3000–6000 Hz | 4.2% | **38.1%** |
| 6000–8000 Hz | 2.0% | **25.6%** |

### 这两组数据说明什么

1. **4.096 MHz 那批录音的频谱是正常语音**：低频 + 基频 + 语音主体共占 74.9%，
   3 kHz 以上只有 6.2%，这是人声的典型分布；动态范围 39–47 dB，说明有真实的
   说话停顿。它唯一的问题是 **65% 相邻采样重复**——每个有效采样被重复了约 2 次，
   所以听起来"失真、机械"。这是时钟比例错，不是麦克风坏。
2. **12.288 MHz 那批录音的频谱不是语音**：300 Hz 以下只剩 2.8%，3 kHz 以上占
   63.7%，动态范围掉到 10.2 dB，全程没有静音（最安静的 100 ms 也有 -16.5 dBFS）。
   这是宽带噪声，语音频段被吃掉了。
3. 结论：**768 倍频只是把"重复采样"这个显性症状压下去了，代价是把信号换成了噪声。**
   方向反了。应该回到 4.096 MHz，把寄存器表修对。
4. `gain0`（把 0x16 写成 0x00）那次电平确实降了约 16 dB，但那次的时钟仍是旧的
   4.096 配置（重复率 60%），属于**两个变量同时改动**，所以"降增益没用"这个
   结论不成立，需要重做。

---

## 三、根因（按影响排序）

### 1. 唤醒词不是"贾维斯"，是英文 "Jarvis"

`wn9_jarvis_tts` 的模型标签写在模型元信息里，实测为：

```
wakenet9l_tts1h8_Jarvis_3_0.627_0.632
```

`managed_components/espressif__esp-sr/wakeword_list.md` 第 53 行明确标注：

| 唤醒词 | 模型 |
|---|---|
| Jarvis | `wn9_jarvis_tts` |

如果你这段时间一直喊中文"贾维斯"，WakeNet **永远不会触发**，与阈值、增益、
AFE 配置都无关。想用中文唤醒词，应换成 `wn9_hilexin`（你好小鑫）、
`wn10_nihaoxiaozhi`（你好小智）、`wn10_xiaoaitongxue`（小爱同学）等中文模型，
并在 `sdkconfig` 里改 `CONFIG_SR_WN_WN*` 后重新打包 `srmodels.bin`。

**这一条不需要改任何代码就能验证：对着麦克风用英文说 "Jarvis"。**

### 2. 手抄 ES8311 寄存器表与时钟不匹配

探针里 `configure_codec_for_adc()` 是一张固定的寄存器序列，其中
`0x02/0x03/0x04/0x05/0x06/0x07/0x08` 属于时钟与 OSR 配置，这些值必须和实际
MCLK/LRCK 比例严格对应。实测表现就是"比例对不上时约 2/3 采样被重复"。

正确做法不是继续手调这张表，而是让驱动按 (MCLK, 采样率) 查表。官方用的是
`esp_codec_dev` + `es8311.c`，对应 4.096 MHz / 16 kHz 有确定的表项。

### 3. 麦克风增益用了非法编码

`ES8311_ADC_GAIN_CODE` 原值 `0x24`。寄存器 0x16 的 mic PGA 合法编码是
`0x00–0x07`（0/6/12/…/42 dB），`0x24` 不在其中，会带上未定义位，导致电平异常高
（RMS -7.5 dBFS、削波 0.2%）。官方默认约 30 dB，即编码 `0x05`。

---

## 四、建议方案

### 方案 A（推荐）：改用官方驱动，别再手写寄存器

在探针的 `idf_component.yml` 加：

```yaml
dependencies:
  espressif/esp_codec_dev: "^1.3.0"
```

然后按官方 `Es8311AudioCodec` 的做法：`i2s_std` 双工 + `I2S_MCLK_MULTIPLE_256`
（4.096 MHz）+ `esp_codec_dev_open()` 传入 16 kHz/16 bit/单声道 +
`esp_codec_dev_set_in_gain(dev, 30)`。寄存器表、模拟麦克风偏置、高通滤波全部由
驱动负责，可一次性消掉"表对不上时钟"这整类问题。

### 方案 B（最小改动）：留在 4.096 MHz，换正确表项

把 `I2S_MCLK_MULTIPLE_768` 改回 `I2S_MCLK_MULTIPLE_256`，
`fixed_mclk = 16000 * 256`，并把时钟相关寄存器按 256 倍频的表项重写
（pre_div=1, mult=1, adc_osr=0x10, dac_osr=0x20）。改动小，但仍是在手维护寄存器表。

### 方案 C（10 分钟验证硬件）：先烧一次官方固件

用 `xiaozhi-esp32` 选 `esp-sparkbot` 板卡烧一次，确认"这块板子本身能不能正常
录音和唤醒"。能，说明硬件没问题，回到方案 A/B 调软件；不能，直接查麦克风小板。
这是排除硬件变量最快的办法，建议先做。

---

## 五、立即可做的三步

1. **不改代码**：对着麦克风用英文说 "Jarvis"（不是"贾维斯"），看串口是否出现
   `WakeNet event`。这一步排除唤醒词根因。
2. **不改代码**：确认麦克风小板的 4P 1.27 mm 排针/排母插到底（官方设计中麦克风
   是独立的贴装小板，通过该连接器对接主板的 MICP/MICN/ADCVREF）。接触不良会
   表现为"有底噪、无语音"。
3. **改代码后重录，用硬指标验收**：录音必须满足

   | 指标 | 合格线 | 检查手段 |
   |---|---|---|
   | 相邻采样重复率 | < 5% | `tools/analyze_probe_wav.py` |
   | 静音段（100 ms p5） | < -55 dBFS | 同上 |
   | 说话段 RMS | -25 ~ -15 dBFS | 同上 |
   | 削波比例 | < 0.01% | 同上 |
   | 包络起伏 | > 3 dB | 同上 |
   | 频段分布 | 0–1000 Hz 合计 > 50% | 同上 `--verbose` |

   建议录音协议固定为「先静音 5 秒，再说 7 秒话」，让静音段和说话段在同一段
   文件里，才能算出真实 SNR。

---

## 六、复现命令

```bash
cd "/Users/jh-05/Documents/ChatGPT/智能中枢系统/智能中枢"
python3 tools/analyze_probe_wav.py /Users/jh-05/esp/esp_sr_probe/*.wav
python3 tools/analyze_probe_wav.py 某段.wav --verbose   # 加频段分布
```

脚本的三条判定阈值：重复% > 20 判时钟不匹配；底噪 > -35 dBFS 判"全程没有安静"；
包络 < 2 dB 判"信号恒定，不像说话"。这三条正好对应本次踩过的三个坑。

---

## 七、一句话总结

> 4.096 MHz 的录音频谱是对的、只是采样被重复；12.288 MHz 的录音重复没了、
> 但信号变成了高频噪声。**应该往 4.096 MHz 走，而不是继续往 768 倍频走**；
> 唤醒词还得从"贾维斯"改成英文 "Jarvis" 或换中文模型。
