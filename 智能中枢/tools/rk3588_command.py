#!/usr/bin/env python3
"""RK3588 命令 API 直连客户端（无第三方依赖）。

为什么单独写这个而不是直接用硬件插件：
我方还没拿到插件的发布包（payload/mcp_server.py），但 RK3588 的命令 API
本身是可达的 —— 鉴权已验证通过，唯一缺的是路由。所以先把直连通道做好，
路由一确认就能立刻验证，不必等插件包。

路由全部来自 config/rk3588_routes.json，代码里不写死任何路径。
对方填完 COMMAND_API_ROUTE_REQUEST_2026-10-26.md 第 6 节后，只改那个 JSON。

用法：
    export AGENTOS_COMMAND_API_KEY=xxx
    python3 tools/rk3588_command.py probe              # 检查哪些路由还没填
    python3 tools/rk3588_command.py devices            # 列出设备
    python3 tools/rk3588_command.py status  bean_ye_1  # status.read，无副作用
    python3 tools/rk3588_command.py start   bean_ye_1  # record.start，有副作用
    python3 tools/rk3588_command.py stop    bean_ye_1
    python3 tools/rk3588_command.py call    record.start bean_ye_1 --param k=v

约定：只有拿到 HTTP 200 且 status 不是 failed 的响应，才算"服务端已受理"。
拿到受理回执也不等于设备已动作 —— 录音这类写操作必须再看最终状态。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 允许指向另一份路由表，方便在不动正式配置的情况下做 A/B 试探
ROUTE_FILE = Path(os.getenv("RK3588_ROUTE_FILE") or (ROOT / "config" / "rk3588_routes.json"))

# 能力名 -> 路由名
CAP_TO_ROUTE = {
    "status.read": "status_read",
    "record.start": "record_start",
    "record.stop": "record_stop",
}


def die(msg: str, code: int = 2):
    print(f"[错误] {msg}", file=sys.stderr)
    sys.exit(code)


def load_routes() -> dict:
    if not ROUTE_FILE.exists():
        die(f"找不到路由表 {ROUTE_FILE}")
    with ROUTE_FILE.open(encoding="utf-8") as fh:
        return json.load(fh)


def render(tpl, vars_):
    """把 {device_id} 这类占位符替换掉。非字符串原样返回。"""
    if tpl is None:
        return None
    if isinstance(tpl, str):
        out = tpl
        for k, v in vars_.items():
            out = out.replace("{" + k + "}", str(v))
        return out
    if isinstance(tpl, dict):
        return {k: render(v, vars_) for k, v in tpl.items()}
    if isinstance(tpl, list):
        return [render(v, vars_) for v in tpl]
    return tpl


def unresolved(obj) -> list[str]:
    """找出还没替换掉的占位符，避免把 {device_id} 原样发出去。"""
    found = []
    if isinstance(obj, str):
        if "{" in obj and "}" in obj:
            found.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            found += unresolved(v)
    elif isinstance(obj, list):
        for v in obj:
            found += unresolved(v)
    return found


def build_request(cfg: dict, route_name: str, vars_: dict):
    route = cfg.get("routes", {}).get(route_name)
    if not route:
        die(f"路由表里没有 {route_name}")
    path = route.get("path")
    if not path:
        die(f"路由 {route_name} 的 path 还没填，见 config/rk3588_routes.json")

    vars_ = dict(vars_)
    vars_.setdefault("tenant", cfg.get("tenant", ""))

    # 租户按 path 风格时，允许 path 模板里直接带 {tenant}
    path_final = render(path, vars_)
    left = unresolved(path_final)
    if left:
        die(f"路径里还有未替换的占位符：{left}")

    base = (os.getenv("RK3588_BASE_URL") or cfg["base_url"]).rstrip("/")
    url = base + path_final

    body = render(route.get("body"), vars_)
    left = unresolved(body)
    if left:
        die(f"请求体里还有未替换的占位符：{left}")

    headers = {"Accept": "application/json"}
    key = os.getenv("AGENTOS_COMMAND_API_KEY", "").strip()
    if not key:
        die("环境变量 AGENTOS_COMMAND_API_KEY 没设置")
    headers["Authorization"] = f"Bearer {key}"

    style = cfg.get("tenant_style", "header")
    if style == "header":
        headers[cfg.get("tenant_header", "X-Tenant-ID")] = cfg.get("tenant", "")
    elif style == "body" and isinstance(body, dict):
        body.setdefault("tenant_id", cfg.get("tenant", ""))

    method = (route.get("method") or "GET").upper()
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    return url, method, headers, data, body


def call(cfg: dict, route_name: str, vars_: dict, verbose: bool = True):
    url, method, headers, data, body = build_request(cfg, route_name, vars_)
    if verbose:
        print(f"→ {method} {url}")
        if body is not None:
            print(f"  body: {json.dumps(body, ensure_ascii=False)}")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    # 内网直连，绕过系统代理（这台机器上配过代理，会把 192.168 也劫走）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=float(cfg.get("timeout_s", 15))) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status = e.code
    except urllib.error.URLError as e:
        die(f"连不上 {url}：{e.reason}")

    if verbose:
        print(f"← HTTP {status}")
        print(f"  {raw[:800]}")

    if status == 401:
        die("401 —— API Key 无效或没带上", 3)
    if status == 404:
        die("404 —— 路由不对。这个服务是精确匹配，路径差一个字符就是 404；\n"
            "      请核对 config/rk3588_routes.json 里对应项", 4)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {"_raw": raw}

    if isinstance(payload, dict) and payload.get("status") == "failed":
        die(f"服务端拒绝：{payload.get('error_code')} / {payload.get('message')}", 5)
    return status, payload


def cmd_probe(cfg: dict) -> int:
    """检查路由表填了几条、还缺什么。不联网。"""
    base = os.getenv("RK3588_BASE_URL") or cfg.get("base_url")
    print(f"服务地址：{base}")
    print(f"租户    ：{cfg.get('tenant')}（传递方式 {cfg.get('tenant_style')}）")
    print(f"Key     ：{'已设置' if os.getenv('AGENTOS_COMMAND_API_KEY') else '未设置 ← 先 export'}")
    print()
    missing = 0
    for name, route in cfg.get("routes", {}).items():
        path = route.get("path")
        if path:
            print(f"  ✓ {name:16s} {route.get('method','GET'):5s} {path}")
        else:
            print(f"  ✗ {name:16s} 未填")
            missing += 1
    print()
    if missing:
        print(f"还有 {missing} 条路由没确认。需要对方回填 "
              f"COMMAND_API_ROUTE_REQUEST_2026-10-26.md 第 6 节。")
        return 1
    print("路由表已填满，可以跑 devices / status 了。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="RK3588 命令 API 直连客户端")
    ap.add_argument("action",
                    choices=["probe", "devices", "status", "start", "stop", "call"])
    ap.add_argument("device", nargs="?", default="bean_ye_1")
    ap.add_argument("--param", action="append", default=[],
                    help="附加参数 k=v，可重复")
    ap.add_argument("--capability", default=None, help="call 子命令用")
    ap.add_argument("--request-id", default=None)
    args = ap.parse_args()

    cfg = load_routes()

    if args.action == "probe":
        return cmd_probe(cfg)

    params = {}
    for item in args.param:
        if "=" not in item:
            die(f"--param 需要 k=v 形式，收到 {item!r}")
        k, v = item.split("=", 1)
        params[k] = v

    vars_ = {
        "device_id": args.device,
        "request_id": args.request_id or os.urandom(6).hex(),
        "params": params,
        # 路由模板 2026-10-26 版要求这几个字段，探测时给默认值即可
        "conversation_id": "tool-probe",
        "plan_id": "",
        "actor_ref": cfg.get("actor_ref", "hardware-agent"),
        "user_request": "tools/rk3588_command.py 探测",
        "tenant": cfg.get("tenant", ""),
    }

    if args.action == "devices":
        call(cfg, "list_devices", vars_)
        return 0

    if args.action == "call":
        if not args.capability:
            die("call 需要 --capability，例如 --capability record.start")
        capability = args.capability
    else:
        capability = {"status": "status.read",
                      "start": "record.start",
                      "stop": "record.stop"}[args.action]

    route_name = CAP_TO_ROUTE[capability]
    vars_["capability"] = capability
    status, payload = call(cfg, route_name, vars_)

    print()
    print("受理成功。注意：这只代表服务端收下了命令，不代表设备已经动作。")
    if capability != "status.read":
        print("录音这类写操作还要再查一次状态或等 ACK 才能对外播报「已完成」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
