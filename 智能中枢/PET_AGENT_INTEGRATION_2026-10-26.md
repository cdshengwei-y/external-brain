# 桌宠智能化接入说明（2026-10-26）

对应交接文档：`桌宠接入智能硬件体插件交接文档 v0.1.1`。

本仓负责的是**桌宠这一侧的宿主实现**：对话宿主 + MCP 客户端。硬件插件
（`hardware-agent-mcp.exe`）、设备权限校验、设备在线验证、命令转发与
`request_id` 幂等由插件团队提供，本仓不重复实现。

## 1. 链路

```text
桌宠唤醒词（本地 / 服务端均可）
  -> 录音 + ASR
  -> PetAgent（中枢）：把用户话、历史、工具清单交给对话模型
  -> 模型决定是否调用工具
  -> McpStdioClient：JSON-RPC over stdio
  -> hardware-agent-mcp(.exe)
  -> RK3588 / 硬件网关 / 真实设备
  -> 工具结果原样回填模型
  -> 模型生成最终回复 -> TTS -> 桌宠播放
```

写操作多一道确认：模型请求 `execute_command(s)` 时不直接执行，宿主先播报
「我准备对『客厅灯』执行『开关灯』，要执行吗？」，用户回答"执行"后才调用插件。

## 2. 本轮新增

| 文件 | 作用 |
| --- | --- |
| `agent_gateway/hardware_mcp.py` | MCP stdio 客户端：进程拉起、三管道、握手、工具发现、双层错误解析、超时与重启策略、日志脱敏 |
| `agent_gateway/pet_agent.py` | 对话宿主：OpenAI 兼容模型客户端、工具循环（≤4 轮）、写操作确认计划（默认 60 秒）、语音确认/取消解析、结果话术与防夸大校验 |
| `agent_gateway/portable.py` | 新增 `agent` 参数：唤醒后的每一轮对话交给 PetAgent，回复文本与 TTS 音频仍走原随身设备接口 |
| `run.py` | 新增 `--agent`：按环境变量组装桌宠大脑，配置缺失时自动退回原 RK3588 路由 |
| `tools/mock_hardware_mcp.py` | 按文档协议实现的 mock 插件（7 个工具 + 全部错误码），本机无 exe 也能联调 |
| `tools/pet_agent_console.py` | 文字控制台，支持 `--demo` 离线演示 |
| `tests/test_hardware_mcp.py` | 14 个协议层用例 |
| `tests/test_pet_agent.py` | 26 个宿主层用例（含装配降级与便携接口集成） |

## 3. 环境变量

### 3.1 远端接口模式（推荐）

插件已经跑在别处时，宿主只发 HTTP 请求，不需要本地可执行文件，也不受对方系统限制。

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `HARDWARE_AGENT_MCP_URL` | 是 | 完整接口地址，例如 `http://192.168.0.20:18800/mcp`，必须 `http://` 或 `https://` 开头 |
| `HARDWARE_AGENT_MCP_TOKEN` | 视服务端 | 访问令牌，默认按 `Authorization: Bearer <token>` 发送 |
| `HARDWARE_AGENT_MCP_TOKEN_HEADER` | 否 | 自定义鉴权头名，默认 `Authorization` |
| `HARDWARE_AGENT_MCP_TOKEN_PREFIX` | 否 | 令牌前缀，默认 `Bearer `（自定义头时可设空） |
| `HARDWARE_AGENT_MCP_STARTUP_TIMEOUT` | 否 | 握手超时，默认 10 秒 |
| `HARDWARE_AGENT_MCP_CALL_TIMEOUT` | 否 | 单次工具调用超时，默认 60 秒 |

远端模式下**不再需要**四个身份变量（`HARDWARE_AGENT_TENANT_ID` 等），也不再需要 `HARDWARE_AGENT_ALLOW_MISSING_IDENTITY`——身份由服务端持有。

传输按 MCP Streamable HTTP 实现：POST JSON-RPC，响应支持 `application/json` 与 `text/event-stream`；服务端下发 `Mcp-Session-Id` 时后续请求自动携带；请求自动绕过系统 `HTTP_PROXY`（内网调用被代理劫持会表现为 502）。

