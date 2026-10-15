# ESP-SparkBot 烧录与硬件自检工程

这是随身设备第一阶段的真实硬件自检固件，目标芯片按工程文件确认​​为 `ESP32-S3-WROOM-1-N16R8`：

- Flash：16 MB；
- PSRAM：8 MB Octal；
- USB 串口：启用 ESP32-S3 原生 USB CDC；
- 固件当前只读取芯片信息、Flash/PSRAM 信息并扫描 Wi-Fi；
- 不初始化 ES8311、麦克风、功放，不驱动未知 GPIO，不执行录音和播放。

## 当前目录

```text
firmware/sparkbot_wearable/
├── boards/esp32-s3-wroom-1-n16r8.json  # 自定义 N16R8 板卡定义
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
flash_bytes: 16777216
psram_found: yes
psram_bytes: 8388608
```

实际输出以芯片和板卡状态为准；如果 Flash/PSRAM 与上面不一致，先停止后续音频测试，检查板卡型号和 PlatformIO 板卡配置。

## 串口命令

- `i`：重新打印芯片和存储器信息；
- `s`：立即扫描 Wi-Fi；
- `h`：显示帮助。

## 当前不做的事

- 不修改原有录音豆固件；
- 不接入 MQTT、RK3588 或 AgentOS；
- 不执行录音开始/停止命令；
- 不根据猜测填写麦克风、音频编解码器和功放 GPIO。

完成本阶段并确认 USB、Flash、PSRAM、Wi-Fi 扫描正常后，下一步再按原理图和原厂固件确认 ES8311 的 I2S/I2C 引脚，再增加音频采集测试。
