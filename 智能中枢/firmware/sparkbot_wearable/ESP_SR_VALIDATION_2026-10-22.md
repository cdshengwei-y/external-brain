# ESP-SR 接入验证记录

日期：2026-10-22

## 已验证

- PlatformIO Core：6.1.19。
- 默认 `platform = espressif32` 解析到 8.0.0，但该平台要求下载
  `toolchain-riscv32-esp 14.2.0+20260121`；本机下载停留在 0%，因此未完成默认平台构建。
- 本机已缓存 `espressif32@6.5.0` 及 Arduino-ESP32 2.0.14 工具链。
- 使用临时项目配置将平台指定为 `espressif32@6.5.0` 后，以下环境均成功编译：

  - `sparkbot_actual`：RAM 13.1%，Flash 14.0%。
  - `audio_capture`：RAM 15.0%，Flash 17.4%。

- 临时配置未写回 `platformio.ini`，没有烧录设备。

## ESP-SR 兼容性结论

StackChan 使用的是 ESP-IDF Component Manager 依赖：

```yaml
espressif/esp-sr: ~2.3.0
```

该依赖不是可直接填入 `lib_deps` 的 Arduino 库。StackChan 的组件声明还要求
ESP-IDF `>=5.5.2`。

## 已完成的独立探针验证

已安装以下环境：

- ESP-IDF `v5.5.2`：`/Users/jh-05/esp/esp-idf`
- ESP32-S3 工具链和组件管理器 Python 环境
- 独立探针：`/Users/jh-05/esp/esp_sr_probe/esp_sr_probe`

探针使用 `idf.py add-dependency "espressif/esp-sr^2.3.0"`，在 ESP32-S3 目标下
完成 `idf.py build`。组件管理器当前将该范围解析为 `esp-sr 2.6.0`，并同时解析
`esp-dl`、`esp-dsp` 等依赖。该构建只验证组件解析和链接，没有执行烧录。

因此现在已经证明 ESP-IDF 侧的 `esp-sr` 接入链路可用；独立探针还完成了官方
`wn9_jarvis_tts` 模型选择、模型分区打包和 WakeNet API smoke test 的编译。
尚未把 WakeNet/AFE 代码合入 SparkBot 主工程，也没有烧录或运行时验证。

## 下一步前置条件

下一步应在该探针中加入持续 I2S 输入和 AFE feed/fetch 任务，确认真实麦克风的
音频帧格式、模型内存和 WakeNet 事件后，再把已验证的组件逐步合并回
`sparkbot_wearable`。

## 第二阶段：AFE + I2S 编译验证（2026-10-23）

已在独立探针中加入：

- `afe_config_init("M", ..., AFE_TYPE_SR, AFE_MODE_LOW_COST)`；
- 仅启用 `wn9_jarvis_tts` WakeNet，暂时关闭 AEC、SE、NS、VAD 和 AGC；
- ESP32-S3 legacy I2S RX：16 kHz、16 bit、单左声道；
- ESP-IDF legacy I2C 初始化 ES8311：地址 `0x18`、SDA `GPIO4`、SCL `GPIO5`、100 kHz；
- 沿用主工程已验证的 ES8311 ADC-only 寄存器序列，保持 DAC 和 NS4150B 关闭；
- 按 AFE feed chunk 从 GPIO40 读取音频，并通过 `feed/fetch_with_delay` 运行双任务；
- 串口打印 WakeNet 状态、唤醒词索引、音量、VAD 状态和堆内存。

构建命令：

```bash
cd /Users/jh-05/esp/esp_sr_probe/esp_sr_probe
source /Users/jh-05/esp/esp-idf/export.sh
idf.py build
```

结果：构建成功，应用镜像约 `0xD9FA0`，2 MiB 应用分区剩余约 57%；`idf.py size`
显示 DIRAM 使用 33.33%，IRAM 使用 93.73%。IRAM 余量较小，后续启用 NS/AGC/AEC
前需要重新评估资源。当前只完成编译和链接，没有烧录；真实 ES8311 控制寄存器初始化、
麦克风运行时帧和唤醒事件仍待连接实机后验证。构建中的 legacy I2S 弃用提示属于 API
警告，不影响本次链接结果。

## 实机运行验证（2026-10-23）

设备通过 `/dev/cu.usbmodem13101` 连接后，已烧录独立探针的 bootloader、分区表、
应用和 `srmodels.bin`，四段写入均通过 SHA 校验。启动日志确认：

