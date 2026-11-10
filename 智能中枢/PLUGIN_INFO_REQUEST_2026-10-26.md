# 硬件插件接入 · 信息请求单

**发送方**：桌宠主控（硬件宿主侧）
**接收方**：智能硬件体插件团队
**版本**：v1.0 · 2026-10-26
**用途**：宿主侧的 MCP 客户端已按贵方 2026-10-26 回复实现完毕，现在只缺运行参数和首次注册信息。本文是一份填空单，填完即可联调。

---

## 0. 给整理方（Agent）的指令

如果你是被要求"整理"这份文件的一方，请按以下规则处理：

1. 只回答第 2 节（`2.1` – `2.6`）里的 `【待填】` 项，其余内容不要改写。
2. 无法回答的项，填 `unknown`，并在同一行的 `备注` 里写清"谁可以提供"或"什么时候能给"。**不要留空，也不要猜测。**
3. 如果某一项与我方在第 3 节的实现规格冲突，在该项末尾追加 `【冲突：说明】`，不要直接按你的理解改掉。
4. 输出格式照第 4 节的模板，一项一行，不要合并。
5. **密钥类信息（API Key、令牌）不要写进本文档**，另行通过安全渠道传递，只在本文件里标注"已另发"。

---

## 1. 背景（一段话）

桌宠是 ESP32-S3 的语音终端，用户对它说话 → 音频上传宿主 → 宿主做语音识别 → 调贵方插件的 MCP 工具 → 合成语音回传给桌宠喇叭。

宿主侧是 Python，用 **stdio 子进程 + 行分隔 JSON-RPC** 拉起插件。按贵方回复，`.exe` 只是启动器，真正逻辑在 `payload/mcp_server.py`（纯标准库），因此宿主侧计划在 macOS arm64 上直接 `python3 payload/mcp_server.py`，不经过 Windows、不使用 Wine。

本方已完成：MCP 客户端（握手、工具发现、双层错误解析、幂等、超时策略）、工具调用循环、写操作二次确认、语音合成与回传。**尚未做真插件联调，因为没有发布包和连接参数。**

---

## 2. 需要贵方提供的信息

### 2.1 发布包

**Q1. 发布包获取方式**
`【待填】`（下载链接 / 网盘 / 直接附件 / 内网路径）

**Q2. 包文件名与大小**
`【待填】` 例如 `hardware-agent-deepseek-harness-0.1.1-windows.zip`

**Q3. SHA-256 是否仍为下列值**
`00ADA89D6E9083D63FABB70516701CF454B68E27A8B79D91DCAC2C120EE53181`
`【待填】`（一致 / 不一致，若不一致请给新值）

**Q4. 是否可以只提供 `payload/` 目录**
我们只需要 `payload/`，不需要 `.exe`。`【待填】`（可以 / 必须整包）

### 2.2 运行环境（macOS）

**Q5. `payload/mcp_server.py` 的 Python 最低版本**
`【待填】` 例如 `3.10`

**Q6. 是否零第三方依赖（纯标准库）**
`【待填】`（是 / 否，若否则给出 `requirements.txt`）

**Q7. 在 macOS arm64 上直接跑 `mcp_server.py`，贵方是否认可**
贵方原回复标注"Mac 实机未验收"。我们需要知道：是"没测过但理论上可以"，还是"已知有问题"。
`【待填】`（理论可行 / 有已知问题 / 需要额外适配）`【备注：】`

**Q8. `mcp_server.py` 是否依赖工作目录**
例如是否会按相对路径读写配置文件、设备包、日志。
`【待填】`（无要求 / 必须在包根目录 / 必须在 payload/ 下）

### 2.3 连接与身份参数

宿主通过**进程环境变量**注入下列值。请给出可用于联调的真实值（密钥另发）。

**Q9. `RK3588_BASE_URL`**
贵方示例为 `192.168.0.140:8099`。请确认联调环境的真实地址（含协议与端口）。
`【待填】`

**Q10. `HARDWARE_AGENT_TENANT_ID`**
`【待填】`

**Q11. `HARDWARE_AGENT_ACTOR_REF`**
`【待填】`

**Q12. `AGENTOS_COMMAND_API_KEY`**
`【待填：已另发 / 尚未提供】` —— **不要把密钥明文写在这里**

**Q13. `RK3588_HTTP_TIMEOUT` 的建议值**
我们默认 `15` 秒，单次工具调用超时据此推算。若贵方实际下发链路更慢，请给出建议值。
`【待填】` 例如 `30`

**Q14. `VHA_DATA_DIR` 能否指向任意可写目录**
这个目录存幂等记录（同 `request_id` 不重复执行），需要持久化。我们默认 `~/.smart-hub/hardware-agent`。
`【待填】`（可以 / 必须固定路径：xxx）

