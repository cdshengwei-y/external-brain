# RK3588 网关与中枢适配

## 架构边界

外部硬件是入口和执行对象，RK3588 是当前服务器与硬件路由节点，链路是双向的：

```text
外部硬件 / 上位机 / 桌宠
        ⇅ 请求、语音、状态、事件
RK3588 Server
        ⇅ 结构化计划、确认、执行结果
AgentOS（后续由同事接入）
        ⇅
设备执行适配层 / HTTP-MQTT 兼容桥
        ⇅
录音豆、手机及其他硬件
```

当前本仓只负责验证 AgentOS 接入前的基线：

```text
上位机 → RK3588 → HTTP-MQTT 兼容桥 → MQTT → 录音豆
录音豆 → MQTT ACK / 状态 → 兼容桥 → RK3588 / 上位机
```

后续 AgentOS 插入 RK3588 和设备执行适配层之间；本仓不在本轮代替同事实现 AgentOS 接入。

上位机不再把中文直接转换成硬件命令。所有用户消息先由 `RK3588AgentClient.route_message()` 发送到 RK3588 的 `task=auto` 自动路由；只有路由结果为硬件命令且返回结构化 `plan_id` 时，才进入用户确认和执行流程。

当前支持：

- 开始录音；
- 停止录音；
- 查询状态；
- 查看设备。

2026-10-16 复验结果：RK3588 健康检查、设备目录、自动路由和“开始录音”结构化计划均正常；真实执行尚未通过，因为录音豆当前 MQTT `availability` 为 `offline`，兼容桥按设计拒绝下发命令。设备重新上线后，先验证 `status.read`，再验证 `record.start` 和 `record.stop` 的真实 ACK。

桌面端回复语音当前使用 macOS 系统 TTS（`say` + `afplay`）按需生成和播放，便于先验证界面交互。RK3588 的 TTS 适配器已保留，后续可在服务速度稳定后切换。

上位机入口位于项目根目录的 `外接大脑上位机.app`。当前 RK3588 的 `dispatch=false` 规划接口仍需同事完成稳定性联调；接口未返回合法 `plan_id` 时，上位机会失败提示，不会绕过规划直接控制硬件。

随身设备接口已经提供第一版文字和音频入口：

```text
POST /api/v1/portable/v1/media
POST /api/v1/portable/v1/requests
GET  /api/v1/portable/v1/requests/{request_id}
POST /api/v1/portable/v1/requests/{request_id}/confirm
```

音频请求会从 SQLite 读取已上传的音频，转换为千问 ASR 服务所需的 `multipart/form-data`，调用 `http://192.168.0.153:18005/transcribe` 的 `file` 字段获取文字，再把文字送入 RK3588 的 `POST /api/route` 自动路由。转写结果会保存到请求的 `transcript` 字段；未配置音频理解时会明确返回 `AUDIO_UNDERSTANDING_NOT_CONFIGURED`，不会执行硬件命令。

已确认的千问 ASR 服务地址为 `http://192.168.0.153:18005`，提供：

```text
GET  /health
POST /transcribe     multipart 字段：file、language
```

已确认的 RK3588 测试中枢服务地址为 `http://192.168.0.140:8095`，提供：

```text
GET  /api/health
GET  /api/mode
POST /api/route
POST /api/asr
POST /api/tts
```

项目中的 `RK3588AgentClient` 统一使用 `protocol_version`、`request_id`、`conversation_id`、`target_device_id`、`plan_id` 和 `source`；硬件命令仍然必须交给 `HubService` 做能力、权限、队列和 MQTT 校验。

桌面聊天入口必须使用带 V1 上下文的 `/api/route` 自动路由，不应在桌面端自行判断聊天和命令。RK3588 自动路由返回聊天时直接展示，返回硬件计划时再交给硬件网关接口执行。旧版 `/api/agent` 仅作为历史通用智能体接口，不作为当前硬件联调入口：

```text
GET  /api/hub/v1/health
GET  /api/hub/v1/devices
POST /api/hub/v1/devices
POST /api/hub/v1/commands
POST /api/hub/v1/commands/{request_id}/confirm
```

硬件网关接口支持 `dispatch=false` 的先规划、`dispatch=true` 的执行和高风险动作确认。`RK3588AgentClient` 已经封装这组接口，并支持可选的 `X-Hub-Key`。

当前真实录音豆仍然使用 MQTT 协议。RK3588 网关下发的 HTTP `/command` 通过中枢进程中的 HTTP-MQTT 兼容桥转换为现有 `HubService` 命令，再由 MQTT 发送给录音豆，不修改录音豆固件协议。启动方式见项目根目录 `README.md` 的兼容桥章节。
