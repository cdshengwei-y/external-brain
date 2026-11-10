# 插件接入方案确认（按对方 2026-10-26 回复）

| 项目 | 内容 |
| --- | --- |
| 对应来函 | `HARDWARE_PLUGIN_HANDOFF_REPLY_2026-10-26.md` |
| 结论 | **能接，但传输只能是 stdio；不需要 Windows，也不需要跑 exe** |
| 我方改动 | 客户端已按对方 11 条确认逐条对齐，62 个相关用例通过 |

---

## 1. 关键结论

对方明确说：**没有远端 HTTP 接口**，正式交付是 Windows x64 的 `hardware-agent-mcp.exe`，传输是 stdin/stdout 行分隔 JSON-RPC。

但同一份文档第 5 节写了另一件事：exe 只是**启动器**，真正逻辑在 `payload/mcp_server.py`，纯 Python 标准库，Python 3.10+ 可跑。Windows 启动器的作用只是找到 Python 再把 `mcp_server.py` 拉起来。

所以结论是：

> 我们不需要 Windows，也不需要 exe，只要发布包里的 `payload/mcp_server.py`。
> **Mac 上直接 `python3 payload/mcp_server.py`，就是一个完全等价的 stdio MCP 服务。**

这正是"不跑他的 exe、只走协议接口"的落地方式——协议完全一样，只是把启动器换成 `python3`。

唯一风险：对方标注 **Mac 未验收**（第 6.1 节）。可能踩的点是文件编码、相对路径、Python 版本。我们已经准备了排查工具，见第 4 节。

---

## 2. 按对方回复改了什么

对方第 3 节列了 11 条假设，其中 5 条有差异。已全部对齐：

| 编号 | 差异 | 我方改动 |
| --- | --- | --- |
| P1 | 不是 POST 到 URL，是 stdio 行分隔 JSON | 早先的 stdio 客户端就是主路径，HTTP 传输保留但不再用于对方插件 |
| P3 | 通知不返回 202 也不返回响应体 | 原实现只写不读，本来就正确，未改 |
| P6 | 状态不止 succeeded/failed，还有 `queued`/`executing`/`timed_out`，可能带 `pending` | `ToolOutcome` 新增 `pending` 属性；非终态集合 `INCOMPLETE_STATUSES` |
| P7 | `timed_out` **不会**自动置 `isError=true` | 解析以**内层 `status`** 为准，不依赖 `isError`；只有 `succeeded` 才算完成 |
| P8 | 只有执行类工具要求 `request_id`/`conversation_id`/`user_request` | 注册/注销不在聊天里触发（`ADMIN_TOOL_NAMES`），无需改 |
| P9 | 同 ID 同参数返回原结果；同 ID 不同参数返回 `DUPLICATE_REQUEST` | 我们不重发、不换 ID；补了 `DUPLICATE_REQUEST` 话术 |
| P10 | 没有 `Mcp-Session-Id` | stdio 本来就不带，未改 |

另外三处顺手补齐：

1. **错误码话术**：从 14 条补到 30 条，覆盖对方 2.4 表里的 `DEVICE_TIMEOUT`（实际使用的码，不是 `COMMAND_TIMEOUT`）、`DEVICE_ACK_INVALID`、`RK3588_UNAVAILABLE`、`QUEUE_FULL`、`UNAUTHORIZED`、`INVALID_DEVICE_PACKAGE` 等。未知码按"失败、不自动重试写操作"处理。
2. **JSON-RPC 层错误带业务码**：对方 4.4 样例里，参数非法是 `error.code=-32000` + `error.data.error_code=INVALID_PARAMS`。现在异常会带出 `error_code`，用户听到的是"命令参数不对，我没有执行"，而不是"返回错误 -32000"。
3. **字段限长**：按 `tools/list` 的 `inputSchema`，`request_id`/`plan_id` ≤120、`conversation_id` ≤120、`user_request` ≤2000，超出会被判 `INVALID_PARAMS`，已在发送前截断（宿主内部仍用原始值）。
4. **ping**：对方说没有 `/health`，用 MCP `ping` 探活。客户端加了 `ping()`，自检里会打印结果。

---

## 3. 还需要你（或同事）给的东西

按对方第 0 节的四项 + 发布物：

| 项 | 用途 | 备注 |
| --- | --- | --- |
| 发布包 ZIP | 拿 `payload/` 里的 `mcp_server.py` | `dist/hardware-agent-deepseek-harness-0.1.1-windows.zip`，SHA-256 `00ADA8...3181` |
| `RK3588_BASE_URL` | 插件下游地址 | 对方示例 `http://192.168.0.140:8099`，实际待确认 |
| `AGENTOS_COMMAND_API_KEY` | 下游鉴权 | 走安全渠道，不要贴群里 |
| `HARDWARE_AGENT_TENANT_ID` / `ACTOR_REF` | 联调身份 | 由他们按联调账号发 |
| 测试设备 + 设备包 | 真实联调 | 对方说**没有**虚拟设备/测试租户，需要指定一台真设备 |

没有 ZIP 就无法真跑——所有协议行为只能继续用 mock 验证。拿到 ZIP 就能立刻自检。

---

## 4. 拿到 ZIP 后的三步

```bash
# 1. 解压 + 校验哈希 + 定位入口（不需要 Windows）
python3 tools/prepare_plugin.py --zip ~/Downloads/hardware-agent-*.zip

# 2. 按提示配好五个环境变量后自检：握手 / ping / 工具清单 / 设备列表
python3 tools/check_plugin.py \
  --command ~/.smart-hub/hardware-plugin/<hash>/payload/mcp_server.py \
  --cwd ~/.smart-hub/hardware-plugin/<hash>/payload

# 3. 接到中枢，然后对桌宠说话
HARDWARE_AGENT_MCP_COMMAND=.../payload/mcp_server.py \
HARDWARE_AGENT_MCP_CWD=.../payload \
./tools/start_pet_hub.sh
```

`prepare_plugin.py` 会做四件事：算 SHA-256 与对方给的值比对、解压、找 `mcp_server.py`、检查文件是否 UTF-8 可读（Windows 上常见的编码坑），最后直接打印可复制的命令。

---

## 5. 真机联调顺序（照抄对方第 7 节）

对方明确当前**没有虚拟设备**，所以第一次真机联调必须走注册流程：

1. 配好身份与下游地址，启动插件，完成 `initialize` / `initialized` / `tools/list` / `ping`；
2. `register_device` 注册测试设备包 → `verify_device` 探活 → 状态变 `online`；
3. `list_devices` / `get_device_status` 确认可见；
4. 桌宠侧说"把 XX 打开"→ 先播报确认 → 用户确认 → 执行；
5. 只在返回 `status == "succeeded"` 时播报"已经完成"；
6. 补测四种异常：离线、超时、重复请求、错误参数。

第 2 步是新增的前置动作，之前没做过——聊天里不允许注册（`ADMIN_TOOL_NAMES` 直接拦），需要在设置流程或命令行里单独跑一次。需要我给 `register_device` 配一个小工具吗？

---

## 6. 仍然存在的三个不确定

1. **Mac 未验收**：`mcp_server.py` 在 Mac 上可能出现路径、编码或依赖问题。第 4 节的自检会直接暴露，不会静默失败。
2. **`serverInfo.version` 是 0.1.0，包是 0.1.1**：对方承认是元数据不一致。我们不校验这个字段，只记录。
3. **没有命令结果查询工具**：超时后只能"同参数同 request_id 再调一次"，靠插件幂等记录兜底。我们的实现本来就是这么做的，但前提是**不能换 ID**——这条有单测守着。