- ESP32-S3 revision 0.2、8 MB PSRAM 正常；
- 模型分区成功加载 1 个模型，AFE pipeline 为单麦克风 `WakeNet(wn9_jarvis_tts)`；
- ES8311 I2C 应答正常：`SYSTEM0D=0x01`、`ADC_GAIN=0x24`；
- `fetch` 持续返回 1024 字节（512 samples），输入音量约 `-48` 至 `-52 dBFS`；
- 持续运行约 2 分钟无崩溃、I2C 错误或 AFE fetch 超时，本轮未捕获 WakeNet 事件。

设备当前运行的是独立探针固件，不是 `sparkbot_wearable` Arduino 主固件；后续需在
更安静、距离和音量可控的条件下单独评估“贾维斯”唤醒灵敏度，再决定是否合并主工程。

## Flash 配置修正与第二次实机启动（2026-10-23）

首次镜像头将 Flash 容量标为 8 MB，而实机检测到的 Flash 为 32 MB。已将探针的
`CONFIG_ESPTOOLPY_FLASHSIZE` 修正为 `32MB`，重新构建并使用 `--flash_size 32MB`
烧录。第二次启动日志确认：

- `SPI Flash Size : 32MB`，不再出现实测容量大于镜像头容量的告警；
- bootloader、分区表、应用和 `srmodels.bin` 均写入并通过 SHA 校验；
- `wn9_jarvis_tts`、AFE、I2S RX 和 ES8311 初始化均正常；
- `fetch` 持续返回 `1024` 字节，静音基线约 `-49` 至 `-53 dBFS`，期间无崩溃、I2C
  错误或 AFE fetch 超时；
- 本次人工语音测试窗口未捕获 `WakeNet event`，因此尚不能据此判定唤醒灵敏度或
  “贾维斯”唤醒成功。

本次烧录仍为独立 ESP-SR 探针固件，未覆盖 `sparkbot_wearable` 主工程；在确认唤醒词
事件和资源余量前，不合并回主工程，也不启用 NS/AGC/AEC。

## 原始麦克风录音验证（2026-10-23）

为绕过 WakeNet 事件，已将独立探针改为自动录音模式：启动后等待 10 秒，使用同一套
ES8311 ADC 和 I2S RX 链路采集 60 秒单声道 PCM，并通过 USB 串口导出 WAV。录音文件
`/Users/jh-05/esp/esp_sr_probe/esp_sr_probe_60s.wav` 校验结果为：16 kHz、16 bit、
单声道、60.000 秒、数据区 1,920,000 字节。整体 RMS 约 `-8.3 dBFS`，电平偏高且有
少量采样点达到满量程，试听时需留意轻微削波；本次录音未启用 NS、AGC 或 AEC，反映
的是当前 ADC 原始输入质量。

第二次录音已改为 I2S `RIGHT_LEFT` 双槽读取，再提取左声道，并确认录音在
`RECORD_BEGIN` 后才开始。但导出的 `esp_sr_probe_60s_auto_fixed.wav` 仍有约
`-7.6 dBFS` RMS、约 0.7% 采样点触顶和大量重复采样，声音仍明显失真。因此当前
不能把问题归因于 WakeNet 阈值，也不能继续合并主工程；下一步需暂停 AFE，单独按
主工程 `audio_capture.cpp` 的原始双声道采样路径做诊断录音，确认 I2S 帧宽、时钟和
ES8311 ADC 输出格式后再恢复唤醒测试。

## 原始 I2S 诊断探针（2026-10-23）

已在独立探针中完全旁路 AFE/WakeNet，仅保留 ES8311 ADC 初始化和主工程的原始采样路径：
每次读取 `int16_t capture_buffer[2048]`，按交错双槽提取 `capture_buffer[frame * 2]`，并将
I2S DOUT 也绑定到主工程使用的 GPIO42。设备连续运行和 USB WAV 导出均稳定，未再发生任务栈
崩溃；烧录仍只写独立探针，不覆盖主工程。

录音文件：

- `/Users/jh-05/esp/esp_sr_probe/esp_sr_probe_60s_raw_i2s.wav`
- `/Users/jh-05/esp/esp_sr_probe/esp_sr_probe_60s_raw_i2s_dout42.wav`

两次文件均为 16 kHz、16 bit、单声道、60.000 秒。统计结果基本一致：

