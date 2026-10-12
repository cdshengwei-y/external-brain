# AgentOS—上位机—智能外设统一链路

**版本：** V1.0  
**日期：** 2026-10-12  
**状态：** 架构决策已确认  
**适用范围：** 随身设备、上位机服务、AgentOS 智能体、固定式中枢 RK3588、智能外设

## 1. 架构决策

本项目采用“随身设备负责采集和播放、上位机负责接入、AgentOS 负责理解和调度、RK3588 负责硬件执行网关、智能外设负责确定性执行”的链路。

```text
随身设备
  │ 音频 / 文字
  ▼
上位机服务
  │ ASR 转文字、会话管理、请求关联
  ▼
AgentOS 中的外接大脑总管智能体
  │ 判断聊天、调用其他智能体或生成硬件计划
  ├──────────────► 聊天回复
  │                    │
  │                    ▼
  │              TTS → 上位机 → 随身设备播放
  │
  └──────────────► 结构化硬件命令
                       │
                       ▼
                  RK3588 硬件网关
                       │
                       ▼
                  HubService / Bridge
                       │ MQTT / HTTP / 其他协议
                       ▼
                    智能外设
                       │
                       ▼
                 状态和执行结果
                       │
                       └──── 返回 AgentOS → 上位机 → 随身设备
```

## 2. 各层职责

### 2.1 随身设备

随身设备是用户的语音入口和反馈终端，首版只负责：

- 采集用户语音；
- 通过 Wi-Fi 或其他网络上传音频；
- 播放上位机返回的语音；
- 提供按键、取消和必要的状态提示；
- 携带 `device_id`、`conversation_id`、`request_id` 等请求上下文。

随身设备不直接连接 MQTT，不直接控制智能外设，不保存完整设备数据库，也不保存 AgentOS 管理员密钥。

### 2.2 上位机服务

上位机最终定位为一个服务，不依赖桌面界面才能运行。桌面上位机可以作为调试和管理客户端，但不是最终产品链路的必要节点。

上位机服务负责：

- 接收随身设备的音频或文字；
- 调用 ASR 服务，将音频转换为文字；
- 维护会话和请求状态；
- 将文字请求发送给 AgentOS；
- 接收 AgentOS 的文字回复、工具结果和硬件状态；
- 调用 TTS，将文字回复转换为音频；
- 把音频、处理中状态和最终结果返回给随身设备；
- 支持多条请求同时存在，并按 `request_id` 关联结果。

上位机不自行判断“聊天还是硬件命令”，不把中文直接转换成 MQTT 命令。

### 2.3 AgentOS 外接大脑总管智能体

在 AgentOS 中部署一个自有的总管智能体，建议命名为 `jarvis_hub`。它负责：

1. 理解用户文字；
2. 判断当前请求是普通聊天、澄清问题、调用其他智能体还是硬件控制；
3. 调用其他专业智能体或 AgentOS 工具；
4. 生成经过能力白名单约束的结构化硬件计划；
5. 读取硬件执行结果；
6. 组织用户可以理解的文字回复。

总管智能体只输出结构化意图和命令，不直接拼接 MQTT 主题，不直接控制 GPIO，也不把自然语言当作硬件命令执行。

### 2.4 RK3588 固定式中枢

RK3588 作为统一硬件执行网关，负责：

- 设备注册和设备能力目录；
- 计划和命令校验；
- 权限、确认和风险控制；
- 每个设备的命令队列；
- 幂等、超时、取消和替换；
- 将统一 HTTP 命令转换为 MQTT、HTTP、蓝牙或其他外设协议；
- 汇总设备状态和执行结果。

即使某个 AgentOS 智能体理论上可以直接访问某个智能外设，V1 仍统一先经过 RK3588。这样可以避免每个智能体重复实现设备认证、队列和安全逻辑。

### 2.5 智能外设和设备适配器

智能外设不理解自然语言，只负责确定性动作和状态反馈。

项目中不把物理设备本身称为“容器”。正确抽象是：

```text
物理设备 + 固件
        ↓
设备 manifest / 能力描述
        ↓
设备适配器或能力服务
        ↓
RK3588 统一硬件网关
```

例如：

- 录音豆适配器：`record.start`、`record.stop`、`status.read`；
- 音箱适配器：`speaker.play`、`speaker.stop`、`volume.set`；
- 烧水壶适配器：`kettle.boil`、`temperature.read`；
- 灯光适配器：`light.on`、`light.off`、`brightness.set`。

设备适配器可以作为 AgentOS 应用任务、MCP 工具或 RK3588 本地协议适配模块实现，但真实硬件的最终执行必须经过能力校验和状态确认。

## 3. 上位机服务接口

### 3.1 接收用户内容

