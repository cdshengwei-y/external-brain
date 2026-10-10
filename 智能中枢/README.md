# 智能中枢项目

## 1. 项目定位

本项目不是复刻某个现有硬件产品，而是为自有智能体提供连接和控制现实世界设备的能力。

智能中枢负责设备发现、能力管理、任务调度、状态汇总和协议转换；具体联网外设负责传感器采样、执行动作以及与设备安全相关的本地闭环。

总体链路：

```text
用户
  │ 自然语言对话
  ▼
智能体
  │ 理解意图，生成结构化任务
  ▼
智能中枢
  │ 校验能力，调度任务，通过 MQTT 下发
  ▼
联网硬件外设
  │ 执行动作，读取传感器，回传状态
  ▼
智能中枢
  │ 汇总结果和事件
  ▼
智能体
  │ 组织自然语言反馈
  ▼
用户
```

## 2. 第一阶段目标

第一阶段使用现有的 `smart recording beam` 验证最小闭环，不直接做智能烧水壶或智能微波炉。

验收链路：

```text
设备配对
  → 设备注册
  → 上报在线状态
  → 上报按钮事件
  → 中枢或智能体发送控制命令
  → 录音豆控制指示灯
  → 回传执行状态
```

第一阶段只要求录音豆实现：

- 在线状态和心跳；
- 按钮事件上报；
- 指示灯状态控制；
- 设备能力描述；
- 断线重连；
- 命令执行结果和错误上报。

录音音频传输暂不纳入第一阶段核心链路。

## 3. 系统结构

```text
现有智能体
    │ 本地 REST API + 事件流
    ▼
桌面中枢应用
    ├─ 设备管理
    ├─ 能力模型
    ├─ 任务调度器
    ├─ 状态和事件日志
    └─ 本地 MQTT Broker
          │ MQTT + JSON
          ▼
smart recording beam
```

桌面应用分为两层：

- 本地中枢服务：负责 MQTT、设备注册、命令路由、任务状态和对外 API；
- 桌面界面：负责设备查看、配对、测试控制和日志展示。

中枢服务与桌面界面分离，未来可以把中枢服务迁移到专用硬件，桌面应用继续作为管理端。

当前参考实现使用 Python 标准库，先固定协议、能力模型和服务边界；后续桌面界面或服务运行时可以替换，不改变设备协议。

## 4. 通信边界

### 4.1 智能体到中枢

智能体不直接操作 MQTT，而是调用中枢本地 API：

```text
GET  /api/v1/devices
GET  /api/v1/devices/{device_id}
GET  /api/v1/devices/{device_id}/state
POST /api/v1/devices/{device_id}/commands
GET  /api/v1/commands/{command_id}
GET  /api/v1/commands/{command_id}/wait?timeout_ms=30000
POST /api/v1/commands/{command_id}/cancel
GET  /api/v1/events
```

智能体负责理解自然语言和拆解任务；中枢负责能力校验、任务调度和执行状态管理。

`GET /api/v1/devices/{device_id}/state` 返回设备最近一次通过 MQTT 上报的真实状态；如果设备还没有上报过状态，响应中的 `available` 为 `false`，不能把缺失状态当成设备正常。

智能体发送命令时使用以下结构，不直接操作 MQTT：

```json
{
  "conversation_id": "conversation-001",
  "device_id": "bean_ye_1",
  "capability": "record.start",
  "params": {},
  "policy": "queue"
}
```

`POST /api/v1/devices/{device_id}/commands` 只代表命令已被中枢接收。智能体应保存返回的 `command_id`，再调用 `GET /api/v1/commands/{command_id}/wait?timeout_ms=30000` 等待最终结果。响应中的 `completed` 为 `true` 时，只有 `command.state` 为 `succeeded` 才能向用户确认动作完成；`failed`、`cancelled`、`superseded` 和 `expired` 都必须如实反馈。

### 4.2 中枢到设备

设备侧统一采用 MQTT + JSON：

```text
hub/v1/devices/{device_id}/availability
hub/v1/devices/{device_id}/state
hub/v1/devices/{device_id}/events
hub/v1/devices/{device_id}/commands
```

