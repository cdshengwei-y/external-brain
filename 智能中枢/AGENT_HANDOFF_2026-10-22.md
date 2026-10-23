# 智能中枢项目交接文档

交接日期：2026-10-22
交接范围：SparkBot 桌宠本地语音唤醒与语音回复优化

## 一、当前目标

把桌宠当前“服务端 ASR 判断唤醒词”的方案，升级为更流畅的本地唤醒方案：

```text
ESP32-S3 本地 WakeNet 检测“贾维斯”
    -> 播放“我在”
    -> 采集用户语音
    -> 上传中枢进行 ASR / 智能体处理
    -> 下载或流式播放回复
```

本次交接时仍处于分析和准备阶段，尚未修改框架，尚未烧录新固件。

## 二、正确工程位置

当前桌宠固件工程：

```text
/Users/jh-05/Documents/ChatGPT/智能中枢系统/智能中枢/firmware/sparkbot_wearable
```

不要把以下目录当作当前主工程：

```text
/Users/jh-05/Documents/ChatGPT/智能中枢系统/智能中枢/firmware/esp32_recording_beam_template
```

相关硬件资料：

```text
/Users/jh-05/Documents/ChatGPT/智能中枢系统/智能中枢/firmware/sparkbot_wearable/HARDWARE_MAPPING.md
/Users/jh-05/Documents/ChatGPT/智能中枢系统/智能中枢/firmware/sparkbot_wearable/README.md
/Users/jh-05/Documents/ChatGPT/智能中枢系统/智能中枢/firmware/sparkbot_wearable/REAL_HARDWARE_TEST_2026-10-16.md
```

## 三、硬件实测结论

桌宠当前通过原生 USB CDC 连接，最近确认的串口为：

```text
/dev/cu.usbmodem13201
```

串口号可能因重新插拔变化，使用前应重新执行 `ls /dev/cu.*` 确认。

已通过 `esptool` 只读确认：

| 项目 | 实测结果 |
|---|---|
| 主控 | ESP32-S3，revision 0 |
| Flash | 32 MB |
| PSRAM | 8 MB，Octal |
| Wi-Fi / BLE | 支持 |
| ES8311 | 控制通信和初始化正常 |
| 音频输入 | I2S，16 kHz，16 bit |
| 麦克风 | 能采集到有效变化信号 |

已看到的串口日志包括：

```text
Chip is ESP32-S3 (QFN56)
Features: WiFi, BLE, Embedded PSRAM 8MB
[I2S] started ... rate=16000Hz mode=RX
[ES8311] configuring ADC, 16kHz/16bit, I2S slave
[AUDIO] channel=left/right ...
```

结论：硬件、PSRAM、ES8311 和 16 kHz 麦克风输入满足继续评估 WakeNet/AFE 的基本条件。

## 四、当前固件状态

`platformio.ini` 当前只有 Arduino 框架：

```ini
framework = arduino
```

当前环境包括：

```text
sparkbot_actual
audio_probe
audio_capture
```

当前工程已经有：

- ESP32-S3 芯片、Flash、PSRAM 和 Wi-Fi 自检；
- ES8311 I2C 探测与寄存器读取；
- ES8311 ADC 初始化；
- I2S 16 kHz/16 bit 麦克风采集；
- WAV 导出；
- 通过 Wi-Fi 上传音频到中枢并调用千问 ASR；
- 将 ASR 文字发送到 RK3588 自动路由。

当前工程没有：

- `esp-sr` 依赖；
- `WakeNet` 本地唤醒词检测；
- `AFE` 音频前处理；
- 持续运行的音频采集任务；
- WebSocket 流式音频会话。

源码重点：

```text
firmware/sparkbot_wearable/src/audio_capture.cpp
firmware/sparkbot_wearable/src/main.cpp
firmware/sparkbot_wearable/include/board_config.h
firmware/sparkbot_wearable/platformio.ini
```

当前 `audio_capture.cpp` 主要是按命令启动 I2S、采集短音频、停止 I2S，不是持续运行的音频服务。

## 五、为什么参考 StackChan

StackChan 资料目录：

```text
/Users/jh-05/Downloads/StackChan-项目资料-20261020/03-官方源码摘录/source-notes/firmware
```

关键证据：

```text
main/idf_component.yml
main/Kconfig.projbuild
main/hal/hal_ws_avatar.cpp
main/apps/app_ai_agent/app_ai_agent.cpp
```

StackChan 使用：

- ESP-IDF；
- `espressif/esp-sr`，资料中为约 `2.3.0`；
- AFE + WakeNet；
- 本地检测唤醒词；
- WebSocket 长连接；
- Opus 音频帧流式发送和接收；
- 持续音频任务和队列。

