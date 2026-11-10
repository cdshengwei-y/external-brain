# 智能硬件体插件接入交接单

| 项目 | 内容 |
| --- | --- |
| 文档版本 | v1.0 |
| 日期 | 2026-10-26 |
| 我方 | 桌宠 / 智能中枢（宿主侧） |
| 对方 | 硬件插件团队（`hardware-agent-mcp`） |
| 目的 | 确认插件架构形态，拿到接口信息，双方开始联调 |

---

## 0. 一句话说明

桌宠这边**不运行你们的 exe**，只作为 MCP 客户端接入插件接口。宿主侧代码已经写好并自测通过（stdio 与远端 HTTP 两种传输都支持），现在缺的是**插件长什么样**和**接口在哪、怎么调**。

只要下面第 2、3 节填完，我方当天就能连上并给出联调结果。

---

## 1. 我方现状（已就绪，不需要你们改代码）

链路：

```
桌宠（ES8311 麦克风）→ Wi-Fi → 中枢（Mac，局域网）→ 聊天模型 → 插件接口 → 设备
                                        ↑                              |
                                        └──────── 工具结果回填 ─────────┘
```

已实现并自测通过的部分：

1. **MCP 客户端**：`initialize` → `notifications/initialized` → `tools/list` → `tools/call`，协议版本 `2025-03-26`。
2. **两种传输**：
   - 远端接口：`HARDWARE_AGENT_MCP_URL=http://<host>:<port>/<path>`，POST JSON-RPC，响应支持 `application/json` 与 `text/event-stream`，自动携带 `Mcp-Session-Id`，自动绕过系统代理；
   - 本地子进程：`HARDWARE_AGENT_MCP_COMMAND`，stdin/stdout 走协议、stderr 单独收日志。
3. **双层错误处理**：JSON-RPC 层 `error` 与业务层 `result.content[0].text` 里的 `status/error_code` 都解析；只有 `status == "succeeded"` 才对外说"已完成"。
4. **幂等**：写操作带 `request_id`，超时或断连**不换 ID 重发**；查询类工具断连后自动重连重试一次。
5. **二次确认**：任何写操作先播报"我准备对「客厅灯」执行「开关灯」，要执行吗"，用户口头确认后才真正调用。
6. **自检工具**：`python3 tools/check_plugin.py --url <接口地址>`，一条命令跑完连接、握手、工具清单、设备列表。

我们按交接文档 v0.1.1 实现了全套假设，**第 4 节列了这些假设，请逐条确认或指出差异**。

---

## 2. 需要你们回答：插件架构

这一节决定我们怎么部署和怎么排障，请按实际情况回答（有不确定的写"待确认"即可，但不要空着）。

### 2.1 部署形态

| 编号 | 问题 | 回答 |
| --- | --- | --- |
| A1 | 插件是**常驻服务**，还是由宿主按需拉起的**子进程**？ | |
| A2 | 如果是常驻服务：跑在哪台机器（Windows / Linux / 容器 / 云）？谁负责启动和守护？ | |
| A3 | 如果是子进程：原本设计是 stdio 还是本地端口？有没有跨平台版本？ | |
| A4 | 一个插件实例能同时服务几个客户端？桌宠、手机 App、Web 后台会不会共用同一个实例？ | |

### 2.2 网络与访问

| 编号 | 问题 | 回答 |
| --- | --- | --- |
| A5 | 接口的 IP / 端口 / 路径是什么？和桌宠、中枢在**同一个局域网**吗？ | |
| A6 | 是否需要 VPN、端口映射、防火墙放行、白名单？我们这边需要提前开什么？ | |
| A7 | 有没有健康检查端点（例如 `GET /health`），返回什么？ | |
| A8 | 是否支持 HTTPS？如果只有 HTTP，是否接受内网明文？ | |

### 2.3 接口形态

| 编号 | 问题 | 回答 |
| --- | --- | --- |
| A9 | 接口是标准 **MCP Streamable HTTP**（POST JSON-RPC），还是自定义 REST / WebSocket / gRPC？ | |
| A10 | 如果响应是 SSE：是一条 `message` 事件就结束，还是会持续推送（通知/进度）？ | |
| A11 | 是否要求客户端先调 `initialize`？有没有额外的握手或版本协商？ | |

### 2.4 状态与身份

| 编号 | 问题 | 回答 |
| --- | --- | --- |
| A12 | 会话状态绑定在 `Mcp-Session-Id` 上吗？会话多久过期？ | |
| A13 | `tenant_id` / `actor_ref` 是**插件自己持有**，还是每个请求都要带？ | |
| A14 | 鉴权方式：`Authorization: Bearer <token>`，还是自定义 header / 签名？token 怎么发、有效期多久、怎么续？ | |
| A15 | 一个 token 对应一个用户，还是对应一个租户（多用户共用一个 token）？ | |

### 2.5 设备与能力

| 编号 | 问题 | 回答 |
| --- | --- | --- |
| A16 | 设备清单从哪来（RK3588 / 云平台 / 本地配置文件）？是实时查询还是缓存？缓存多久？ | |
| A17 | `tools/list` 是**固定 7 个工具**，还是会按设备能力动态增减？ | |
| A18 | 设备能力（capability）命名是否稳定？新增设备类型时会不会引入新工具名？ | |
| A19 | 现在有没有可用的**测试租户 / 测试设备**？能不能给我们一套用来联调（不用真灯）？ | |

### 2.6 执行语义