### 3.2 本地子进程模式

插件由宿主按会话拉起时，注入配置（文档第 2.1 节）：

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `HARDWARE_AGENT_TENANT_ID` | 是 | 租户/家庭空间 ID |
| `HARDWARE_AGENT_ACTOR_REF` | 是 | 当前用户 ID |
| `RK3588_BASE_URL` | 是 | RK3588 硬件服务地址 |
| `AGENTOS_COMMAND_API_KEY` | 是 | 运行时密钥，只经进程环境变量传递 |
| `RK3588_HTTP_TIMEOUT` | 否 | 默认 15 秒 |
| `VHA_DATA_DIR` | 建议 | 默认 `~/.smart-hub/hardware-agent`，自动创建 |

`HARDWARE_AGENT_MCP_URL` 与 `HARDWARE_AGENT_MCP_COMMAND` 同时存在时，**URL 优先**。

### 3.3 宿主自身配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PET_AGENT_ENABLED` | `1` | 设为 `0` 关闭桌宠大脑 |
| `HARDWARE_AGENT_ALLOW_MISSING_IDENTITY` | — | 本地 mock 联调时设为 `1`，跳过身份变量校验；真实插件不要设 |
| `HARDWARE_AGENT_MCP_COMMAND` | — | 插件可执行文件路径；`.py` 结尾会自动补解释器 |
| `HARDWARE_AGENT_MCP_CWD` | 插件目录 | 工作目录 |
| `HARDWARE_AGENT_MCP_STDERR_LOG` | — | 插件 stderr 落盘路径 |
| `PET_AGENT_CHAT_BASE_URL` | 回退 `OPENAI_BASE_URL` | 模型接口，带 `/v1` |
| `PET_AGENT_CHAT_MODEL` | 回退 `OPENAI_MODEL` | 模型名 |
| `PET_AGENT_CHAT_API_KEY` | 回退 `OPENAI_API_KEY` | 模型密钥 |
| `PET_AGENT_MAX_TOOL_ROUNDS` | `4` | 单轮对话工具往返上限 |
| `PET_AGENT_CONFIRM_TTL` | `60` | 确认计划有效期（秒） |
| `PET_AGENT_NARRATE` | `model` | `local` = 执行结果用固定话术，省一次模型调用 |


## 4. 启动

```bash
# 中枢 + 桌宠大脑（配置不全时会打印原因并退回旧链路）
PET_AGENT_CHAT_BASE_URL=http://192.168.0.140:8000/v1 \
PET_AGENT_CHAT_MODEL=qwen2.5-7b-instruct \
HARDWARE_AGENT_TENANT_ID=tenant-a \
HARDWARE_AGENT_ACTOR_REF=desktop-user \
RK3588_BASE_URL=http://192.168.0.140:8099 \
AGENTOS_COMMAND_API_KEY=*** \
python3 run.py --host 0.0.0.0 --port 8787 --agent

# 本机联调（mock 插件，不需要 Windows exe）
python3 tools/pet_agent_console.py --demo

# 接真实模型、mock 插件
python3 tools/pet_agent_console.py \
  --model-url http://127.0.0.1:8000/v1 --model qwen2.5-7b-instruct

# 接远端插件接口（首选，不关心对方是什么系统）
python3 tools/check_plugin.py --url http://192.168.0.20:18800/mcp --token ***

# 接本地插件进程（对方给了可执行文件时）
python3 tools/pet_agent_console.py --real-plugin \
  --mcp "C:/Program Files/smart-hardware/hardware-agent-mcp.exe"
```

## 5. 与交接文档的对应关系

