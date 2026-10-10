# 第一阶段文字智能体

第一阶段的文字智能体由真实设备上位机调用。用户在上位机窗口输入中文，`TextAgent` 将当前支持的意图转换为中枢命令，再等待真实设备返回最终状态。

当前支持：

- 开始录音；
- 停止录音；
- 查询状态；
- 查看设备。

上位机入口位于项目根目录的 `外接大脑上位机.app`。后续接入大模型时，只替换 `TextAgent` 的意图解析部分，继续复用中枢 API、命令 ID、等待结果机制和真实 MQTT 硬件协议。

随身设备接口已经提供第一版文字和音频入口：

```text
POST /api/v1/portable/v1/media
POST /api/v1/portable/v1/requests
GET  /api/v1/portable/v1/requests/{request_id}
```

当前音频只完成上传和持久化，尚未接入中枢音频理解适配器；未配置音频理解时会明确返回 `AUDIO_UNDERSTANDING_NOT_CONFIGURED`，不会执行硬件命令。
