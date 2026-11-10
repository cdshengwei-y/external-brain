#!/usr/bin/env python3
"""把 mock 硬件插件暴露成远端接口（MCP Streamable HTTP）。

用途：验证「宿主只连接口、不跑对方 exe」这条路。协议行为与
``tools/mock_hardware_mcp.py`` 完全一致，只是把 stdio 换成了 HTTP POST。

    python3 tools/mock_hardware_mcp_http.py --port 18800

    # 让宿主走这条链路
    export HARDWARE_AGENT_MCP_URL=http://127.0.0.1:18800/mcp

调试开关：

    --sse                 用 text/event-stream 回包（默认 application/json）
    --require-token VAL   只接受 Authorization: Bearer VAL
    --delay-tool NAME     对该工具延迟 30 秒才回包，用来验证超时不重发
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mock_hardware_mcp import MockPlugin  # noqa: E402

PROTOCOL_VERSION = "2025-03-26"


class CapturedPlugin(MockPlugin):
    """复用 stdio 版的行为，只把「写 stdout」换成「返回给 HTTP 层」。"""

    def __init__(self, options: argparse.Namespace) -> None:
        super().__init__(options)
        self._outbox: List[Dict[str, Any]] = []
        self._out_lock = threading.Lock()

    def _write(self, payload: Dict[str, Any]) -> None:
        with self._out_lock:
            self._outbox.append(payload)

    def handle(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        self._handle(message)
        with self._out_lock:
            if self._outbox:
                return self._outbox.pop(0)
            return None


class MockHttpServer:
    def __init__(self, options: argparse.Namespace) -> None:
        self.options = options
        self.plugin = CapturedPlugin(options)
        self.lock = threading.Lock()
        self.sessions: Dict[str, float] = {}
        self.pending_session: Optional[str] = None

    def dispatch(self, message: Dict[str, Any], session: Optional[str]) -> Optional[Dict[str, Any]]:
        method = message.get("method")
        if method == "initialize":
            with self.lock:
                session_id = uuid.uuid4().hex
                self.sessions[session_id] = time.time()
            self.pending_session = session_id
        if method == "tools/call":
            name = (message.get("params") or {}).get("name")
            if name and name in set(self.options.delay_tool):
                time.sleep(30)
        with self.lock:
            return self.plugin.handle(message)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="mock hardware-agent MCP 服务（HTTP 传输）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18800)
    parser.add_argument("--path", default="/mcp", help="接口路径，默认 /mcp")
    parser.add_argument("--sse", action="store_true", help="用 SSE 回包")
    parser.add_argument("--require-token", help="只接受该 Bearer token")
    parser.add_argument("--delay-tool", action="append", default=[], help="对该工具延迟回包")
    parser.add_argument("--timeout-device", action="append", default=[])
    parser.add_argument("--unverified", action="append", default=[])
    parser.add_argument("--hang-tool", action="append", default=[])
    parser.add_argument("--noisy-stderr", action="store_true")
    return parser


def make_handler(server_state: MockHttpServer, options: argparse.Namespace):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "mock-hardware-agent-http/0.1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认日志
            sys.stderr.write("[mock-http] " + fmt % args + "\n")

        def _send(
            self,
            status: int,
            body: bytes = b"",
            content_type: str = "application/json",
            session: Optional[str] = None,
        ) -> None:
            self.send_response(status)
            if body:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if session:
                self.send_header("Mcp-Session-Id", session)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 - http.server 命名
            if self.path.split("?")[0] != options.path:
                self._send(404, b'{"error":"not found"}')
                return
            if options.require_token:
                header = self.headers.get("Authorization") or ""
                if header != f"Bearer {options.require_token}":
                    self._send(401, b'{"error":"unauthorized"}')
                    return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                self._send(
                    400,
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {"code": -32700, "message": "Parse error"},
                        }
                    ).encode("utf-8"),
                )
                return

            session = self.headers.get("Mcp-Session-Id")
            response = server_state.dispatch(message, session)
            new_session = server_state.pending_session
            server_state.pending_session = None

            if response is None:
                self._send(202, b"", session=new_session)
                return

            payload = json.dumps(response, ensure_ascii=False).encode("utf-8")
            if options.sse:
                chunk = b"event: message\ndata: " + payload + b"\n\n"
                self._send(
                    200, chunk, content_type="text/event-stream", session=new_session
                )
            else:
                self._send(200, payload, session=new_session)

        def do_DELETE(self) -> None:  # noqa: N802
            self._send(204)

        def do_GET(self) -> None:  # noqa: N802
            self._send(405, b'{"error":"use POST"}')

    return Handler


def main() -> None:
    options = build_parser().parse_args()
    state = MockHttpServer(options)
    httpd = ThreadingHTTPServer((options.host, options.port), make_handler(state, options))
    print(
        f"mock hardware-agent MCP 已启动: http://{options.host}:{options.port}{options.path}"
        f" (sse={options.sse})",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