| 文档要求 | 落点 |
| --- | --- |
| 2.1 进程环境变量注入、三管道、不走 `cmd /c` | `HardwareAgentConfig.from_env()`、`McpStdioClient._spawn()` 用参数数组启动，stderr 独立线程 |
| 2.2 initialize → initialized → tools/list，缓存清单 | `McpStdioClient._handshake()`；`to_openai_tools()` 原样透传 `name/description/inputSchema` |
| 2.3 工具循环、≤4 轮、system prompt 约束 | `PetAgent._run_tool_loop()`、`SYSTEM_PROMPT` |
| 3.1/3.2/3.3 参数要求 | `PetAgent._build_plan()`：`request_id`/`conversation_id`/`user_request`/`plan_id` 由宿主生成；模型的 `device_id`/`capability`/`params` 原样使用；`execute_commands` 最多 8 项 |
| 4 语音确认策略 | `classify_confirmation()`；计划绑定五个要素，TTL 60 秒；"嗯/好吧"再问一次；换话题即丢弃 |
| 5 双层错误处理 | JSON-RPC 层 `McpProtocolError`；业务层 `parse_tool_result()` + `ERROR_PHRASES`；只有 `succeeded` 才播报完成 |
| 6 进程/超时/重连 | 进程按会话长驻；查询工具在进程退出后重启重试一次；写操作超时抛 `McpCallTimeout(sent=True)`，**不换 request_id 重发** |
| 7 注册/注销走设置页 | `ADMIN_TOOL_NAMES` 在聊天里直接返回 `ADMIN_ONLY` |
| 8 日志与隐私 | 日志只记录 conversation_id / request_id / 工具名 / 状态 / 错误码；`describe()` 只输出环境变量键名；模型上下文中的密钥有单测守护 |

## 6. 验收场景（文档第 9 节）

| 场景 | 期望 | 覆盖用例 |
| --- | --- | --- |
| 普通问题 | 不调用硬件工具 | `test_plain_chat_does_not_touch_hardware` |
| "有哪些设备" | 调用 `list_devices` 并播报真实列表 | `test_list_devices_feeds_result_back_to_model` |
| "打开客厅灯" | 先确认，未确认前无 `execute_command` | `test_write_action_waits_for_confirmation` |
| 明确确认 | 使用真实 `device_id`/`capability`/Schema，成功才播报完成 | 同上 |
| 设备离线 | 播报离线，不声称已打开 | `test_offline_device_never_reported_as_success` |
| 不支持的动作 | 返回能力不支持，不猜替代能力 | `test_hardware_mcp` 中的 `CAPABILITY_NOT_SUPPORTED` |
| ASR 结果重复两次 | 只有一个 `request_id`、一次执行 | `test_repeated_asr_result_executes_only_once` |
| 工具进程被关掉 | 自动重启、重新初始化后完成查询 | `test_query_tool_restarts_dead_process` |
| 用户拒绝确认 | 无写操作 | `test_cancel_discards_plan_without_executing` |
| 切换账号 | 新进程用新身份 | `build_agent_from_env()` 每次按环境变量重建客户端；切号需重建 agent（见第 8 节） |

复跑：

```bash
python3 -m unittest tests.test_hardware_mcp tests.test_hardware_mcp_http tests.test_pet_agent -v
```

## 7. 接入真实插件要做的三件事

1. 把 `HARDWARE_AGENT_MCP_COMMAND` 指向发布包里的 `hardware-agent-mcp.exe`；
2. 用安装环境下的 `HARDWARE_AGENT_TENANT_ID` / `HARDWARE_AGENT_ACTOR_REF` /
   `RK3588_BASE_URL` / `AGENTOS_COMMAND_API_KEY` 注入；
3. 联调前先确认 `list_devices` 返回真机，再走一次 `status.read` 这类无副作用命令，
   最后才试 `record.start` 等真实动作。

## 8. 已知边界

- **切换账号/租户**：MCP 进程按账号隔离。当前做法是重建 `PetAgent`（进程内换客户端），
  宿主层还没有账号体系，等设置页落地时补"结束旧进程 + 新身份重启"的钩子。
- **注册设备**：不在聊天里做，等设置页；聊天里一律返回 `ADMIN_ONLY`。
- **流式**：模型调用目前是整段返回，TTS 仍是整段合成。要压到 1 秒内需要流式
  ASR + 流式 TTS，属于下一阶段，不在本轮范围。
- **本地唤醒**：`esp-sr` / WakeNet 仍按 `AGENT_HANDOFF_2026-10-22.md` 的路线推进，
  本轮不动固件，桌宠照旧通过随身设备接口上传音频。
- **模型能力**：工具循环依赖原生 function calling；若模型只支持文本，需要按文档
  2.3 的备选方案让它输出结构化 JSON 再由宿主解析（尚未实现）。

