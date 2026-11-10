# RK3588 命令 API · 路由与报文请求单

**发送方**：桌宠主控（语音宿主侧）
**接收方**：RK3588 / AgentOS 硬件服务提供方
**版本**：v1.0 · 2026-10-26
**用途**：地址、租户、测试设备、API Key 都已收到，鉴权已验证通过。**现在只差路由清单和报文结构**，拿到即可端到端联调。

---

## 0. 给整理方（Agent）的指令

1. 只回答第 3 节的 `【待填】` 项，不要改写第 1、2 节。
2. 无法回答的填 `unknown`，并在同一行 `备注` 写清"谁可以提供"。**不要留空，不要猜测路径。**
3. **路径务必逐字照抄**（含大小写、连字符、是否带尾部斜杠）。这个服务是 `http.server` 硬编码精确匹配，少一个字符就是 404，没有容错。
4. 密钥不要写进本文档，标注"已另发"即可。
5. 最小可用材料是 **第 4 节的三条 curl**。只要这三条能跑通，其余可以边联调边补。

---

## 1. 背景（一段话）

桌宠是 ESP32-S3 语音终端：用户说话 → 音频经串口上传到 Mac 宿主 → 宿主做 ASR → 需要调用 RK3588 的命令 API 去操作真实设备（本次是 `bean_ye_1` 录音豆，能力 `record.start` / `record.stop` / `status.read`）→ 结果再合成语音回传给桌宠喇叭。

我方已完成：语音采集与判停、ASR、TTS、串口回传、会话状态机。**现在卡在"不知道往哪个 URL 发命令"这一步。**

已收到的参数（无需再给）：

| 项 | 值 |
| --- | --- |
| 服务地址 | `http://192.168.0.140:8099` |
| 租户 | `tenant-a` |
| 测试设备 | `bean_ye_1` |
| 设备能力 | `record.start`、`record.stop`、`status.read` |
| 命令 API Key | 已另发（走安全渠道） |

---

## 2. 我方已自行确认的事实（请勿重复回答）

为避免来回，下列结论已由我方实测得出：

1. **服务存活**，监听 `192.168.0.140:8099`，走 HTTP/1.0。
2. **鉴权格式是 `Authorization: Bearer <key>`**。不带该头返回 `401 {"status":"failed","error_code":"UNAUTHORIZED","message":"Bearer token is invalid"}`；带上之后 401 消失，说明 **Key 有效、鉴权通过**。
3. **服务不是框架**：`Server: BaseHTTP/0.6 Python/3.10.12`，路由是手写精确匹配，没有 FastAPI/Django 那套自动文档。
4. **不存在 `/openapi.json`、`/docs`、`/health`、`/ping`、`/api`、`/v1`**，这些路径一律 404。
5. 未匹配路径统一返回 `404 {"status":"failed","error_code":"NOT_FOUND","message":"route not found"}`，**方法不匹配也是 404**（不是 405），所以无法通过报错区分"路径对了但方法错了"。
6. `OPTIONS` 返回 `501 Unsupported method`，即没有 CORS 预检。

我方已盲试且**全部 404** 的路径（供你们反向确认真实命名）：

```
GET/POST  /api/v1/devices/bean_ye_1/commands
GET/POST  /api/v1/commands
GET/POST  /v1/commands
GET       /api/v1/devices
GET       /api/v1/devices/bean_ye_1
GET       /api/v1/devices/bean_ye_1/status
GET/POST  /api/v1/tenants/tenant-a/devices/bean_ye_1/commands
GET       /devices  /devices/bean_ye_1  /devices/bean_ye_1/status
GET/POST  /commands  /command
GET       /v1/devices  /v1/status  /status
GET       /agent/v1/commands  /api/v1/agent/commands  /agent  /agentos
POST 的请求体用的是 {"capability":"status.read","params":{}}
```

---

## 3. 需要贵方提供的信息

### 3.1 路由清单（最核心）

**Q1. 列出设备**
完整路径 + 方法。`【待填】`