它流畅的主要原因是唤醒词不需要经过“录音 -> 上传 -> 服务端 ASR -> 返回”，而是在 ESP32-S3 本地完成。

## 六、建议技术路线

已确定优先采用：

```text
ESP-IDF + Arduino 混合框架
```

理由：

- 尽量保留现有 Arduino、ES8311、I2S 和中枢通信代码；
- 使用 ESP-IDF 组件方式接入官方 `esp-sr`；
- 后续可以继续使用 AFE、WakeNet、降噪和回声消除；
- 避免一次性重写全部桌宠固件。

但注意：该方案目前只是技术决策，尚未验证当前 PlatformIO 版本、ESP-IDF 版本和 `esp-sr` 版本组合是否能直接编译。接手 Agent 必须先做编译验证，再决定是否继续沿用 PlatformIO，或建立一个独立的 ESP-IDF 工程。

## 七、接手后的推荐顺序

### 1. 保护现有工作区

先查看状态：

```bash
cd "/Users/jh-05/Documents/ChatGPT/智能中枢系统/智能中枢"
git status --short --branch
```

当前仓库存在用户已有的未提交修改，不要使用 `git reset --hard`、`git checkout --` 或批量覆盖。先确认改动归属，再只修改 `firmware/sparkbot_wearable` 相关文件。

### 2. 读取当前工具版本

```bash
pio --version
pio platform show espressif32
python3 --version
```

### 3. 先建立可回退的本地副本

在修改前，复制当前 `sparkbot_wearable` 工程作为本地备份。备份不要提交到仓库，也不要覆盖现有源码。

### 4. 做最小编译实验

先确认当前工程基线可以编译：

```bash
cd "/Users/jh-05/Documents/ChatGPT/智能中枢系统/智能中枢/firmware/sparkbot_wearable"
pio run -e sparkbot_actual
pio run -e audio_capture
```

这一步只编译，不烧录。

### 5. 确认 `esp-sr` 接入方式

优先参考 StackChan 的 `idf_component.yml` 和 `sdkconfig.defaults`，核对本机 ESP-IDF 和 `esp-sr` 版本兼容性。不要直接把 StackChan 的整套工程覆盖到 SparkBot 上。

### 6. 第一阶段只做本地唤醒最小闭环

目标是：

```text
持续 I2S 采集
    -> WakeNet / AFE
    -> 命中后串口打印 wake event
    -> 播放现有“我在” PCM
```

先使用官方已有唤醒词或模型验证模型运行、内存和音频格式；成功后再评估“贾维斯”自定义唤醒词模型。不要一开始同时改本地唤醒、WebSocket、ASR 和 TTS。

### 7. 编译通过后再征求烧录测试

用户此前明确要求：排查阶段先不要执行烧录。除非用户再次确认，不能自动执行上传或擦除 Flash。

## 八、禁止误判的事项

- `esptool` 的 `ESP32-S3 has no Chip ID. Reading MAC instead.` 不是故障，芯片已经通过 MAC 识别成功；
- `Short waveform statistics only; no audio file is saved.` 不是错误，只表示当前测试不保存 WAV；
- 历史文档中的 `/dev/cu.usbmodem13101` 可能已过期，当前最近串口是 `/dev/cu.usbmodem13201`；
- `framework = arduino` 现状不代表已经支持 `esp-sr`；
- 有 ES8311 和 16 kHz 采样，不等于已经有 WakeNet；
- 不要把录音豆、RK3588 和桌宠固件混成一个设备；桌宠是随身语音入口，RK3588 负责中枢路由，录音豆是智能外设；
- 不要把 AgentOS API 密钥写进 ESP32 固件，当前桌宠应通过中枢服务通信。

## 九、项目现有服务端闭环

已经记录的真实链路是：

```text
桌宠采集 WAV
  -> 中枢 HTTP 接口
  -> 千问 ASR
  -> RK3588 /api/route
  -> 返回聊天或硬件计划
```

已记录的测试地址仅供核对，不要假设当前网络地址永久不变：

```text
千问 ASR：曾验证 192.168.0.153:18005
中枢 API：曾使用 192.168.0.107:18787
RK3588：曾验证 192.168.0.140:8095/api/route
```

其中 `192.168.0.107:18005` 曾无法连接，不能与 `192.168.0.153:18005` 混用。开始新测试前应重新探测服务健康状态。

## 十、交接结论

当前最重要的事实是：

```text
硬件条件已确认满足
音频采集链路已确认正常
服务端 ASR / RK3588 链路已有真实验证
本地 WakeNet/AFE 尚未实现
混合框架尚未修改
当前没有新的固件烧录
```

下一个 Agent 应从“基线编译 -> 选择 `esp-sr` 接入方式 -> 最小本地唤醒编译实验”继续，不要从硬件排查重新开始，也不要直接烧录。