## 9. 语音闭环：对着桌宠说话，它用喇叭回你

桌宠自己带 ES8311 麦克风和功放，说话和听声都在设备上完成，中枢只做识别和思考。
整条链路：

```text
桌宠麦克风 -> VAD 断句 -> 上传 wav -> 中枢 ASR -> 桌宠大脑
   ^                                                    |
   └────── 下载 /audio 用功放播放 <── 本地 say 合成语音 <──┘
```

### 9.1 启动（一条命令）

```bash
./tools/start_pet_hub.sh
```

脚本等价于：

```bash
python3 run.py --host 0.0.0.0 --port 18787 --demo-agent --local-tts
```

三处默认值都是为了让桌宠一张嘴就能用：

- `--host 0.0.0.0`：固件指向 `http://192.168.0.107:18787`，只听 127.0.0.1 会连不上；
- `--local-tts`：RK3588 的 TTS 不在线时，用本机 `say` 合成回复语音，不再依赖那台机器；
- `--demo-agent`：没配模型时用脚本模型，链路照通；配了 `PET_AGENT_CHAT_BASE_URL`
  和 `PET_AGENT_CHAT_MODEL` 会自动换成 `--agent` 走真模型。

### 9.2 桌宠侧条件

- 固件要烧 `AUDIO_CAPTURE` 那一版（`audio_capture.cpp`），它才有 HTTP 上传和功放回放；
  默认版 `main.cpp` 只打印芯片信息和 Wi-Fi 扫描，不会联网。
- `wifi_config.h` 里的 `SPARKBOT_HUB_BASE_URL` 指向本机局域网 IP（当前 `192.168.0.107:18787`）。
- 唤醒词看 `portable.py` 的 `_is_wake_phrase`，当前是「贾维斯」及若干同音变体。
  喊中之后中枢返回 `session_action=awake`，固件播「我在」并进入会话。

### 9.3 实测结果（2026-10-26，本机）

用 `tools/simulate_pet_voice.py` 以合成语音代替嘴，走的是和固件一样的四个接口：

| 你说 | 识别 | 回复 | 语音 |
| --- | --- | --- | --- |
| 贾维斯（唤醒） | 贾维斯。 | 我在 | 固件自播 |
| 帮我看看现在有哪些设备 | 帮我看看现在有哪些设备。 | 现在的设备有：客厅灯、录音豆、卧室插座、厨房温湿度计。 | 169 KB wav |
| 把客厅灯打开 | 把客厅灯打开。 | 我准备对「客厅灯」执行「开关灯」，要执行吗？ | 124 KB wav |
| 执行 | 执行。 | 已经完成：客厅灯的「开关灯」。 | 100 KB wav |
| 今天天气怎么样 | 今天天气怎么样？ | 我在。现在还没接上大模型…… | 275 KB wav |

ASR 是局域网 Qwen3-ASR-1.7B（`192.168.0.153:18005`），单句约 0.7 秒。

### 9.4 桌宠不在手边时怎么验

```bash
# 合成一句话，走完整链路并播放回复
python3 tools/simulate_pet_voice.py --text "帮我看看现在有哪些设备" --play

# 用手机录的音频
python3 tools/simulate_pet_voice.py --file ~/Desktop/say.m4a --play

# 两轮确认流程（conversation 要一致）
python3 tools/simulate_pet_voice.py --text "把客厅灯打开" --conversation c-1
python3 tools/simulate_pet_voice.py --text "执行" --conversation c-1 --play

# 只验唤醒词
python3 tools/simulate_pet_voice.py --text "贾维斯" --wake
```

脚本默认加 `--base-url http://127.0.0.1:18787`，验局域网路径时传
`--base-url http://192.168.0.107:18787`。

### 9.5 现在的限制

- 脚本模型的回复是固定的，能证明「说话有回应、声音出得来」，但答不了开放式问题；
  要真对话得配模型端点。
- 唤醒和断句在固件里是 VAD + 文本唤醒词，不是 `esp-sr` / WakeNet 的本地唤醒，
  安静环境下可用，有底噪时误触发会偏多。