设备接入时应上报：

```json
{
  "device_id": "recording-beam-001",
  "device_type": "recording_beam",
  "protocol_version": "1.0",
  "firmware_version": "待填",
  "capabilities": [
    "button.press",
    "indicator.set"
  ]
}
```

## 5. 异步任务和并发控制

硬件命令不是同步请求。每条命令都必须有独立的 `command_id`，并区分：

```text
created      已创建
accepted     中枢已接收
queued       等待执行
running      硬件正在执行
succeeded    执行完成
failed       执行失败
cancelled    被取消
superseded   被新命令替代
expired      超时失效
```

智能体只有收到 `succeeded` 才能向用户确认“已经完成”；收到 `accepted` 或 `running` 时只能说明“已接收”或“正在执行”。

第一版默认规则：

- 每个设备默认只允许一个正在执行的长任务；
- 查询类命令可以并行；
- 灯光、音量等状态类命令采用最新命令生效；
- 长任务收到新命令时必须明确排队、替换、取消或拒绝；
- 命令必须关联设备、会话和任务状态；
- 设备离线时不能把命令伪装成已完成；
- 高风险设备必须支持取消和硬件急停。

示例：

```text
命令 A 正在执行
  → 命令 B 到达
  → 中枢判断两者是否冲突
  → 排队 / 替换 / 取消 / 并行
  → 两个命令的状态均可追踪
```

## 6. 开发顺序

### 阶段一：`smart recording beam` + 软件中枢

- 定义设备能力模型和 MQTT 主题；
- 实现本地 MQTT Broker 管理；
- 实现设备注册、在线状态、按钮事件和指示灯控制；
- 实现任务状态、命令 ID、超时、取消和替换；
- 提供本地 REST API、事件流和桌面控制台；
- 接入现有智能体。

### 阶段二：ESP32 灯光和音箱节点

- 增加更直观的实体反馈；
- 验证多设备管理；
- Home Assistant 只作为可选外部适配器，不进入核心链路。

### 阶段三：低压传感器和执行器节点

- 加入温度、风扇、低压加热片、舵机等；
- 验证“目标值—传感器—本地闭环—任务结果”；
- 不接入 220V 和高风险设备。

### 阶段四：摄像头感知节点

- 摄像头作为感知类外设接入；
- 智能体根据视觉结果调用中枢任务；
- 不改变已有设备协议和任务系统。

### 阶段五：桌面机械臂

- 用于操作没有联网接口的传统设备；
- 放在设备协议、任务调度和安全控制稳定之后；
- 后续再评估按键、旋钮、插拔和物理定位能力。

## 7. 家电外设原则

智能烧水壶、微波炉等未来采用自研联网控制板，而不是让通用中枢直接控制危险执行器。

```text
中枢：下发目标和任务
设备控制板：采样、调功率、执行安全闭环
```

例如烧水壶控制板自行负责：

- 温度采样；
- 加热控制；
- 水位和干烧检测；
- 超温保护；
- 本地急停；
- 任务完成确认。

微波炉必须单独设计控制板、安全联锁和外壳防护。第一阶段不涉及市电继电器、微波炉高压部分或直接改造现有微波炉。

## 8. 第一阶段完成标准

1. 录音豆可以通过配置或配对码加入中枢；
2. 桌面应用能显示录音豆在线状态；
3. 录音豆按钮事件能被中枢接收并记录；
4. 控制台和智能体都能切换录音豆指示灯；
5. 智能体能通过本地 API 创建和查询任务；
6. 命令 A 未完成时，命令 B 能按策略排队、替换、取消或拒绝；
7. 设备断线、恢复和命令超时都有明确状态；
8. 局域网断开云服务时，基础演示仍可完成。

## 9. 第一阶段明确不包含

- 云端服务和远程访问；
- 中枢内置大模型；
- BLE 自动配网；
- 录音音频流管理；
- 智能烧水壶实机；
- 智能微波炉实机；
- 220V 或微波炉高压控制；
- 中枢实体硬件；
- 最终桌面应用技术栈选型。

## 10. 当前代码和启动方式

当前代码位于：