**Q2. 读取设备状态（`status.read`）**
完整路径 + 方法。`【待填】`

**Q3. 下发命令（`record.start` / `record.stop`）**
完整路径 + 方法。`【待填】`

**Q4. 查询命令结果**
完整路径 + 方法。`【待填】`

**Q5. 取消命令（如有）**
`【待填】`（无则填 `none`）

### 3.2 报文结构

**Q6. 下发命令的请求体**
请给一份 `record.start` 的**完整** JSON 示例（含所有必填字段）。`【待填】`

**Q7. 设备标识放哪里**
`device_id` 是放在 URL 路径里、请求体里，还是两者都有？`【待填】`

**Q8. 能力名放哪里**
`capability` 是路径段、请求体字段，还是用工具名区分（例如 `/commands/record.start`）？`【待填】`

**Q9. 租户怎么传**
走请求头（哪个头名）、URL 路径，还是请求体？我方目前只在试 `X-Tenant-ID`。`【待填】`

### 3.3 身份与幂等

**Q10. 是否需要 `actor_ref` / `request_id` / `conversation_id`**
如果要，字段名、是否必填、长度上限。`【待填】`

**Q11. 幂等策略**
同 `request_id` 同参数是否返回原结果？不同参数是否报 `DUPLICATE_REQUEST`？`【待填】`

### 3.4 返回与超时

**Q12. 下发命令的即时响应**
是直接返回最终结果，还是返回 `command_id` 让我方轮询？`【待填】`

**Q13. 建议超时时间**
`record.start` 从下发到设备 ACK，正常多少秒？超时后的错误码是什么？`【待填】`

**Q14. 成功响应样例**
请给一份 `status.read` 和一份 `record.start` 的**真实**响应 JSON（脱敏可）。`【待填】`

### 3.5 错误码

**Q15. 错误码全集**
我方已知 `UNAUTHORIZED` / `NOT_FOUND`，还需要：参数非法、设备离线、设备超时、队列满、重复请求、下游不可用。`【待填】`

---

## 4. 最小可用材料（如果只能给一样）

如果整理时间有限，**只给下面这三条 curl 就够了**，我方自己反推报文：

```bash
# 1) 列出设备
curl -i -H "Authorization: Bearer <KEY>" \
  http://192.168.0.140:8099<按Q1填>

# 2) 读状态（无副作用，先跑这条）
curl -i -X POST -H "Authorization: Bearer <KEY>" -H "Content-Type: application/json" \
  -d '<按Q6填>' \
  http://192.168.0.140:8099<按Q2填>

# 3) 开始录音（有副作用，最后再跑）
curl -i -X POST -H "Authorization: Bearer <KEY>" -H "Content-Type: application/json" \
  -d '<按Q6填>' \
  http://192.168.0.140:8099<按Q3填>
```

> 请在贵方环境里**实际跑一遍**再贴回来，确保返回 200 而不是 404。这是唯一能排除"文档和实现不一致"的办法。

---

## 5. 我方拿到后的联调顺序

1. `status.read` 跑通（确认路由、鉴权、租户三者对齐）；
2. `record.start` → 观察录音豆是否真的开始录 → `record.stop`；
3. 把这条链路接进桌宠语音：**用户说"开始录音" → 桌宠复述确认 → 用户确认 → 下发**，只在收到成功时播报"已开始"；
4. 补测异常：设备离线、超时、重复请求、参数非法。

我方承诺：**在拿到真实响应样例之前，不会把"规划成功"当成"设备已动作"播报给用户。**

---

## 6. 回填模板

```
Q1  路由-列设备        ：
Q2  路由-读状态        ：
Q3  路由-下发命令      ：
Q4  路由-查结果        ：
Q5  路由-取消          ：
Q6  record.start 请求体：
Q7  device_id 位置     ：
Q8  capability 位置    ：
Q9  租户传递方式        ：
Q10 身份字段           ：
Q11 幂等策略           ：
Q12 即时响应形态        ：
Q13 建议超时/超时码     ：
Q14 成功响应样例        ：
Q15 错误码全集         ：
```
