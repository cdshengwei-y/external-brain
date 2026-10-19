# ESP-SparkBot 实机音频链路测试

## 测试目标

验证真实桌宠的麦克风采集、USB 音频导出、千问 ASR 和 RK3588 自动路由链路。

```text
ES8311 麦克风
  -> ESP32-S3 I2S
  -> USB 导出 WAV
  -> 千问 Qwen3-ASR-1.7B
  -> RK3588 /api/route
```

## 硬件和固件

- 实物：ESP-SparkBot 桌宠板
- 串口：`/dev/cu.usbmodem13101`
- 芯片：ESP32-S3，实测 32 MB Flash、8 MB PSRAM
- 音频：16 kHz、16 bit、单声道 WAV
- ES8311 I2C 地址：`0x18`
- I2S：MCLK `GPIO45`、BCLK `GPIO39`、LRCK `GPIO41`、DIN `GPIO40`
- 功放：NS4150B 保持关闭，未执行播放测试

## 实测结果

### 1. I2S 采样

两次采样均成功，左右声道数据一致，符合单麦克风信号复制到双声道的表现：

```text
left/right min=-125 max=4728 rms_centered=1245.39
left/right min=-138 max=4632 rms_centered=1220.56
```

### 2. WAV 导出

通过固件 `w` 命令导出真实音频，文件检查通过：

```text
RIFF/WAVE
16 bit mono 16000 Hz
1.0 seconds
32044 bytes
min=-232 max=4723 rms_centered=407.27
```

对应文件：`/tmp/sparkbot_capture.wav`

### 3. 千问 ASR

实际调用 `http://192.168.0.153:18005`：

```json
{
  "status": "ok",
  "model": "Qwen3-ASR-1.7B",
  "text": "嗯。",
  "language": "Chinese"
}
```

`http://192.168.0.107:18005` 当前无法连接；项目暂以实际可用的 `192.168.0.153` 为 ASR 地址，不能把两个地址混用。

### 4. RK3588 自动路由

把 ASR 转写结果发送到 `http://192.168.0.140:8095/api/route`：

```json
{
  "route": "chat",
  "route_detail": "qwen3_chat",
  "reply": "您好，有什么我可以帮您的吗？",
  "status": "completed",
  "actions": []
}
```

此次没有发送硬件控制语句，因此没有下发录音命令，符合安全测试要求。

### 5. 中枢音频接口闭环

使用同一段真实桌宠 WAV，通过本地中枢的随身设备接口完成了完整服务端链路：

```text
POST /api/v1/portable/v1/media       -> media_id
POST /api/v1/portable/v1/requests    -> request_id
GET  /api/v1/portable/v1/requests/{request_id}
```

最终结果：

```text
transcript: 嗯。
state: completed
response_text: 你好！今天有什么想聊的吗？
```

这证明“上传音频 -> 中枢调用千问 ASR -> RK3588 自动路由 -> 返回聊天回复”的服务端链路正常。

## 结论

本阶段已确认：

1. 桌宠真实麦克风、ES8311、ESP32-S3 I2S 采集链路正常。
2. 桌宠可以导出可被 ASR 使用的标准 WAV 文件。
3. 千问 ASR 可以识别桌宠导出的 WAV。
4. ASR 文字可以进入 RK3588 自动路由，并正确区分为聊天。

尚未完成：

1. 桌宠填写真实 Wi-Fi 配置后执行 `u`，完成无线上传实测；固件和中枢接口已经接好。
2. RK3588 根据语音内容生成真实硬件动作计划，目前只验证了聊天路由。
3. 录音豆在线时的真实 `record.start` / `record.stop` 联调。
4. RK3588 或随身设备端的语音播放闭环。

## 无线上传实现状态

- 固件先 `POST /api/v1/portable/v1/media` 上传原始 WAV；
- 再 `POST /api/v1/portable/v1/requests` 提交音频请求；
- 固定式中枢调用千问 ASR，再把转写文字交给 RK3588 `/api/route`；
- 桌宠轮询请求状态，聊天会打印回复，硬件计划只打印待确认，不自动执行；
- 上位机下次启动时将中枢 API 绑定到 `0.0.0.0:18787`，当前电脑局域网地址为 `192.168.0.107`；
- 临时以 `0.0.0.0:18789` 验证了局域网健康检查，HTTP 返回 `200`，验证后已关闭。

## 真实无线实测结果

使用真实 Wi-Fi 配置烧录后，桌宠执行串口 `u` 命令，结果如下：

```text
Wi-Fi SSID: 2605-TP
桌宠 IP: 192.168.0.216
中枢地址: http://192.168.0.107:18787
音频上传: HTTP 201，获得 media_id
音频请求: HTTP 202，获得 request_id
ASR transcript: 嗯。
request state: completed
RK3588 response: 您好！有什么我可以帮您的吗？
```

结论：真实桌宠已经可以通过 Wi-Fi 把语音音频送到固定式中枢，中枢完成千问 ASR 和 RK3588 自动路由，并返回聊天结果。本次音频内容不是硬件命令，因此没有执行录音豆动作。
