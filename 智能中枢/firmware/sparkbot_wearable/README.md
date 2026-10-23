# ESP-SparkBot 烧录与硬件自检工程

这是随身设备第一阶段的真实硬件自检固件，硬件基准为嘉立创工程 `随身设备 / ESP_SparkBot_MainBoard_V1.2`。

原理图中 U1 的器件值标注为 `ESP32-S3-WROOM-1-N8`，但已连接的实物通过 `esptool` 实测为 32MB Flash、约 8MB PSRAM。因此烧录配置以实测芯片容量为准：

- Flash：实测 32 MB；
- PSRAM：8 MB Octal；
- USB 串口：启用 ESP32-S3 原生 USB CDC；
- 固件当前只读取芯片信息、Flash/PSRAM 信息并扫描 Wi-Fi；
- 不初始化 ES8311、麦克风、功放，不驱动未知 GPIO，不执行录音和播放。

## 当前目录

```text
firmware/sparkbot_wearable/
├── boards/esp32-s3-sparkbot.json       # 按实物容量定义的板卡配置
├── include/board_config.h              # 安全自检参数
├── platformio.ini                       # PlatformIO 工程配置
└── src/main.cpp                         # 第一版自检固件
```

## 烧录前准备

1. 将三块板按原工程连接好；
2. 使用能传数据的 USB-C 口连接电脑，优先使用原理图中 `D+`、`D-` 连接到 ESP32-S3 的端口；
3. 第一次只接 USB，不接电池；
4. 在 macOS 终端执行：

```bash
cd "/Users/jh-05/Documents/ChatGPT/智能中枢系统/智能中枢/firmware/sparkbot_wearable"
pio device list
```

如果没有出现新的 `/dev/cu.*` 串口，先不要烧录，检查 USB-C 线和使用的接口。当前电脑在未接入设备时只有蓝牙/调试串口，不能据此判断硬件在线。

## 编译与烧录

```bash
cd "/Users/jh-05/Documents/ChatGPT/智能中枢系统/智能中枢/firmware/sparkbot_wearable"
pio run
pio run --target upload --upload-port /dev/cu.你的设备串口
pio device monitor --port /dev/cu.你的设备串口 --baud 115200
```

也可以先让 PlatformIO 自动选择端口：

```bash
pio run --target upload
pio device monitor
```

## 预期串口输出

烧录成功后，串口应看到：

```text
SparkBot wearable - hardware self-test
SAFE MODE: no audio, motor, relay or GPIO drive
model: ESP32-S3
flash_bytes: 33554432
psram_found: yes
psram_bytes: 8388608
```

实际输出以芯片和板卡状态为准；本次实测结果为 Flash 32MB、PSRAM 约 8MB。硬件引脚和音频连接以 `HARDWARE_MAPPING.md` 以及嘉立创原理图为准。

## 串口命令

- `i`：重新打印芯片和存储器信息；
- `s`：立即扫描 Wi-Fi；
- `h`：显示帮助。

## 当前不做的事

- 不修改原有录音豆固件；
- 不接入 MQTT、RK3588 或 AgentOS；
- 不执行录音开始/停止命令；
- 不根据猜测填写麦克风、音频编解码器和功放 GPIO。

完成本阶段并确认 USB、Flash、PSRAM、Wi-Fi 扫描正常后，先使用 `audio_probe` 环境验证 ES8311 控制总线，再增加音频采集测试。

## ES8311 控制总线探测

探测固件使用嘉立创原理图中的 `GPIO4`（CDATA/SDA）和 `GPIO5`（CCLK/SCL），只进行 I²C 扫描和只读寄存器探测，不写入 ES8311 配置，不启动 I2S、麦克风或功放：

```bash
pio run -e audio_probe
pio run -e audio_probe --target upload --upload-port /dev/cu.usbmodem13101
pio device monitor --port /dev/cu.usbmodem13101 --baud 115200
```

