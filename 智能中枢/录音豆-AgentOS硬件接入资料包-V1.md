# 录音豆 `bean_ye_1` AgentOS 硬件接入资料包 V1

> 用途：给 AgentOS 硬件智能体、RK3588 网关和 HTTP-MQTT Bridge 开发人员使用。
>
> 资料依据：`zc125/RK3588-2` 的 `master` 分支，重点参考提交 `eb4db3a9de8e7c641b8a900a666591916264091`（`Add WiFi MQTT hardware integration kit`）及其中的设备包、统一协议、Bridge 契约和录音豆接入清单。
>
> 安全边界：本文和同目录文件不包含真实 API Key、MQTT 密码、Token、私钥或证书。部署地址可以按环境替换，不能把密钥写入设备包、固件、Git 或日志。

## 1. 接入结论

录音豆不是 AgentOS 的直接 HTTP 设备，也不直接连接 RK3588。标准链路如下：

```text
用户语音/文字
  -> AgentOS 硬件智能体
  -> RK3588 /api/agentos/v1/commands
  -> HTTP-MQTT Bridge /command
  -> MQTT Broker
  -> 录音豆 bean_ye_1
  -> 真实 command ack
  -> Bridge / RK3588
  -> AgentOS 生成最终 reply
```

职责必须保持清晰：

| 组件 | 负责内容 | 不负责内容 |
| --- | --- | --- |
| AgentOS 硬件智能体 | 理解自然语言、读取设备目录、选择设备和 capability、生成结构化命令 | 不访问 MQTT，不生成 Topic，不保存硬件凭据 |
| RK3588 | 校验 `device_id`、capability、参数、权限和幂等，调用 Bridge，等待真实结果 | 不根据产品名称猜设备，不处理 MQTT |
| HTTP-MQTT Bridge | 注册设备、映射协议、发布 MQTT、等待 ack、处理重试和超时 | 不理解自然语言，不把 publish 成功当动作成功 |
| 录音豆 | 连接 Wi-Fi/MQTT，执行录音动作，上报 availability/state/ack | 不访问 AgentOS，不访问 RK3588 HTTP 接口 |

## 2. 设备固定身份

这些字段是设备的稳定身份，换 Wi-Fi、重启或升级固件不能改变 `device_id`：

| 字段 | 值 | 说明 |
| --- | --- | --- |
| `device_id` | `bean_ye_1` | 全生态唯一，AgentOS 和 RK3588 只使用这个 ID |
| `name` | `录音豆` | 对用户展示的名称 |
| `device_type` | `audio-recorder` | 稳定设备类型 |
| `location` | 待部署填写 | 同类设备超过一个时必须填写 |
| `package_version` | `1.0`（直接 manifest） | 与最新 RK3588 设备目录格式一致 |
| 固件版本 | 当前联调记录为 `0.3.12-agent-hub-test` | 仅作为部署信息，不写入公开 manifest |

公开设备描述位于：

```text
hardware/device-packages/bean-ye-1/manifest.json
```

另外提供了符合最新硬件资料包校验器的包装文件：

```text
hardware/device-packages/bean-ye-1/device-package.json
```

两者区别：`manifest.json` 是当前 RK3588 设备目录直接读取的公开 manifest；`device-package.json` 是带 `package_type`、`package_id` 和 `protocol_version` 的完整注册包，供新设备包注册工具使用。不要把两份文件合并成一个私有配置。

## 3. 当前能力合同

录音豆 V1 只向 AgentOS 暴露以下三个能力：

| capability | 用户可说的话 | 参数 | 成功条件 | 默认风险 |
| --- | --- | --- | --- | --- |
| `record.start` | 开始录音、启动录音 | `{}` | 麦克风已打开并进入录音状态 | 改变设备状态，建议确认后执行 |
| `record.stop` | 停止录音、结束录音 | `{}` | 采集停止且文件已关闭保存 | 改变设备状态，建议确认后执行 |
| `status.read` | 查询录音状态、看看是否在录音 | `{}` | 返回设备当前真实状态 | 只读，可自动执行 |

三个能力的参数 Schema 都必须是：

```json
{
  "type": "object",
  "properties": {},
  "additionalProperties": false
}
```

AgentOS 不得生成目录之外的能力，也不得把“录音豆”扩展解释为音箱、文件传输或删除文件能力。后续增加能力必须先改设备包、Bridge 映射和验收测试。

