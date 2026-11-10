#!/usr/bin/env python3
"""测一轮「音频 → 文字 → 意图 → 语音」的端到端耗时。

用法：
    python tools/bench_pipeline.py "你叫什么名字"
    python tools/bench_pipeline.py "启动音乐喷泉" --repeat 3

它用一个本地合成的 16k 单声道 WAV 冒充设备上传的录音，走的是和固件完全
一样的 HTTP 路径（/media + /requests + 轮询），只是省掉了空中那一段。
每段耗时从中枢打出的 [TIMING] 行里读，也会在本地再打一遍客户端侧视角。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

BASE = "http://127.0.0.1:18787/api/v1/portable/v1"
DEVICE_ID = "portable_bench"


def make_wav(text: str) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        aiff = Path(tmp) / "in.aiff"
        wav = Path(tmp) / "out.wav"
        subprocess.run(
            ["say", "-v", "Tingting", "-o", str(aiff), text],
            check=True,
        )
        subprocess.run(
            [
                "afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
                str(aiff), str(wav),
            ],
            check=True,
        )
        return wav.read_bytes()


def upload_audio(data: bytes) -> str:
    request = urllib.request.Request(
        f"{BASE}/media",
        data=data,
        method="POST",
        headers={
            "Content-Type": "audio/wav",
            "X-Portable-Device-Id": DEVICE_ID,
            "X-Media-Type": "audio",
            "X-Audio-Format": "wav",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())["media_id"]


def submit(media_id: str, rid: str) -> None:
    payload = {
        "portable_device_id": DEVICE_ID,
        "conversation_id": "conv_bench",
        "request_id": rid,
        "protocol_version": "1.0",
        "source": "wearable",
        "input_type": "audio",
        "media_id": media_id,
        "session_mode": "conversation",
    }
    request = urllib.request.Request(
        f"{BASE}/requests",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(request, timeout=15).read()


def wait(rid: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with urllib.request.urlopen(f"{BASE}/requests/{rid}", timeout=15) as response:
            data = json.loads(response.read())
        if data["state"] in ("completed", "succeeded", "failed"):
            return data
        time.sleep(0.1)
    return {"state": "timeout"}


def run_once(text: str, index: int) -> float:
    rid = f"bench-{int(time.time())}-{index}"
    audio = make_wav(text)
    media_id = upload_audio(audio)
    started = time.perf_counter()
    submit(media_id, rid)
    data = wait(rid)
    accepted = time.perf_counter() - started
    audio_bytes = 0
    if data.get("state") == "completed":
        try:
            with urllib.request.urlopen(f"{BASE}/requests/{rid}/audio", timeout=15) as r:
                audio_bytes = len(r.read())
        except Exception:
            pass
    print(
        f"  #{index} state={data.get('state')} 轮到完成={accepted * 1000:.0f}ms "
        f"action={data.get('session_action')} reply={data.get('response_text')!r}"
    )
    print(f"      reply_audio={audio_bytes}B  in_audio={len(audio)}B")
    return accepted


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("text", help="要合成给中枢听的话")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()

    samples = []
    for i in range(1, args.repeat + 1):
        samples.append(run_once(args.text, i))
    if samples:
        avg = sum(samples) / len(samples)
        print(f"\n平均 {avg * 1000:.0f}ms（不含空中与设备播放）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