**Q15. 同一台机器上能否同时运行多个插件实例**
例如两个 `tenant` 并行。我们当前是**单进程、串行调用**。
`【待填】`（支持 / 不支持，只能单实例）

### 2.4 首次注册设备（真机联调前置）

贵方说明"没有虚拟设备、没有测试租户"，因此首次联调必须先注册真机。

**Q16. 设备包的形式**
`【待填】`（JSON / YAML / ZIP / 由贵方系统下发 / 其他）

**Q17. 设备包从哪里获取**
`【待填】`

**Q18. `hardware_agent_register_device` 的完整入参**
请按 `tools/list` 里的 `inputSchema` 原样贴出，或给出一段真实请求样例。
`【待填】`

**Q19. `hardware_agent_verify_device` 的完整入参**
`【待填】`

**Q20. 可用于联调的真机**
请指定一台，并给出它的 `device_id` 和 `capabilities` 列表（例如 `light.power.set`、`plug.power.set`）。
`【待填】`

**Q21. 注册后多久可被 `list_devices` 看到**
`【待填】`（立即 / 需要等待 N 秒 / 需要重新 verify）

### 2.5 执行语义（影响用户听到的话）

这几项决定桌宠在"不确定"时该怎么说，请尽量明确。

**Q22. `execute_command` 是同步等结果，还是先返回中间态再轮询**
贵方回复里提到 `queued` / `executing` 状态。宿主是同步调用，一次调用最多等 60 秒。
`【待填】`（同步返回终态 / 可能返回中间态，需要再次查询：用哪个工具）

**Q23. 典型一次开灯的耗时**
`【待填】` 例如 `1–2 秒`

**Q24. 返回 `timed_out` 时，设备端实际会不会执行**
这决定我们是说"确认没生效"还是"不确定是否生效"。
`【待填】`（一定不执行 / 可能已执行 / 不确定）

**Q25. `request_id` 由宿主生成，长度 ≤120，格式是否有额外限制**
例如是否必须是 UUID、是否区分大小写、是否允许前缀。
`【待填】`（无限制 / 必须 UUID / 其他：xxx）

**Q26. 幂等记录保存多久、存在哪里**
贵方回复称"不过期、落 `VHA_DATA_DIR`"。请确认清空该目录的后果（是否会导致重复执行）。
`【待填】`

### 2.6 对话模型（决定"问什么答什么"）

**这是本次最关键的一组问题。** 桌宠要能自然对话，需要支持 function calling 的模型。贵方交付物名为 `hardware-agent-deepseek-harness`，我们需要确认它的定位。

**Q27. 插件内部是否自带对话模型 / LLM harness**
`【待填】`（自带，对外只暴露工具 / 不自带，只做执行层 / 两者都支持）

**Q28. 如果自带，宿主要如何使用**
是通过某个 MCP 工具把自然语言交给它，还是仍由宿主自己调模型、只调执行类工具？
`【待填】`

**Q29. 贵方是否有可共用的 OpenAI 兼容端点**（`/chat/completions` + `tools`）
若有，请给 `base_url`、`model` 名称、以及密钥获取方式（密钥另发）。
`【待填】`（有 / 无 / 可申请）

**Q30. 贵方联调时用的是哪个模型**
`【待填】` 例如 `deepseek-chat` / `qwen2.5-7b-instruct`

---

## 3. 我方实现规格（供核对，不需要填，有差异请标注）

以下是宿主侧**已经写进代码**的行为。若与贵方实现不一致，请在该条后加 `【冲突：说明】`，我们改代码，不要求贵方迁就。

**P1. 传输**
stdio，stdin/stdout 各一行一个 JSON-RPC 2.0 消息，UTF-8，`\n` 分隔。stderr 单独线程收取并落日志文件，绝不混入协议流。

**P2. 握手序列**
`initialize`（`protocolVersion: "2025-03-26"`，带 `clientInfo`）→ 等服务端返回 → `notifications/initialized` → `tools/list`。握手超时 5 秒。

**P3. 协议版本协商**
优先 `2025-03-26`；若服务端返回更低版本（如 `2024-11-05`），以服务端返回值为准，不重试。

**P4. 探活**
用 MCP 的 `ping` 方法，不调用任何 HTTP `/health`。

**P5. 工具清单**
`tools/list` 返回值原样转成模型的 `tools` 定义，宿主代码里**没有第二份固定能力表**。`listChanged` 按 `false` 处理，不监听变更通知。

**P6. 结果判定**
只解析 `result.content[0].text` 的 JSON。**以内层 `status` 为准，不看 `isError`**：只有 `succeeded` 视为成功；`queued` / `executing` / `pending` / `timed_out` / 缺失 一律视为"未完成"，不向用户说"已完成"。