```text
hub_core/              中枢核心、SQLite 状态库、任务调度和 MQTT 适配层
run.py                 本地 HTTP API 启动入口
tests/                 调度器和并发策略测试
requirements.txt       可选 MQTT 客户端依赖
```

直接启动本地 API：

```bash
cd 智能中枢
python3 run.py
```

默认地址：`http://127.0.0.1:8787`

当前不配置 `--mqtt-host` 时使用内存传输，适合先验证 API 和任务调度。接入真实 MQTT Broker 时：

```bash
python3 -m pip install -r requirements.txt
python3 run.py --mqtt-host 127.0.0.1 --mqtt-port 1883
```

如果要让 RK3588 通过 HTTP 控制当前 MQTT 录音豆，在同一个中枢进程中同时开启兼容桥：

```bash
python3 run.py \\
  --host 0.0.0.0 \\
  --mqtt-host 0.0.0.0 \\
  --mqtt-port 1883 \\
  --mqtt-client-id smart-hub-main \\
  --bridge-host 0.0.0.0 \\
  --bridge-port 19700
```

桥接接口为 `POST /command`，它接收 RK3588 硬件网关格式，并把 `command.action` 映射为现有中枢能力。桥接层会复用 `request_id` 作为本地 `command_id`，避免网络重试重复下发 MQTT 命令；只有收到真实设备 `succeeded` ack 才返回 `ok: true`。

```bash
curl -X POST http://127.0.0.1:19700/command \\
  -H 'Content-Type: application/json' \\
  -d '{
    "request_id": "hub-20261010-0001",
    "source": "rk3588",
    "command": {
      "device_id": "bean_ye_1",
      "action": "record.start",
      "params": {}
    }
  }'
```

桥接服务健康检查：`GET http://127.0.0.1:19700/health`。

注册一个测试设备：

```bash
curl -X POST http://127.0.0.1:8787/internal/v1/devices/register \
  -H 'Content-Type: application/json' \
  -d '{
    "device_id": "recording-beam-001",
    "device_type": "recording_beam",
    "protocol_version": "1.0",
    "firmware_version": "dev",
    "capabilities": ["button.press", "indicator.set"]
  }'
```

发送指示灯命令：

```bash
curl -X POST http://127.0.0.1:8787/api/v1/devices/recording-beam-001/commands \
  -H 'Content-Type: application/json' \
  -d '{
    "conversation_id": "demo-001",
    "capability": "indicator.set",
    "params": {"color": "blue", "mode": "solid"}
  }'
```

运行测试：

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

当前版本已经实现设备注册、SQLite 持久化、命令生命周期、排队、替换、取消、事件列表、SSE 事件流、真实录音豆 MQTT 接入和第一阶段桌面上位机。

下一阶段三层硬件产品的总体架构、最先设计对象和实施顺序，见：

```text
硬件总体架构与实施路线.md
```

随身设备与固定式中枢的第一版接口协议见：

```text
随身设备与固定式中枢接口协议.md
```

### MQTT 设备协议补充

设备发布和订阅的主题包括：

```text
hub/v1/devices/{device_id}/manifest
hub/v1/devices/{device_id}/availability
hub/v1/devices/{device_id}/state
hub/v1/devices/{device_id}/events
hub/v1/devices/{device_id}/commands
hub/v1/devices/{device_id}/command-acks
```

第一阶段使用真实录音豆验证链路。用户通过项目根目录的 `外接大脑上位机.app` 输入中文，不需要在终端输入命令，也不使用模拟器。

真实 ESP32 设备通过局域网连接 MQTT Broker。双击上位机后，它会优先复用已经运行的 Broker 和中枢服务；如果本机服务未启动，会在后台自动启动，用户不需要打开终端。

当前开发电脑的局域网地址是 `192.168.0.107`，真实设备的 MQTT Broker 地址应填写这个地址。该 LAN 配置只适合可信家庭网络联调，仍然没有认证和 TLS。

ESP32 接入模板位于 `firmware/esp32_recording_beam_template/`。它只实现协议骨架，真实录音豆的芯片、引脚和音频功能仍需根据硬件资料补齐。