## 4. AgentOS 硬件智能体配置

AgentOS 需要配置两个服务端 HTTP 动作。密钥只从 AgentOS 服务端密钥管理读取：

```text
设备目录：GET  http://<rk3588-host>:8095/api/agentos/v1/devices
执行命令：POST http://<rk3588-host>:8095/api/agentos/v1/commands
鉴权：Authorization: Bearer <AGENTOS_COMMAND_API_KEY>
```

当前联调 RK3588 地址是 `http://192.168.0.140:8095`；部署到其他网络时以实际地址替换，不能写死在录音豆固件中。

### 4.1 目录选择规则

1. 先读取设备目录。
2. 只使用目录中精确出现的 `device_id` 和 capability。
3. 只有一个录音豆时使用 `bean_ye_1`；多个同类设备时根据 `location` 向用户澄清。
4. 用户意图不明确时先聊天或澄清，不发送硬件命令。
5. 不生成 MQTT Topic、Bridge 地址或任何密钥。

### 4.2 命令请求示例

```json
{
  "request_id": "agentos-turn-20261014-0001",
  "conversation_id": "user-session-001",
  "user_request": "帮我开始录音",
  "command": {
    "device_id": "bean_ye_1",
    "capability": "record.start",
    "params": {}
  },
  "reason": "用户明确要求开始录音"
}
```

字段规则：

- `request_id`：一次 AgentOS 命令请求唯一，网络重试必须复用；同一个 ID 不能换设备、能力或参数。
- `conversation_id`：同一轮对话保持不变，但不作为硬件幂等键。
- `device_id`：必须从 RK3588 设备目录获取，固定为 `bean_ye_1`。
- `capability`：必须是 `record.start`、`record.stop` 或 `status.read`。
- `params`：当前必须是空对象，不能附加未定义字段。

### 4.3 最终回复规则

只有 RK3588 返回 `succeeded`，AgentOS 才能向用户说“已经开始/停止录音”。以下状态必须如实表达：

```text
queued / executing -> 命令已排队或正在执行
failed -> 设备执行失败
timed_out -> 等待设备确认超时
DEVICE_NOT_FOUND -> 设备未注册
CAPABILITY_NOT_SUPPORTED -> 设备不支持该动作
AUTO_EXECUTION_NOT_ALLOWED -> 需要用户确认或管理员授权
```

## 5. RK3588 命令接口

RK3588 侧按以下顺序处理：

1. 验证 AgentOS Bearer 密钥。
2. 验证 `request_id` 幂等关系。
3. 验证 `bean_ye_1` 已注册。
4. 验证 capability 属于录音豆。
5. 校验空参数 Schema。
6. 检查自动执行白名单和用户确认策略。
7. 生成内部 `plan_id`，调用 Bridge。
8. 等待真实 MQTT ack。
9. 将设备真实状态返回 AgentOS。

RK3588 不能只因为 MQTT publish 成功、Bridge 返回 HTTP 200 或设备收到消息就报告成功。

建议配置项只放在 RK3588 root-only 环境文件中：

```ini
AGENTOS_COMMAND_API_KEY=<在部署环境生成>
AGENTOS_AUTO_EXECUTE_CAPABILITIES=status.read
```

如果产品策略明确允许免确认开始/停止录音，再由负责人将 `record.start,record.stop` 加入白名单；该配置不写入 manifest，也不写入录音豆固件。

## 6. HTTP-MQTT Bridge 契约

Bridge 监听：

```text
GET  /health
POST /command
```

当前项目的 LAN 联调地址记录为 `http://192.168.0.107:19700`；实际部署时由 Bridge 所在主机地址决定。

### 6.1 `/health` 成功响应

```json
{
  "status": "ok",
  "service": "http-mqtt-bridge",
  "hub": {
    "status": "ok",
    "service": "smart-hub"
  }
}
```

### 6.2 `/command` 请求

```json
{
  "protocol_version": "1.0",
  "request_id": "agentos-turn-20261014-0001",
  "plan_id": "plan-001",
  "conversation_id": "user-session-001",
  "source": "rk3588",
  "command": {
    "device_id": "bean_ye_1",
    "capability": "record.start",
    "params": {}
  }
}
```

