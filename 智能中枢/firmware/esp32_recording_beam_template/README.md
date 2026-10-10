# ESP32 录音豆 MQTT 接入模板

这是一个协议接入模板，不代表真实录音豆的最终固件。当前工作区没有找到录音豆的真实固件源码、芯片型号或原理图，因此所有硬件相关参数都必须根据实际设备补齐。

## 使用方式

1. 复制 `src/config.example.h` 为 `src/config.h`；
2. 填写 Wi-Fi、运行中枢电脑的局域网 IP 和设备 ID；
3. 根据真实硬件修改 LED 和按钮引脚；
4. 使用 PlatformIO 编译和烧录：

```bash
pio run
pio run --target upload
pio device monitor
```

## 已实现的协议行为

- 连接 Wi-Fi 和 MQTT；
- 发布 `manifest`、`availability` 和 `state`；
- 订阅 `commands`；
- 执行 `indicator.set`；
- 发布 `running` 和 `succeeded` 命令确认；
- 发布 `button.press` 事件；
- MQTT 断线后定时重连；
- 通过 MQTT Last Will 发布离线状态。

## 需要根据真实硬件补齐

- 芯片和 PlatformIO `board`；
- LED 的实际驱动逻辑；
- 按钮输入方式；
- 真实固件版本；
- Wi-Fi 配网方式；
- 是否需要 MQTT 用户名、密码和 TLS；
- 录音功能与音频传输协议。