**P7. 错误分层**
- JSON-RPC 层：读 `error.code` 与 `error.data.error_code`（例如 `-32000` + `INVALID_PARAMS`），翻译成用户可理解的话。
- 业务层：读 `content[0].text` 里的 `error_code`，按贵方 2.4 全表映射话术（含 `DEVICE_TIMEOUT`，`COMMAND_TIMEOUT` 作旧码兼容）。

**P8. 幂等**
`request_id` 由宿主生成。超时或传输失败时**复用同一个 `request_id` 重试，绝不换新 ID**。不做失败换号重发。

**P9. 字段限长**（按贵方 `inputSchema` 截断）
`request_id` ≤120、`plan_id` ≤120、`conversation_id` ≤120、`user_request` ≤2000。

**P10. 写操作与查询操作区别对待**
`execute_command(s)`：必须用户语音确认后才发；超时后不自动重试。
`list_devices` / `get_device_status` / `verify_device`：只读，进程崩溃后允许在新进程上重试一次。

**P11. 管理员工具不在聊天里触发**
`register_device` / `unregister_device` 由独立命令行脚本调用，模型的工具清单里对这两个工具直接拒绝执行，避免模型擅自注册设备。

**P12. 会话与鉴权**
不使用 `Mcp-Session-Id`，不使用 SSE，不带任何 HTTP 鉴权头（stdio 模式）。身份只经进程环境变量注入，不写日志、不进模型上下文。

**P13. 调用模型**
单进程、串行调用，同一时刻只有一个 `tools/call` 在途。

---

## 4. 回填格式（请照此输出）

```yaml
# 插件接入信息回填
meta:
  package_name: ""
  sha256: ""
  sha256_matches: true|false
  python_min: ""
  macos_arm64_ok: ""          # 理论可行 / 有已知问题 / 需要额外适配
  working_dir_required: ""    # 无要求 / 必须在包根目录 / 必须在 payload/ 下
  zero_dependency: true|false

connection:
  rk3588_base_url: ""
  tenant_id: ""
  actor_ref: ""
  api_key_delivered: ""       # 已另发 / 尚未提供（不要写明文）
  http_timeout_suggest: ""
  data_dir: ""                # 任意可写 / 固定路径:xxx
  multi_instance: true|false

device:
  package_format: ""
  package_source: ""
  register_device_args: ""    # inputSchema 或真实样例
  verify_device_args: ""
  test_device_id: ""
  capabilities: []
  visible_after_register: ""  # 立即 / 需要等待N秒 / 需要重新verify

execution:
  sync_or_async: ""           # 同步返回终态 / 可能返回中间态
  typical_latency: ""
  timed_out_effect: ""        # 一定不执行 / 可能已执行 / 不确定
  request_id_constraint: ""   # 无限制 / 必须UUID / 其他
  idempotency_retention: ""

model:
  plugin_has_llm: ""          # 自带 / 不自带 / 两者都支持
  how_to_use_if_bundled: ""
  openai_compatible_endpoint: ""   # 有 / 无 / 可申请
  base_url: ""
  model_name: ""
  model_used_in_testing: ""

conflicts: []                 # 每条格式："P6：说明xxx"
unanswered: []                # 无法回答的项，格式："Q24：由某某提供，预计x日"
```

填好后回传本文件即可。

---

## 5. 拿到回填后的操作（我方执行，仅供知悉）

1. `python3 tools/prepare_plugin.py --zip <发布包>` —— 校验哈希、解压、定位入口、打印可复制命令
2. 按回填值配好环境变量
3. `python3 tools/check_plugin.py` —— 一次跑完：平台检查 / 身份变量 / 进程拉起 / ping / 工具清单 / 设备列表
4. 用独立入口（不经聊天）执行首次 `register_device` + `verify_device`
5. 启动宿主，端到端验证三轮：「有哪些设备」→「把客厅灯打开」（只播报确认）→「执行」（真正下发）
6. 若第 3 步就失败，会把插件自身 stderr 贴回给贵方，不需要贵方登机器排查

---

## 6. 三个已知风险（供贵方判断优先级）

1. **macOS 未验收**：若 `mcp_server.py` 在 arm64 上有编码或路径问题，我们会在第 3 步暴露出来，届时需要贵方支持或提供 Linux 包。
2. **`timed_out` 语义模糊**：若设备实际已执行但返回超时，用户会被告知"不确定"，体验受损。Q24 请务必明确。
3. **对话模型归属未定**：若贵方插件不自带 LLM，我们需要单独准备一个支持 function calling 的端点，这是"问什么答什么"的前提条件。