Bridge 必须按 `request_id` 查找幂等记录。重试同一个请求不能创建第二个 `command_id`，也不能重复向 MQTT 发布。

### 6.3 Bridge 成功响应

```json
{
  "ok": true,
  "protocol_version": "1.0",
  "request_id": "agentos-turn-20261014-0001",
  "command_id": "command-001",
  "device_id": "bean_ye_1",
  "capability": "record.start",
  "state": "succeeded",
  "message": "已经开始录音",
  "result": {
    "recording": true
  }
}
```

推荐状态映射：

| 场景 | HTTP | `state` | `ok` |
| --- | ---: | --- | --- |
| 真实动作完成 | 200 | `succeeded` | `true` |
| 已排队 | 202 | `queued` | `false` |
| 执行中 | 202 | `executing` | `false` |
| 设备失败 | 502 | `failed` | `false` |
| 设备离线 | 503 | `failed` | `false` |
| 等待 ack 超时 | 504 | `timed_out` | `false` |
| 请求字段错误 | 400 | `failed` | `false` |

## 7. MQTT 主题和消息

录音豆继续使用现有 MQTT 主题，不因接入 AgentOS 更换：

```text
hub/v1/devices/bean_ye_1/manifest
hub/v1/devices/bean_ye_1/availability
hub/v1/devices/bean_ye_1/state
hub/v1/devices/bean_ye_1/events
hub/v1/devices/bean_ye_1/commands
hub/v1/devices/bean_ye_1/command-acks
```

命令示例：

```json
{
  "type": "command",
  "command_id": "command-001",
  "conversation_id": "user-session-001",
  "device_id": "bean_ye_1",
  "capability": "record.start",
  "params": {},
  "issued_at": "2026-10-14T14:00:00+08:00"
}
```

录音豆成功 ack 必须带回同一个 `command_id`：

```json
{
  "command_id": "command-001",
  "device_id": "bean_ye_1",
  "state": "succeeded",
  "result": {
    "recording": true,
    "message": "已经开始录音"
  }
}
```

失败 ack 示例：

```json
{
  "command_id": "command-001",
  "device_id": "bean_ye_1",
  "state": "failed",
  "error_code": "TF_CARD_UNAVAILABLE",
  "message": "TF 卡不可用"
}
```

MQTT 发布成功只表示 Broker 接收，不表示录音动作完成。只有收到设备真实 `state=succeeded` ack 才能向用户确认。

## 8. 录音豆启动、状态和去重

设备启动和重连必须依次完成：

1. 连接 MQTT Broker。
2. 发布 manifest，声明 `bean_ye_1` 和三个能力。
3. 发布 `availability.status=online`。
4. 订阅 `commands`。
5. 周期发送心跳或在线状态。
6. 断线后自动重连，并重新发布 manifest 和 online。

availability 示例：

```json
{
  "device_id": "bean_ye_1",
  "status": "online"
}
```

离线示例：

```json
{
  "device_id": "bean_ye_1",
  "status": "offline",
  "reason": "mqtt_disconnect"
}
```

设备收到相同 `command_id` 时不能重复开始或停止录音，应返回已有终态或当前执行状态。重启后应保留足够的最近命令记录，至少覆盖 Bridge 重试窗口。

## 9. 状态上报

状态必须表示真实设备状态。建议最少包含以下字段：

```json
{
  "device_id": "bean_ye_1",
  "recording": false,
  "current_file": "",
  "tf_total_mb": 7571,
  "tf_free_mb": 7501,
  "wifi_connected": true,
  "mqtt_connected": true,
  "updated_at": "2026-10-14T14:00:00+08:00"
}
```

以上容量和联网状态字段来自当前真实联调记录；设备实际上报字段名若不同，以固件最终 payload 为准，Bridge 负责映射。未收到新的状态时，桌面上位机必须显示“最近状态时间”，不能把历史状态当成当前在线。

## 10. 标识字段不能混用

| 字段 | 作用 | 是否用于硬件幂等 |
| --- | --- | --- |
| `request_id` | AgentOS 一轮请求 ID，重试复用 | 是，跨 RK3588/Bridge 请求去重 |
| `conversation_id` | 连续对话上下文 | 否 |
| `plan_id` | RK3588 内部计划和审计 | 否 |
| `command_id` | Bridge/设备实际硬件命令 ID，ack 必须带回 | 是，设备端去重 |
| `device_id` | 物理设备唯一身份 | 不是命令去重键 |

