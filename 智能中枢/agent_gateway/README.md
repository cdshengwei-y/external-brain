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

已确认的 RK3588 测试中枢服务地址为 `http://192.168.0.140:8095`，提供：

```text
GET  /api/health
GET  /api/mode
POST /api/agent
POST /api/asr
POST /api/tts
```

项目中的 `RK3588AgentClient` 只负责调用这些 AI 能力并提取显式结构化动作；硬件命令仍然必须交给 `HubService` 做能力、权限、队列和 MQTT 校验。

硬件接入应优先使用 RK3588 硬件网关接口，而不是直接解析通用 `/api/agent` 的自然语言回复：

```text
GET  /api/hub/v1/health
GET  /api/hub/v1/devices
POST /api/hub/v1/devices
POST /api/hub/v1/commands
POST /api/hub/v1/commands/{request_id}/confirm
```

硬件网关接口支持 `dispatch=false` 的先规划、`dispatch=true` 的执行和高风险动作确认。`RK3588AgentClient` 已经封装这组接口，并支持可选的 `X-Hub-Key`。

当前真实录音豆仍然使用 MQTT 协议。RK3588 网关下发的 HTTP `/command` 通过中枢进程中的 HTTP-MQTT 兼容桥转换为现有 `HubService` 命令，再由 MQTT 发送给录音豆，不修改录音豆固件协议。启动方式见项目根目录 `README.md` 的兼容桥章节。
