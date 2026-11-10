#!/usr/bin/env python3
"""查看 / 设置桌宠的说话音量档位（1-5，越小越轻）。

    python3 tools/pet_volume.py            # 看当前档位
    python3 tools/pet_volume.py 2          # 设为 2 档
    python3 tools/pet_volume.py --delta -1 # 降一档
    python3 tools/pet_volume.py 2 -d sparkbot_wearable_001

为什么走 HTTP 而不是直接改 ``~/.sparkbot/devices.json``：中枢启动时把影子
读进内存，外部改文件会被它下一次写盘覆盖。档位是中枢的权威值，必须让
中枢自己改。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_BASE = os.environ.get("PET_HUB_BASE_URL", "http://127.0.0.1:18787")
DEFAULT_DEVICE = os.environ.get("SPARKBOT_DEVICE_ID", "sparkbot_wearable_001")


def _call(base: str, device: str, payload=None, timeout: float = 5.0) -> dict:
    url = f"{base.rstrip('/')}/api/v1/portable/v1/devices/{device}"
    data = None
    headers = {}
    if payload is None:
        request = urllib.request.Request(url)
    else:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url + "/volume", data=data)
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise SystemExit(f"中枢返回 {error.code}：{detail}")
    except urllib.error.URLError as error:
        raise SystemExit(
            f"连不上中枢 {base}（{error.reason}）。先 ./tools/start_pet_hub.sh 起中枢。"
        )


def _show(view: dict) -> None:
    print(
        f"{view['device_id']}：说话音量 {view['volume_step']}/{view['volume_total']} 档"
        f"（{view['volume_label']}，增益 {view['volume_gain']}）"
    )
    music_step = view.get("music_step")
    music = "未上报" if music_step is None else f"{music_step} 档"
    print(f"音乐音量：{music}　音乐：{view.get('music')}　屏幕：{view.get('screen')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="查看/设置桌宠说话音量")
    parser.add_argument("step", nargs="?", type=int, help="档位 1-5（1 最小，5 最大）")
    parser.add_argument("--delta", type=int, help="相对调整，如 -1 / +1")
    parser.add_argument("-d", "--device", default=DEFAULT_DEVICE, help="设备 id")
    parser.add_argument("--base-url", default=DEFAULT_BASE, help="中枢地址")
    args = parser.parse_args()

    if args.step is not None and args.delta is not None:
        parser.error("step 和 --delta 只能给一个")

    if args.step is None and args.delta is None:
        _show(_call(args.base_url, args.device))
        return 0

    payload = {"step": args.step} if args.step is not None else {"delta": args.delta}
    _show(_call(args.base_url, args.device, payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