| 编号 | 问题 | 回答 |
| --- | --- | --- |
| A20 | `execute_command` 是**同步**等设备返回，还是立即返回 `task_id` 让客户端轮询？ | |
| A21 | 单次执行最长可能耗时多久？建议我们客户端超时设多少秒？ | |
| A22 | `request_id` 的幂等窗口多长？超过窗口后重发会怎样？ | |
| A23 | 超时之后有没有查询接口能拿到"到底执行没执行"？ | |

### 2.7 运维与版本

| 编号 | 问题 | 回答 |
| --- | --- | --- |
| A24 | 插件日志在哪？能否按 `request_id` 追踪一次调用？ | |
| A25 | 协议或工具签名变更时，怎么通知我们？有没有版本号可读？ | |
| A26 | 出问题时找谁？有没有值班/群？ | |

---

## 3. 需要你们提供：接口信息

### 3.1 必备项（缺一项就接不上）

| 项 | 说明 | 填写 |
| --- | --- | --- |
| 接口地址 | 完整 URL，含路径，例如 `http://192.168.0.20:18800/mcp` | |
| 传输类型 | `MCP Streamable HTTP` / `stdio` / `REST` / 其他 | |
| 协议版本 | 例如 `2025-03-26` | |
| 鉴权方式 | header 名 + 前缀，例如 `Authorization: Bearer xxx` | |
| 令牌 | 测试用 token（走安全渠道给，不要贴群里） | |
| 工具清单 | 一条 `tools/list` 的**原始返回 JSON**（不要改写） | |
| 错误码表 | `error_code` + 含义 + 是否可重试，见 3.2 | |
| 建议超时 | 查询类 / 执行类分别多少秒 | |

### 3.2 错误码表（请按这个格式给全）

| error_code | 含义 | 客户端应该怎么做 |
| --- | --- | --- |
| 示例 `DEVICE_OFFLINE` | 设备不在线 | 告知用户，不重试 |
| 示例 `CAPABILITY_NOT_SUPPORTED` | 设备不支持该能力 | 告知用户，换设备或换能力 |
| 示例 `INVALID_PARAMS` | 参数不合法 | 不重试，修正参数 |
| 示例 `COMMAND_TIMEOUT` | 设备响应超时 | 不换 request_id 重发，先查询结果 |
| 示例 `DUPLICATE_REQUEST` | 重复的 request_id | 复用已有结果 |
| | | |

### 3.3 样例（各一段就够）

1. `initialize` 请求 + 响应；
2. `tools/call` **成功**的完整响应（含 `content[0].text`）；
3. `tools/call` **业务失败**的完整响应（`isError: true` 的样子）；
4. `tools/call` **参数非法**的响应。

有这四段，我们就能确定解析逻辑要不要改。

### 3.4 可选项（有就更好）

- 并发上限 / QPS 限制；
- 是否需要客户端定期心跳；
- 测试环境与生产环境是否分开、地址分别是什么；
- 设备的 `device_id` 命名规则（我们要不要做展示名映射）。

---

## 4. 我方实现假设（请逐条确认）

这一节是**我们已经写进代码的假设**。请回复「确认」或指出差异，有差异的我们改代码，不需要你们迁就。

| 编号 | 我们的假设 | 确认 / 差异 |
| --- | --- | --- |
| P1 | 传输为 JSON-RPC 2.0，`POST` 到单一 URL | |
| P2 | `initialize` 返回 `protocolVersion`、`serverInfo`、`capabilities` | |
| P3 | `notifications/initialized` 返回 `202` 且无响应体 | |
| P4 | `tools/list` 返回 `result.tools[]`，每项含 `name` / `description` / `inputSchema` | |
| P5 | `tools/call` 返回 `result.content[0].text`，是一段 **JSON 字符串** | |
| P6 | 业务结果字段为 `status`（`succeeded` / `failed`）、`error_code`、`message` | |
| P7 | 失败时同时置 `result.isError = true` | |
| P8 | 写操作参数必带 `request_id`、`conversation_id`、`user_request` | |
| P9 | `request_id` 重复时返回原结果并标记重复，不会二次执行 | |
| P10 | 服务端可下发 `Mcp-Session-Id`，后续请求带上即可 | |
| P11 | 工具名以 `hardware_agent_` 开头，共 7 个（list/get/verify/register/unregister/execute/execute_commands） | |

---

## 5. 信息到手后的流程

```bash
# 1. 自检（连接、握手、工具清单、设备列表一次跑完）
python3 tools/check_plugin.py --url http://<host>:<port>/<path> --token <token>

# 2. 配到中枢
export HARDWARE_AGENT_MCP_URL=http://<host>:<port>/<path>
export HARDWARE_AGENT_MCP_TOKEN=<token>
export PET_AGENT_CHAT_BASE_URL=...   # 对话模型（另一件事，见第 6 节）
export PET_AGENT_CHAT_MODEL=...
./tools/start_pet_hub.sh

# 3. 端到端：对桌宠说「有哪些设备」→「把客厅灯打开」→「执行」
```

自检通过 = 协议对齐；自检不通过 = 卡在哪一步、原因是什么，我们会带着日志找你们。

---

## 6. 另一件事：对话模型（与插件无关，但决定"问什么答什么"）

插件只解决"能不能控制设备"。桌宠要真能"问什么答什么"，还需要一个**支持 function calling 的 OpenAI 兼容模型端点**（地址 / 模型名 / 密钥）。这台我们自己找也行，如果有内部现成的，一并告诉我们地址即可。

---

## 7. 回填方式

把第 2、3 节填好回传即可；第 4 节逐条写「确认」或差异。涉及令牌请走安全渠道（不要把密钥贴在群里或文档里）。

如果插件短期内只有 Windows 版、且不方便提供 HTTP 接口，也请直说——那样我们改成"把中枢部署到那台 Windows 机器上"的形态，方案不一样，但同样能接。