```http
POST /api/v1/input
Content-Type: application/json
```

请求可以携带文字，也可以携带音频地址。首版随身设备优先上传短音频，由上位机调用 ASR。

```json
{
  "protocol_version": "1.0",
  "request_id": "request-001",
  "conversation_id": "conversation-001",
  "source": "portable",
  "device_id": "portable-001",
  "input_type": "audio",
  "audio_url": "https://example.invalid/audio/request-001.wav",
  "text": null,
  "target_device_id": "bean_ye_1"
}
```

如果上游已经完成 ASR，也可以直接发送文字：

```json
{
  "protocol_version": "1.0",
  "request_id": "request-002",
  "conversation_id": "conversation-001",
  "source": "portable",
  "device_id": "portable-001",
  "input_type": "text",
  "text": "开始录音",
  "target_device_id": "bean_ye_1"
}
```

### 3.2 事件和结果

```http
GET /api/v1/events?conversation_id=conversation-001
```

第一版可以使用 SSE，后续随身设备需要双向实时音频时再增加 WebSocket。每一条事件都必须包含 `request_id`：

```json
{
  "event": "assistant.reply",
  "request_id": "request-002",
  "conversation_id": "conversation-001",
  "status": "completed",
  "text": "已经开始录音。",
  "audio_url": "https://example.invalid/tts/request-002.mp3",
  "command_id": "command-001"
}
```

处理中事件示例：

```json
{
  "event": "request.status",
  "request_id": "request-002",
  "conversation_id": "conversation-001",
  "status": "processing",
  "stage": "agent_understanding"
}
```

### 3.3 健康检查

```http
GET /api/v1/health
```

健康检查至少应区分：

- 上位机服务是否正常；
- ASR 是否可用；
- TTS 是否可用；
- AgentOS 是否可连接；
- RK3588 是否可连接；
- MQTT 和设备网关是否可用。

## 4. AgentOS 接入约定

当前公开 AgentOS 代码提供两类外部入口：

```text
POST /client/v1/chat       用户对话
GET  /client/v1/events     对话事件
/mcp                       工具列表和工具调用
```

上位机调用 AgentOS 对话接口时，使用用户或设备 Bearer Token，并为每条消息提供 `client_msg_id`，避免网络重试造成重复消息：

```json
{
  "app": "jarvis_hub",
  "text": "开始录音",
  "client_msg_id": "request-002"
}
```

需要注意：

- AgentOS 的聊天接口要求传入已经安装并启用的 `app`；
- AgentOS 的 `vhm_...` 对话令牌和 RK3588 的 `X-Hub-Key` 不是同一个密钥；
- 当前公开主分支的自动路由应用还没有完全合入，不能假定 `tudou` 已经可用；
- 如果使用 VHA 应用承载硬件能力，每个任务可以自动成为 MCP 工具；
- VHA 硬件应用是否可以直接访问 RK3588，需要按照部署网络和权限策略确认；
- 在自有总管智能体正式部署前，上位机不切换现有 RK3588 `/api/route` 联调链路。

## 5. 统一硬件命令格式

AgentOS 或设备适配器生成的硬件命令必须是结构化对象：

```json
{
  "protocol_version": "1.0",
  "request_id": "request-002",
  "conversation_id": "conversation-001",
  "source": "agentos",
  "plan_id": "plan-001",
  "device_id": "bean_ye_1",
  "capability": "record.start",
  "params": {},
  "policy": "queue"
}
```

RK3588 校验通过后，再交给 HubService：

```text
AgentOS 总管智能体
  → 结构化 plan
  → RK3588 /api/hub/v1/commands
  → HubService
  → HTTP-MQTT Bridge
  → MQTT
  → 智能外设
```

禁止以下做法：

- AgentOS 直接拼接 MQTT 主题；
- 上位机根据关键词直接发送 `record.start`；
- 将智能体自然语言回复当成硬件命令；
- 设备只返回“收到”就向用户报告“执行完成”；
- 跳过 `plan_id`、能力白名单或设备在线状态校验。

## 6. 聊天和硬件命令的返回

### 6.1 普通聊天

```text
随身设备音频
  → ASR
  → AgentOS 总管智能体
  → 文字回复
  → TTS
  → 上位机
  → 随身设备播放
```

普通聊天不生成 `plan_id`，不调用 HubService，不发布 MQTT。

### 6.2 硬件命令

```text
随身设备音频
  → ASR
  → AgentOS 生成 plan
  → 用户确认（高风险动作）
  → RK3588 执行
  → HubService 等待真实结果
  → AgentOS 组织回复
  → TTS
  → 随身设备播放
```

只有硬件命令最终状态为 `succeeded` 时，回复才可以使用“已经完成”。

以下状态必须如实反馈：

