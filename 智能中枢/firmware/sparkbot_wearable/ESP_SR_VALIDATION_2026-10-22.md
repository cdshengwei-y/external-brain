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