- `raw_i2s`：RMS `-7.57 dBFS`，峰值 `32768`，触顶比例 `0.208%`，相邻采样重复比例 `65.38%`；
- `raw_i2s_dout42`：RMS `-7.32 dBFS`，峰值 `32768`，触顶比例 `0.214%`，相邻采样重复比例 `65.54%`。

结论：问题已经稳定复现于 ES8311/I2S 原始采样链路，与 AFE/WakeNet 无关；严格复刻主工程的
采样缓冲、双槽提取和 GPIO42 DOUT 绑定仍未改善。当前不应继续调 WakeNet 阈值或合并主工程，
下一步应针对 I2S slot/frame width、BCLK/LRCK 时序以及 ES8311 ADC 输出格式做寄存器级对照。

随后又做了两轮隔离验证：

- 将 I2S/ES8311 初始化延后到等待窗口结束后再开始读取，结果为 RMS `-7.75 dBFS`、
  重复采样 `65.31%`；排除 DMA 长时间无消费者造成的启动状态问题；
- 将 I2S RX 改为 32-bit slot，并提取高 16 位输出，结果为 RMS `-7.49 dBFS`、
  触顶 `0.192%`、重复采样 `65.96%`；排除简单的 16/32-bit 读取宽度问题。

对应文件：`/Users/jh-05/esp/esp_sr_probe/esp_sr_probe_60s_start_after_wait.wav`、
`/Users/jh-05/esp/esp_sr_probe/esp_sr_probe_60s_i2s32.wav`。当前设备运行最后一版 32-bit
诊断探针，主工程仍未烧录。

## ES8311 MCLK 修正验证（2026-10-23）

确认前述寄存器系数对应 `12.288 MHz MCLK`，而探针此前实际输出为 `4.096 MHz`。已将独立
探针改为官方 `i2s_std` Philips stereo 驱动，并输出 `16 kHz * 768 = 12.288 MHz` MCLK，
随后烧录并完成 60 秒录音。

修正后文件：`/Users/jh-05/esp/esp_sr_probe/esp_sr_probe_60s_mclk12288.wav`

统计结果：RMS `-9.82 dBFS`，触顶比例 `0.0865%`，相邻采样重复比例 `3.25%`。与此前
约 `65%` 重复采样相比，波形已恢复连续，确认爆音的主要根因是 ES8311 时钟系数与实际
MCLK 不匹配。当前仍有少量削波，下一步应先试听并调整 ADC 增益，再考虑将正确的 MCLK
和寄存器配置合入 `sparkbot_wearable` 主工程。

## 方向修正（2026-10-23 复盘）

上述"12888 修正有效"的结论只看了**采样重复率**一个指标，没有检查频谱，需要修正。
用 `tools/analyze_probe_wav.py` 对 10 段录音统一量化后发现：

| 指标 | 4.096 MHz（raw_i2s 等） | 12.288 MHz（mclk12288） |
|---|---|---|
| 相邻采样重复 | 65% | **3.2%** |
| 静音底噪（100 ms p5） | -51 dBFS | **-16.5 dBFS** |
| 动态范围 | 46.7 dB | **10.2 dB** |
| 0–1000 Hz 能量占比 | 74.9%（正常语音） | **10.7%（语音频段被吃掉）** |
| 3–8 kHz 能量占比 | 6.2% | **63.7%（宽带噪声）** |

即：12.288 MHz 只是把"采样重复"这个显性症状压下去了，代价是**信号变成了高频噪声**，
语音低频几乎全丢、全程没有静音段。而 4.096 MHz 那批录音的频谱是正常语音，仅存在
每个采样被重复约 2 次的失真。

因此**方向应回到 4.096 MHz（256 倍频，与官方 esp-sparkbot 板卡一致）**，并改用
`esp_codec_dev` 的 es8311 驱动按 (MCLK, 采样率) 自动查表，而不是继续手抄寄存器序列。

另外确认两件事：

- `wn9_jarvis_tts` 的实际唤醒词是英文 **"Jarvis"**（模型标签
  `wakenet9l_tts1h8_Jarvis_3_0.627_0.632`，见 `wakeword_list.md`），喊中文"贾维斯"
  不会触发；
- 寄存器 0x16 的 mic PGA 合法编码是 `0x00–0x07`（0–42 dB），原值 `0x24` 是非法编码。

完整证据、根因和建议方案见同目录 `AUDIO_ROOT_CAUSE_2026-10-23.md`。
