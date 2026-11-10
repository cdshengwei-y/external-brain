from __future__ import annotations

import json
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from typing import Any, Dict, Optional

from .errors import HubError, ValidationError
from .models import TERMINAL_COMMAND_STATES
from agent_gateway.device import VOLUME_GAINS
from agent_gateway import host_control
from agent_gateway.portable import PortableRequestService
from .service import HubService


# 桌宠本机控制页：按钮 = 语音里喊的同一批能力，动作直接在这台 Mac 上执行。
CONTROL_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>桌宠控制台</title>
<style>
  :root {
    --bg1:#0a0e1a; --bg2:#141b2e; --card:rgba(30,40,64,.55); --card-hover:rgba(48,64,104,.75);
    --line:rgba(120,150,220,.14); --text:#eef2fb; --dim:#8a94ab; --accent:#5b8cff; --ok:#4ade80; --fail:#fb7185;
  }
  * { box-sizing:border-box; }
  body {
    background: radial-gradient(1200px 700px at 20% -10%, #1b2444 0%, transparent 55%),
                radial-gradient(900px 600px at 110% 10%, #1d2a52 0%, transparent 50%),
                linear-gradient(160deg, var(--bg1), var(--bg2));
    background-attachment: fixed;
    color:var(--text); font-family:-apple-system,"PingFang SC",sans-serif;
    margin:0; padding:28px 20px 60px; max-width:760px; margin-inline:auto; min-height:100vh;
  }
  header { display:flex; align-items:center; gap:12px; margin-bottom:6px; }
  header .logo { font-size:30px; }
  h1 { font-size:21px; margin:0; letter-spacing:.5px; }
  .status { display:inline-flex; align-items:center; gap:6px; font-size:12px; color:var(--dim);
            background:var(--card); border:1px solid var(--line); padding:4px 12px; border-radius:99px; }
  .dot { width:8px; height:8px; border-radius:50%; background:var(--ok); box-shadow:0 0 8px var(--ok); }
  .dot.off { background:var(--fail); box-shadow:0 0 8px var(--fail); }
  .sub { color:var(--dim); font-size:13px; margin:6px 0 22px; line-height:1.6; }
  .sub b { color:#b9c6e8; font-weight:500; }
  .mode { display:inline-flex; background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:4px; gap:4px; margin-bottom:16px; }
  .mode button { background:transparent; color:var(--dim); border:none; border-radius:9px;
                 padding:8px 20px; font-size:14px; cursor:pointer; transition:.15s; }
  .mode button.on { background:var(--accent); color:#fff; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(96px,1fr)); gap:12px; margin-bottom:20px; }
  .card {
    background:var(--card); border:1px solid var(--line); border-radius:16px;
    padding:16px 6px 12px; text-align:center; cursor:pointer; user-select:none;
    backdrop-filter: blur(10px); transition:transform .15s, background .15s, border-color .15s;
  }
  .card:hover { transform:translateY(-3px); background:var(--card-hover); border-color:rgba(120,150,220,.35); }
  .card:active { transform:translateY(0) scale(.97); }
  .card .icon { font-size:26px; margin-bottom:6px; }
  .card .name { font-size:13px; color:#c9d3e8; }
  .panel { background:var(--card); border:1px solid var(--line); border-radius:16px; padding:14px;
           backdrop-filter: blur(10px); margin-bottom:22px; }
  .row { display:flex; gap:8px; }
  input { flex:1; background:rgba(10,14,26,.6); color:var(--text); border:1px solid var(--line);
          border-radius:10px; padding:11px 14px; font-size:15px; outline:none; }
  input:focus { border-color:var(--accent); }
  .btn { background:var(--accent); color:#fff; border:none; border-radius:10px; padding:11px 20px;
         font-size:14px; cursor:pointer; transition:.15s; }
  .btn.ghost { background:rgba(90,110,160,.25); }
  .btn:active { transform:scale(.97); }
  #toast { position:fixed; left:50%; bottom:36px; transform:translateX(-50%) translateY(20px);
           background:#1d2740; color:var(--text); border:1px solid var(--line); border-radius:12px;
           padding:12px 22px; font-size:14px; opacity:0; pointer-events:none; transition:.25s;
           box-shadow:0 10px 30px rgba(0,0,0,.45); max-width:86vw; }
  #toast.show { opacity:1; transform:translateX(-50%) translateY(0); }
  #toast.ok { border-color:rgba(74,222,128,.4); } #toast.fail { border-color:rgba(251,113,133,.45); }
  h2 { font-size:15px; color:#b9c6e8; margin:0 0 10px; font-weight:500; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  td, th { text-align:left; padding:8px 10px; border-bottom:1px solid rgba(120,150,220,.08); color:#aeb9d2; }
  th { color:var(--dim); font-weight:400; font-size:12px; }
  td.ok { color:var(--ok); } td.fail { color:var(--fail); }
  .hint { color:#69748c; font-size:12px; margin-top:18px; line-height:1.8; }
  .empty { color:var(--dim); font-size:13px; padding:14px 10px; }
</style>
</head>
<body>
<header>
  <div class="logo">🐾</div>
  <h1>桌宠控制台</h1>
  <span class="status" style="margin-left:auto"><span class="dot" id="dot"></span><span id="statusText">检查中枢…</span></span>
</header>
<div class="sub">这台 Mac 的应用开关 · <b>和对着桌宠说「打开WPS」走同一条指令</b>，点卡片立即生效</div>
<div class="mode" id="mode">
  <button class="on" data-m="open">🔓 打开</button>
  <button data-m="quit">🔒 退出</button>
</div>
<div class="grid" id="apps"></div>
<div class="panel">
  <div class="row">
    <input id="target" placeholder="输入应用名或网址：WPS / 微信 / bilibili.com" onkeydown="if(event.key==='Enter')go()">
    <button class="btn" onclick="go()">执行</button>
  </div>
</div>
<h2>最近动作</h2>
<div class="panel"><table id="log"></table></div>
<div class="hint">
  语音同样可用：对桌宠说「打开WPS」「打开微信」「打开bilibili.com」「退出微信」——
  白名单里的应用走本地快通道，说完 1 秒内就动手。<br>
  新增应用喊法：编辑 <span style="color:#8fa3d0">agent_gateway/host_control.py</span> 的 APP_ALIASES 后重启中枢。
</div>
<div id="toast"></div>
<script>
const APP_ICONS = {"WPS":"📄","微信":"💬","企业微信":"💼","QQ":"🐧","谷歌浏览器":"🌐","Safari":"🧭",
  "VS Code":"💻","计算器":"🧮","备忘录":"📝","飞书":"🐦","百度网盘":"☁️","ChatGPT":"🤖",
  "Docker":"🐳","Obsidian":"📓"};
const APPS = Object.keys(APP_ICONS);
let mode = "open";
const grid = document.getElementById('apps');
APPS.forEach(name => {
  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML = `<div class="icon">${APP_ICONS[name]}</div><div class="name">${name}</div>`;
  card.onclick = () => exec(mode === "open" ? "open_app" : "quit_app", name);
  grid.appendChild(card);
});
document.getElementById('mode').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  mode = b.dataset.m;
  document.querySelectorAll('.mode button').forEach(x => x.classList.toggle('on', x === b));
});
let toastTimer;
function toast(msg, ok) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = 'show ' + (ok ? 'ok' : 'fail');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.className = '', 2600);
}
async function exec(action, target) {
  try {
    const res = await fetch('/api/v1/host/exec', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({action, target})
    });
    const data = await res.json();
    toast(data.message || JSON.stringify(data), data.ok);
    loadLog();
  } catch (e) { toast('请求失败：' + e, false); }
}
function go() {
  const t = document.getElementById('target').value.trim();
  if (!t) { toast('先输入应用名或网址', false); return; }
  exec(mode === "open" ? "open_app" : "quit_app", t);
}
async function loadLog() {
  try {
    const res = await fetch('/api/v1/host/log');
    const data = await res.json();
    const table = document.getElementById('log');
    const rows = (data.actions || []).slice().reverse();
    if (!rows.length) { table.innerHTML = '<tr><td class="empty">还没有动作记录，点上面的卡片试试</td></tr>'; return; }
    table.innerHTML = '<tr><th>时间</th><th>动作</th><th>目标</th><th>结果</th></tr>';
    rows.forEach(a => {
      const row = table.insertRow();
      row.insertCell().textContent = (a.ts || '').slice(5);
      row.insertCell().textContent = a.action === 'open_app' ? '打开' : a.action === 'quit_app' ? '退出' : '网址';
      row.insertCell().textContent = a.target || '';
      const c = row.insertCell(); c.textContent = a.ok ? '成功' : '失败'; c.className = a.ok ? 'ok' : 'fail';
    });
  } catch (e) {}
}
async function checkHealth() {
  const dot = document.getElementById('dot'), text = document.getElementById('statusText');
  try {
    const res = await fetch('/api/v1/health');
    dot.className = 'dot'; text.textContent = '中枢在线';
    dot.style.background = 'var(--ok)'; dot.style.boxShadow = '0 0 8px var(--ok)';
  } catch (e) {
    dot.className = 'dot off'; text.textContent = '中枢离线';
    dot.style.background = 'var(--fail)'; dot.style.boxShadow = '0 0 8px var(--fail)';
  }
}
loadLog(); checkHealth();
setInterval(checkHealth, 10000);
</script>
</body>
</html>
"""


def _pcm_wav_header(data_bytes: int, rate: int = 16000) -> bytes:
    """生成 16k / 单声道 / 16bit 的 44 字节 WAV 头。"""
    import struct

    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_bytes,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        rate,
        rate * 2,
        2,
        16,
        b"data",
        data_bytes,
    )


class HubRequestHandler(BaseHTTPRequestHandler):
    server_version = "SmartHub/0.1"

    @property
    def service(self) -> HubService:
        return self.server.hub_service  # type: ignore[attr-defined]

    @property
    def portable_service(self) -> PortableRequestService:
        return self.server.portable_service  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        try:
            if parsed.path == "/api/v1/health":
                self._send_json(200, self.service.health())
                return
            if parsed.path == "/control":
                self._send_html(200, CONTROL_PAGE_HTML)
                return
            if parsed.path == "/api/v1/host/log":
                self._send_json(200, {"actions": host_control.recent(50)})
                return
            if parsed.path.startswith("/api/v1/portable/v1/requests/") and parsed.path.endswith("/audio"):
                request_id = parsed.path.rstrip("/").split("/")[-2]
                audio, content_type = self.portable_service.get_audio(request_id)
                self._send_binary(200, audio, content_type)
                return
            if parsed.path.startswith("/api/v1/portable/v1/requests/"):
                request_id = parsed.path.rstrip("/").split("/")[-1]
                self._send_json(200, self.portable_service.get(request_id))
                return
            if (
                parsed.path.startswith("/api/v1/portable/v1/music/")
                and parsed.path.endswith("/stream")
            ):
                track_id = parsed.path.rstrip("/").split("/")[-2]
                self._stream_music(track_id)
                return
            if parsed.path.startswith("/api/v1/portable/v1/devices/"):
                device_id = parsed.path.rstrip("/").split("/")[-1]
                self._send_json(200, self._portable_device_view(device_id))
                return
            if parsed.path == "/api/v1/devices":
                self._send_json(200, {"devices": self.service.list_devices()})
                return
            if parsed.path.startswith("/api/v1/devices/") and parsed.path.endswith("/state"):
                device_id = parsed.path.strip("/").split("/")[-2]
                self._send_json(200, self.service.get_device_state(device_id))
                return
            if parsed.path.startswith("/api/v1/devices/"):
                device_id = parsed.path.split("/")[-1]
                self._send_json(200, self.service.get_device(device_id))
                return
            if parsed.path.startswith("/api/v1/commands/") and parsed.path.endswith("/wait"):
                parts = parsed.path.strip("/").split("/")
                command_id = parts[-2]
                query = parse_qs(parsed.query)
                timeout_ms = self._query_int(query, "timeout_ms", 30000)
                if timeout_ms < 0 or timeout_ms > 60000:
                    raise ValidationError("timeout_ms 必须在 0 到 60000 之间")
                command = self.service.wait_for_command(command_id, timeout_ms / 1000)
                completed = command["state"] in TERMINAL_COMMAND_STATES
                self._send_json(
                    200,
                    {
                        "command": command,
                        "completed": completed,
                        "timed_out": not completed,
                    },
                )
                return
            if parsed.path.startswith("/api/v1/commands/"):
                command_id = parsed.path.split("/")[-1]
                self._send_json(200, self.service.get_command(command_id))
                return
            if parsed.path == "/api/v1/events":
                query = parse_qs(parsed.query)
                since_id = self._query_int(query, "since_id", 0)
                limit = self._query_int(query, "limit", 100)
                if "text/event-stream" in self.headers.get("Accept", ""):
                    self._stream_events(since_id)
                else:
                    self._send_json(
                        200,
                        {"events": self.service.list_events(since_id, limit)},
                    )
                return
            self._send_error_json(404, "接口不存在")
        except HubError as error:
            self._send_error_json(error.status_code, error.message)
        except Exception as error:  # pragma: no cover - defensive HTTP boundary
            self._send_error_json(500, f"中枢内部错误: {error}")

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        try:
            if parsed.path == "/api/v1/host/exec":
                payload = self._read_json_body()
                action = str(payload.get("action") or "").strip()
                target = str(payload.get("target") or "").strip()
                if not action or not target:
                    raise ValidationError("action 和 target 都不能为空")
                self._send_json(200, host_control.execute(action, target))
                return
            if parsed.path == "/api/v1/portable/v1/media":
                data = self._read_binary_body()
                media = self.portable_service.upload_media(
                    portable_device_id=self.headers.get("X-Portable-Device-Id", ""),
                    media_type=self.headers.get("X-Media-Type", ""),
                    audio_format=self.headers.get("X-Audio-Format"),
                    content_type=self.headers.get("Content-Type"),
                    data=data,
                )
                self._send_json(201, media)
                return
            payload = self._read_json_body()
            if parsed.path == "/api/v1/portable/v1/requests":
                request = self.portable_service.submit(payload)
                self._send_json(202, request)
                return
            if (
                parsed.path.startswith("/api/v1/portable/v1/requests/")
                and parsed.path.endswith("/confirm")
            ):
                parts = parsed.path.strip("/").split("/")
                request_id = parts[-2]
                plan_id = payload.get("plan_id")
                if not isinstance(plan_id, str) or not plan_id.strip():
                    raise ValidationError("plan_id 必须是非空字符串")
                request = self.portable_service.confirm(request_id, plan_id.strip())
                self._send_json(202, request)
                return
            if parsed.path.startswith("/api/v1/portable/v1/devices/") and parsed.path.endswith("/volume"):
                device_id = parsed.path.strip("/").split("/")[-2]
                self._send_json(200, self._set_portable_volume(device_id, payload))
                return
            if parsed.path == "/internal/v1/devices/register":
                self._send_json(201, self.service.register_device(payload))
                return
            if parsed.path.startswith("/internal/v1/devices/"):
                self._handle_internal_device_post(parsed.path, payload)
                return
            if parsed.path.startswith("/api/v1/devices/") and parsed.path.endswith("/commands"):
                device_id = parsed.path.split("/")[-2]
                command = self.service.submit_command(device_id, payload)
                self._send_json(202, command)
                return
            if parsed.path.startswith("/api/v1/commands/") and parsed.path.endswith("/cancel"):
                command_id = parsed.path.split("/")[-2]
                self._send_json(202, self.service.cancel_command(command_id))
                return
            self._send_error_json(404, "接口不存在")
        except HubError as error:
            self._send_error_json(error.status_code, error.message)
        except json.JSONDecodeError:
            self._send_error_json(400, "请求体必须是有效 JSON")
        except Exception as error:  # pragma: no cover - defensive HTTP boundary
            self._send_error_json(500, f"中枢内部错误: {error}")

    # ---------------- 桌宠设备影子（说话音量/音乐档位） ----------------

    @property
    def device_registry(self):
        registry = getattr(self.portable_service, "devices", None)
        if registry is None:
            raise HubError("设备影子未启用", status_code=503)
        return registry

    def _portable_device_view(self, device_id: str) -> Dict[str, Any]:
        profile = self.device_registry.get(device_id)
        return {
            "device_id": profile.device_id,
            "volume_step": profile.volume_step + 1,
            "volume_total": len(VOLUME_GAINS),
            "volume_gain": round(profile.volume_gain, 3),
            "volume_label": profile.volume_label,
            "music_step": (
                None if profile.music_step is None else profile.music_step + 1
            ),
            "music": profile.music,
            "screen": profile.screen,
            "fountain": profile.fountain,
            "battery": profile.battery,
            "updated_at": profile.updated_at,
            "summary": profile.summary(),
        }

    def _set_portable_volume(
        self, device_id: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        registry = self.device_registry
        raw_step = payload.get("step", payload.get("volume_step", payload.get("level")))
        if raw_step is not None:
            try:
                step = int(raw_step)
            except (TypeError, ValueError):
                raise ValidationError("step 必须是 1 到 5 的整数")
            if not 1 <= step <= len(VOLUME_GAINS):
                raise ValidationError(f"step 必须在 1 到 {len(VOLUME_GAINS)} 之间")
            registry.set_volume(device_id, step - 1)
        elif payload.get("delta") is not None:
            try:
                delta = int(payload["delta"])
            except (TypeError, ValueError):
                raise ValidationError("delta 必须是整数")
            registry.nudge_volume(device_id, delta)
        else:
            raise ValidationError("需要 step（1-5）或 delta")
        return self._portable_device_view(device_id)

    def _handle_internal_device_post(self, path: str, payload: Dict[str, Any]) -> None:
        parts = path.strip("/").split("/")
        if len(parts) < 5:
            self._send_error_json(404, "接口不存在")
            return
        device_id = parts[3]
        action = parts[4]
        if action == "availability":
            self._send_json(200, self.service.update_availability(device_id, payload))
        elif action == "state":
            self._send_json(202, self.service.ingest_state(device_id, payload))
        elif action == "events":
            self._send_json(202, self.service.ingest_device_event(device_id, payload))
        elif action in {"acks", "command-acks"}:
            command_id = payload.get("command_id")
            if not command_id:
                raise ValidationError("acks 缺少 command_id")
            if payload.get("protocol_version") not in {None, "1.0"}:
                raise ValidationError("acks protocol_version 必须是 1.0")
            if payload.get("device_id") not in {None, device_id}:
                raise ValidationError("acks device_id 与 topic 不匹配")
            if "status" in payload and "state" not in payload:
                payload = {**payload, "state": payload["status"]}
            self._send_json(
                200,
                self.service.acknowledge_command(device_id, command_id, payload),
            )
        else:
            self._send_error_json(404, "接口不存在")

    def _stream_events(self, since_id: int) -> None:
        subscription = self.service.subscribe_events()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            for event in self.service.list_events(since_id, 500):
                self._write_sse(event)
            while True:
                try:
                    event = subscription.queue.get(timeout=15)
                    self._write_sse(event)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.service.unsubscribe_events(subscription)

    def _write_sse(self, event: Dict[str, Any]) -> None:
        body = json.dumps(event, ensure_ascii=False)
        message = f"id: {event['event_id']}\nevent: {event['event_type']}\ndata: {body}\n\n"
        self.wfile.write(message.encode("utf-8"))
        self.wfile.flush()

    def _read_json_body(self) -> Dict[str, Any]:
        raw = self._read_body(1024 * 1024)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return payload

    def _read_binary_body(self) -> bytes:
        return self._read_body(10 * 1024 * 1024)

    def _read_body(self, max_length: int) -> bytes:
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            raise ValidationError("缺少 Content-Length")
        try:
            length = int(length_header)
        except ValueError as error:
            raise ValidationError("Content-Length 必须是整数") from error
        if length < 0:
            raise ValidationError("Content-Length 不能为负数")
        if length > max_length:
            raise ValidationError(f"请求体不能超过 {max_length // (1024 * 1024)} MB")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValidationError("请求体长度与 Content-Length 不一致")
        return raw

    @staticmethod
    def _query_int(query: Dict[str, Any], key: str, default: int) -> int:
        try:
            return int(query.get(key, [default])[0])
        except (TypeError, ValueError):
            raise ValidationError(f"{key} 必须是整数")

    def _send_html(self, status_code: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status_code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _stream_music(self, track_id: str) -> None:
        """按字节范围输出 16k 单声道 WAV，支持暂停 / 重播后的 Range 续播。"""
        try:
            info = self.portable_service.music.stream_info(track_id)
        except Exception as error:  # noqa: BLE001 - 音频准备失败统一转成 404/500
            self._send_error_json(404, f"音频不可用: {error}")
            return

        data_offset = int(info["data_offset"])
        data_bytes = int(info["data_bytes"])
        if data_bytes <= 0:
            self._send_error_json(404, "音频为空")
            return

        start = 0
        range_header = (self.headers.get("Range") or "").strip()
        if range_header.lower().startswith("bytes="):
            raw = range_header.split("=", 1)[1].split("-", 1)[0].strip()
            if raw.isdigit():
                start = int(raw)
        start = max(0, min(start, data_bytes))
        remaining = data_bytes - start

        header = _pcm_wav_header(remaining)
        self.send_response(206 if range_header else 200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(header) + remaining))
        self.send_header("Cache-Control", "no-store")
        if range_header:
            self.send_header(
                "Content-Range",
                f"bytes {start}-{data_bytes - 1}/{data_bytes}",
            )
        self.end_headers()

        try:
            self.wfile.write(header)
            with open(info["path"], "rb") as handle:
                handle.seek(data_offset + start)
                left = remaining
                while left > 0:
                    chunk = handle.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # 设备暂停 / 停止会直接断连，属于正常路径
            pass

    def _send_binary(self, status_code: int, payload: bytes, content_type: str) -> None:
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _send_error_json(self, status_code: int, message: str) -> None:
        self._send_json(status_code, {"error": message})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[hub] {format % args}")


class HubHttpServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(
        self,
        server_address: Any,
        service: HubService,
        portable_service: Optional[PortableRequestService] = None,
    ):
        super().__init__(server_address, HubRequestHandler)
        self.hub_service = service
        self.portable_service = portable_service or PortableRequestService(service)


def serve(
    service: HubService,
    host: str = "127.0.0.1",
    port: int = 8787,
    portable_service: Optional[PortableRequestService] = None,
) -> None:
    owns_portable_service = portable_service is None
    server = HubHttpServer((host, port), service, portable_service)
    print(f"智能中枢 API 已启动: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止智能中枢...")
    finally:
        server.server_close()
        if owns_portable_service:
            server.portable_service.close()