不要把 `conversation_id` 当成命令 ID，也不要永久用 `request_id` 代替设备的 `command_id`。

## 11. 接入实施顺序

### RK3588 / AgentOS 开发人员

1. 将 `device-package.json` 注册到 RK3588 设备目录。
2. 确认目录能返回 `bean_ye_1`、`audio-recorder` 和三个结构化能力。
3. 配置硬件智能体读取目录并调用 `/api/agentos/v1/commands`。
4. 配置密钥和执行白名单，不把密钥下发给中枢或录音豆。
5. 验证普通聊天不会调用硬件接口，硬件请求才进入命令接口。

### Bridge / 中枢开发人员

1. 读取 `manifest.json`，使用私有部署配置补齐 Bridge 的 `base_url`。
2. 按 `mqtt-bridge-map.example.json` 配置 Topic、QoS、ack 和超时。
3. Bridge 使用独立且稳定的 MQTT `client_id`，不能和录音豆或 HubService 共用。
4. 验证 `/health`、`/command`、设备离线、ack 超时和重复 `request_id`。
5. 不在公开仓库提交真实 MQTT 用户名、密码或证书。

### 录音豆固件开发人员

1. 固定 `device_id=bean_ye_1`。
2. 发布 manifest、availability、state，并订阅 commands。
3. 支持 `record.start`、`record.stop`、`status.read`。
4. 只有真实动作完成后返回 `succeeded`。
5. 对重复 `command_id` 去重，断线自动重连并恢复上报。
6. 不保存 RK3588 地址，不调用 AgentOS，不把 API Key 写入固件。

## 12. 验收清单

### 注册与在线

- [ ] `bean_ye_1` 能在 RK3588 设备目录中找到。
- [ ] 目录中能力精确为 `record.start`、`record.stop`、`status.read`。
- [ ] 录音豆在线时 availability 为 `online`。
- [ ] 断电或断网后 availability 为 `offline`，历史状态不冒充在线。

### 命令执行

- [ ] “查询录音状态”调用 `status.read`，返回真实状态。
- [ ] “开始录音”只调用 `record.start`，真实进入录音后才返回成功。
- [ ] “停止录音”只调用 `record.stop`，文件关闭保存后才返回成功。
- [ ] 同一 `request_id` 重试不产生第二次 MQTT 发布。
- [ ] 同一 `command_id` 重复到达设备不重复执行。
- [ ] 离线、Bridge 不可用、ack 超时均不会返回成功。

### 安全与边界

- [ ] AgentOS 不直接访问 MQTT。
- [ ] 中枢上位机不保存 `AGENTOS_COMMAND_API_KEY`。
- [ ] 设备包不包含 `base_url`、Broker、Topic、Token、密码或证书。
- [ ] 普通聊天不触发硬件命令。
- [ ] 新能力先更新 manifest、Bridge 映射和测试，再交给 AgentOS 使用。

## 13. 当前资料中的待确认项

以下内容不能凭空补写，接入前由固件/部署人员确认：

1. 录音豆最终固件是否直接接受标准 `capability` 命令 envelope；若仍使用旧动作字段，只在 Bridge 边界映射，不改 AgentOS 命令。
2. 实际部署的 MQTT Broker 主机、TLS、用户名和证书路径。
3. 录音豆正式固件版本和安装位置。
4. `record.stop` 完成后最终文件名字段以及文件传输接口（V1 不把文件传输当作能力）。
5. `TF_CARD_UNAVAILABLE` 等错误码的最终枚举。

这些字段确认后，只更新 Bridge 私有映射和部署配置；不改变 `device_id`、三个 V1 capability 名称和公共协议边界。

## 14. 本地校验

在项目根目录执行：

```bash
python3 /tmp/rk3588-hardware-pack.0yDDlO/work/smart_home_voice_hub/device_package.py \
  validate hardware/device-packages/bean-ye-1/device-package.json
python3 -m json.tool hardware/device-packages/bean-ye-1/manifest.json >/dev/null
python3 -m json.tool hardware/device-packages/bean-ye-1/mqtt-bridge-map.example.json >/dev/null
```

如果临时仓库路径发生变化，使用最新 `RK3588-2/work/smart_home_voice_hub/device_package.py` 替换第一条命令中的路径。