串口中输入：

- `s`：扫描 I²C 总线；
- `r`：尝试读取候选音频编解码器地址的寄存器 `0x00`；
- `h`：显示帮助。

## ES8311 麦克风采样测试

确认 `audio_probe` 检测到 `0x18` 后，可以使用 `audio_capture` 环境。它会启动 ESP32 的 I²S 主机时钟，初始化 ES8311 的 ADC，并读取一小段 16kHz/16bit 音频，只输出最小值、最大值、峰值和 RMS，不保存音频文件，也不打开 NS4150B 功放：

```bash
pio run -e audio_capture
pio run -e audio_capture --target upload --upload-port /dev/cu.usbmodem13101
pio device monitor --port /dev/cu.usbmodem13101 --baud 115200
```

在串口输入 `a` 开始一次采样。采样结果会分别输出左右声道的均值、峰值和去直流 RMS，只能证明 I²S 数据链路有无变化，不能替代 WAV 文件或 ASR 测试。

输入 `w` 可以把真实麦克风采集的 1 秒、16 kHz、16 bit 单声道 WAV 通过 USB 导出。功放仍保持关闭。电脑端可执行：

```bash
python3 -m pip install pyserial
python3 tools/capture_wav.py --port /dev/cu.usbmodem13101 --output /tmp/sparkbot_capture.wav
```

该文件可以直接交给后续 ASR 服务测试；当前 `w` 命令仍不连接 Wi-Fi，也不向 RK3588 发送数据。

## Wi-Fi 上传到 RK3588

固件还提供 `u` 命令：采集 1 秒真实麦克风音频，封装为 WAV，再通过 Wi-Fi 上传到固定式中枢的
`/api/v1/portable/v1/media`，然后提交 `/api/v1/portable/v1/requests`。中枢负责调用千问 ASR，
再把转写文字交给 RK3588 自动路由；
桌宠不保存 API 密钥，也不直接操作 MQTT。

首次使用时复制配置模板并填写真实网络参数：

```bash
cp include/wifi_config.example.h include/wifi_config.h
```

至少确认：

- 桌宠和 RK3588 在同一个局域网；
- `SPARKBOT_HUB_BASE_URL` 使用运行上位机电脑的局域网地址，不能填 `127.0.0.1`；
- 上位机已启动中枢 API，且电脑防火墙允许局域网访问 `18787`；
- 不把 `wifi_config.h` 提交到版本库。

重新编译、烧录后，在串口输入 `u`。串口会打印 Wi-Fi 地址、上传状态、请求状态、ASR 转写和 RK3588 返回结果。
当前上传仍然只采集音频，不启用 NS4150B 功放。

## 第一版语音会话

`v` 命令会进入语音会话循环：待机时采集短语音片段并交给中枢检测“贾维斯”，命中后播放“我在”；随后自动采集每一轮语音，交给千问 ASR 和 RK3588 自动路由。聊天回复或真实硬件执行结果由中枢生成 16 kHz/16 bit/单声道 WAV，桌宠下载后播放。说“退下吧”会播放待命回复并回到待机。

```text
pio device monitor --port /dev/cu.usbmodem13101 --baud 115200
v
```

当前第一版通过服务端 ASR 检测唤醒词，优先验证完整真实链路；它不是本地 WakeNet，待链路稳定后再替换成本地唤醒模型以降低待机网络请求和唤醒延迟。

新增随身设备请求字段与接口：

- `session_mode= wake`：只检测唤醒词，命中返回 `session_action=awake`；
- `session_mode= conversation`：执行聊天或硬件命令，返回 `session_action=continue` 或 `sleep`；
- `GET /api/v1/portable/v1/requests/{request_id}/audio`：下载中枢生成的 WAV 回复音频；
- `source=wearable` 的硬件计划自动执行，仍必须经过 RK3588 结构化计划和真实设备 ACK。
