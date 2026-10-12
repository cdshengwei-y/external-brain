# RK3588 AgentOS 命令接口接入信息清单

## 1. 文档目的

当前上位机已经可以通过 RK3588 的旧版自动路由接口进行聊天和硬件计划处理。为了接入 RK3588 最新版本提供的 AgentOS 命令接口，需要先确认接口鉴权方式和请求协议。

请 RK3588 端开发同事按照本文档补充接口信息，补充完成后再进行上位机迁移和真实硬件联调。

## 2. 当前已知状态

当前 RK3588 地址：

```text
http://192.168.0.140:8095
```

当前版本健康检查正常，已发现真实设备：

```text
设备 ID：bean_ye_1
设备名称：录音豆
支持能力：record.start、record.stop、status.read
```

已发现新版 AgentOS 命令接口：

```http
GET  /api/agentos/v1/devices
POST /api/agentos/v1/commands
```

当前未携带鉴权信息访问时，RK3588 返回：

```text
HTTP 401 Unauthorized
AgentOS command credential is required.
```

这说明接口已经存在，但需要命令密钥才能访问。

## 3. AgentOS 命令密钥是什么

`AGENTOS_COMMAND_API_KEY` 是调用 AgentOS 硬件命令接口的访问凭证，可以理解为接口密码或服务令牌。

它的作用是：

- 识别请求是否来自授权的上位机；
- 防止局域网内其他程序直接控制录音豆；
- 保护设备目录查询和硬件命令接口；
- 后续对不同调用方进行权限区分。

它不是：

- 大模型 API；
- AgentOS 智能体本身；
- 录音豆设备密钥；
- MQTT 密码；
- 用户的聊天内容。

## 4. 请 RK3588 端提供的信息

### 4.1 命令密钥

请通过私密方式提供测试环境的命令密钥，不要把真实密钥提交到 GitHub 或写入公开文档。

```text
AGENTOS_COMMAND_API_KEY=<请填写真实测试密钥>
```

### 4.2 请求头名称和格式

请明确说明密钥应该放在哪个 HTTP 请求头中。

请从以下形式中确认实际采用哪一种，不能由上位机自行猜测：

```http
Authorization: Bearer <AGENTOS_COMMAND_API_KEY>
```

或者：

```http
X-AgentOS-Command-Key: <AGENTOS_COMMAND_API_KEY>
```

或者其他固定格式：

```http
<实际请求头名称>: <实际值格式>
```

### 4.3 设备目录接口

请确认：

```http
GET /api/agentos/v1/devices
```

请求是否只需要命令密钥，返回格式是否类似：

```json
{
  "devices": [
    {
      "device_id": "bean_ye_1",
      "name": "录音豆",
      "capabilities": [
        "record.start",
        "record.stop",
        "status.read"
      ]
    }
  ]
}
```

同时请确认 `devices` 代表：

- 已注册设备目录；还是
- 当前真实在线设备列表；

如果只是注册目录，请提供单独的在线状态字段，不能把已注册直接当成在线。

### 4.4 命令接口

请确认：

```http
POST /api/agentos/v1/commands
Content-Type: application/json
```

建议的标准请求字段如下，请 RK3588 端确认是否完全一致：

```json
{
  "protocol_version": "1.0",
  "request_id": "desktop-request-001",
  "conversation_id": "desktop-conversation-001",
  "device_id": "bean_ye_1",
  "capability": "status.read",
  "params": {},
  "policy": "queue"
}
```

请明确以下字段是否必填：

- `protocol_version`
- `request_id`
- `conversation_id`
- `device_id`
- `capability`
- `params`
- `policy`

### 4.5 返回和等待方式

请确认命令接口属于哪一种模式：

#### 模式 A：同步返回最终结果

```json
{
  "request_id": "desktop-request-001",
  "state": "succeeded",
  "message": "设备状态读取完成",
  "result": {}
}
```

#### 模式 B：异步返回命令 ID

```json
{
  "request_id": "desktop-request-001",
  "command_id": "command-001",
  "state": "accepted"
}
```

如果是异步模式，请同时提供查询或等待接口，例如：

```http
GET /api/agentos/v1/commands/{command_id}
GET /api/agentos/v1/commands/{command_id}/wait
```

并说明以下状态的含义：

```text
accepted、queued、running、succeeded、failed、timeout、cancelled
```

### 4.6 确认机制

请明确 `record.start` 和 `record.stop` 是否：

- 由 AgentOS 接口直接执行；
- 先返回计划，再由上位机确认；
- 需要额外的确认接口；
- 需要在请求中传入 `confirmed: true` 或其他字段。

在确认方式没有明确之前，上位机不会自动发送开始录音和停止录音命令。

## 5. 安全测试顺序

拿到密钥和协议说明后，按以下顺序测试：

### 第一步：查询设备目录

只调用：

```http
GET /api/agentos/v1/devices
```

确认能看到 `bean_ye_1`，不执行任何硬件动作。

### 第二步：查询录音豆状态

使用：

```json
{
  "device_id": "bean_ye_1",
  "capability": "status.read",
  "params": {}
}
```

`status.read` 属于无副作用测试，不会开始或停止录音。

### 第三步：验证计划或确认流程

先确认接口如何处理：

```json
{
  "device_id": "bean_ye_1",
  "capability": "record.start",
  "params": {}
}
```

只有确认接口返回的状态和确认机制明确后，才进行真实开始录音测试。

### 第四步：真实开始和停止录音

必须经过人工确认后执行：

```text
record.start → 等待设备确认 → record.stop → 等待设备确认
```

每一步都要记录：

- `request_id`；
- `command_id` 或 `plan_id`；
- 返回状态；
- 设备最终状态；
- 是否产生录音文件。

## 6. 与当前上位机的关系

当前上位机仍使用旧链路：

```text
上位机 → RK3588 /api/route → 旧版计划与确认流程 → HubService → MQTT → 录音豆
```

接入新版 AgentOS 命令接口后，目标链路是：

```text
上位机
  → RK3588 /api/agentos/v1/commands
  → AgentOS 命令鉴权与设备能力校验
  → RK3588 硬件执行层
  → HubService / MQTT
  → 录音豆
```

在命令密钥、请求头、请求字段、返回状态和确认机制全部明确之前，不迁移现有上位机命令逻辑，避免误控制真实设备。

## 7. 请同事回复的最小信息

请直接补充以下内容：

```text
1. 测试环境 AgentOS 命令密钥：请私下发送，不要提交 GitHub
2. 鉴权请求头：例如 Authorization 或 X-AgentOS-Command-Key
3. POST /api/agentos/v1/commands 的完整请求示例
4. 成功返回示例
5. 失败返回示例
6. 命令状态查询或等待接口
7. record.start / record.stop 的确认方式
8. status.read 是否可以直接执行
```

收到以上信息后，上位机端再完成新版接口适配和真实硬件验证。