```text
accepted   已接收
queued     已排队
running    执行中
failed     执行失败
timeout    执行超时
offline    设备离线
cancelled  已取消
superseded 已被新命令替代
```

## 7. 并发请求和设备队列

上位机、AgentOS 和硬件网关都不能因为上一条请求未完成而拒绝后一条普通消息。

```text
request A：开始录音
request B：查询状态
request C：停止录音
```

三条消息可以同时存在，但每条消息必须独立维护：

- `request_id` 独立；
- AgentOS 回复独立；
- `command_id` 独立；
- UI 或随身设备按 ID 展示状态；
- 同一设备的冲突动作由 RK3588 队列策略决定。

V1 默认策略：

- 查询类命令允许读取最新状态；
- 同一设备的动作命令按 FIFO 排队；
- 冲突动作必须明确排队、替换、取消或拒绝；
- 重试复用原 `request_id`，不能重复发布 MQTT；
- 设备离线时命令进入失败或待定状态，不能伪装成功。

## 8. 硬件直连与 RK3588 网关的取舍

### 8.1 推荐方案：统一经过 RK3588

```text
AgentOS → RK3588 → HubService → 智能外设
```

优点：

- 所有设备统一认证和权限；
- 所有命令统一排队和幂等；
- AgentOS 不需要理解 MQTT、蓝牙和各类厂商协议；
- 设备增加时不需要修改总管智能体；
- 更容易做日志、审计、离线和故障恢复。

### 8.2 可选方案：AgentOS 工具调用设备适配器

```text
AgentOS → 设备适配器 → 智能外设
```

只适合安全、低风险、协议成熟的设备。即使采用直连，适配器内部仍然必须有：

- 设备身份校验；
- 能力白名单；
- 参数校验；
- 超时和重试；
- 状态确认；
- 失败回滚或安全停止。

V1 不采用直连作为默认路径，先统一经过 RK3588。

## 9. 实施顺序

### 第一步：保留现有真实硬件链路

继续使用当前已经验证的：

```text
RK3588 → HubService → HTTP-MQTT Bridge → bean_ye_1
```

先不修改录音豆固件和 MQTT 协议。

### 第二步：确定 AgentOS 应用形态

确认 AgentOS 部署端是否允许安装自定义 `jarvis_hub` 应用，并确定：

- 应用 key；
- 对话入口；
- MCP 工具名称；
- 用户/设备 Bearer Token 获取方式；
- AgentOS 应用到 RK3588 的网络访问方式。

### 第三步：实现上位机 AgentOS 适配器

在上位机增加独立适配层，负责：

- 调用 `/client/v1/chat`；
- 监听 `/client/v1/events`；
- 处理断线和重连；
- 传递 `request_id`、`conversation_id` 和 `client_msg_id`；
- 将 AgentOS 返回的硬件计划交给 RK3588；
- 将 RK3588 真实结果回传给 AgentOS 或上位机。

### 第四步：接入随身设备

随身设备只需调用上位机服务，不需要理解 AgentOS、MQTT 或具体外设协议：

```text
随身设备 → 上位机 /api/v1/input
随身设备 ← 上位机 /api/v1/events
```

### 第五步：扩展智能外设

新增设备时优先新增：

1. 设备 manifest；
2. 能力字段；
3. RK3588 适配器；
4. 状态映射和测试；
5. AgentOS 工具说明。

不重写上位机主流程，不让总管智能体硬编码具体设备协议。

## 10. V1 完成标准

1. 随身设备可以上传一段真实语音；
2. 上位机可以通过 ASR 得到文字；
3. 上位机可以把文字发送给 AgentOS 总管智能体；
4. 普通聊天可以返回文字并通过 TTS 播放；
5. “开始录音”等命令可以生成结构化计划；
6. 计划经过 RK3588 和 HubService 后真实控制 `bean_ye_1`；
7. 硬件结果能够按 `request_id` 返回给随身设备；
8. 前一个命令未完成时，后一个命令可以继续发送并正确排队；
9. 设备离线、AgentOS 不可用、ASR 失败、TTS 失败和硬件超时都有明确反馈；
10. 上位机作为服务运行时，不依赖桌面窗口和终端人工操作。

## 11. 当前未决事项

以下事项在 AgentOS 端确认前不进入硬件返工：

- 自定义 `jarvis_hub` 应用的安装和启用方式；
- AgentOS 总管智能体的对话入口是内置 App、VHA 应用还是外部适配器；
- AgentOS 应用访问 RK3588 的网络路径；
- AgentOS 是否返回可直接消费的结构化硬件计划；
- ASR 和 TTS 服务部署在上位机、RK3588 还是独立服务节点；
- 随身设备首版采用短音频上传还是实时音频流。

