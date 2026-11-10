#!/usr/bin/env python3
"""模拟桌宠说一句话，验证「上传音频 -> 识别 -> 回复 -> 拿回语音」整条链路。

桌宠不在手边、或者只想单独验中枢时用它。它做的事和固件 audio_capture.cpp 完全一致：

    POST /api/v1/portable/v1/media          上传音频
    POST /api/v1/portable/v1/requests       建请求（input_type=audio）
    GET  /api/v1/portable/v1/requests/{id}  轮询状态
    GET  /api/v1/portable/v1/requests/{id}/audio  下载回复语音

用法：

    # 用合成语音假装说了一句话（不需要麦克风）
    python3 tools/simulate_pet_voice.py --text "帮我看看现在有哪些设备" --play

    # 用现成的录音文件（手机录的、桌宠导出的都行）
    python3 tools/simulate_pet_voice.py --file /path/to/say.wav --play

    # 连续几轮，验证确认流程
    python3 tools/simulate_pet_voice.py --text "打开客厅灯" --conversation c-1
    python3 tools/simulate_pet_voice.py --text "执行" --conversation c-1 --play
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

DEVICE_ID = "sparkbot_wearable_001"
TERMINAL_STATES = {"completed", "failed", "cancelled", "expired"}

# 本机和局域网地址不能走系统代理，否则会被 HTTP_PROXY 劫持（表现为 502）。
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def synthesize(text: str, voice: Optional[str]) -> Path:
    """用系统 say 造一段 16k 单声道 wav，充当"用户说的话"。"""
    if shutil.which("say") is None:
        raise SystemExit("当前平台没有 say 命令，请改用 --file 指定录音文件")
    handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    path = Path(handle.name)
    handle.close()
    command = ["say", "-o", str(path), "--data-format=LEI16@16000"]
    if voice:
        command += ["-v", voice]
    command.append(text)
    completed = subprocess.run(command, capture_output=True)
    if completed.returncode != 0:
        raise SystemExit(
            "合成语音失败：" + completed.stderr.decode("utf-8", "replace").strip()
        )
    return path


def to_wav_16k(audio: Path) -> Path:
    """把任意音频转成 16k 单声道 wav，模拟桌宠上行的格式。"""
    if audio.suffix.lower() == ".wav":
        return audio
    if shutil.which("ffmpeg") is None:
        raise SystemExit("需要 ffmpeg 才能转换该格式，或者直接给 wav 文件")
    output = Path(tempfile.mkstemp(suffix=".wav")[1])
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(audio),
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(output)],
        check=True,
    )
    return output


def post_media(base_url: str, audio_bytes: bytes) -> str:
    request = urllib.request.Request(
        f"{base_url}/api/v1/portable/v1/media",
        data=audio_bytes,
        headers={
            "X-Portable-Device-Id": DEVICE_ID,
            "X-Media-Type": "audio",
            "X-Audio-Format": "wav",
            "Content-Type": "audio/wav",
        },
        method="POST",
    )
    with OPENER.open(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    media_id = payload.get("media_id")
    if not media_id:
        raise SystemExit(f"上传音频后没拿到 media_id：{payload}")
    return str(media_id)


def post_request(
    base_url: str,
    conversation_id: str,
    media_id: str,
    session_mode: str = "conversation",
) -> str:
    request_id = f"sim-{uuid.uuid4().hex[:12]}"
    body = json.dumps(
        {
            "portable_device_id": DEVICE_ID,
            "conversation_id": conversation_id,
            "request_id": request_id,
            "protocol_version": "1.0",
            "source": "wearable",
            "input_type": "audio",
            "media_id": media_id,
            "session_mode": session_mode,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/api/v1/portable/v1/requests",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with OPENER.open(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return str(payload.get("request_id") or request_id)


def poll(base_url: str, request_id: str, timeout: float) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        with OPENER.open(
            f"{base_url}/api/v1/portable/v1/requests/{request_id}", timeout=15
        ) as response:
            last = json.loads(response.read().decode("utf-8"))
        if str(last.get("state")) in TERMINAL_STATES:
            return last
        time.sleep(0.25)
    return last


def fetch_audio(base_url: str, request_id: str) -> Optional[bytes]:
    try:
        with OPENER.open(
            f"{base_url}/api/v1/portable/v1/requests/{request_id}/audio", timeout=30
        ) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def play(audio: bytes) -> None:
    handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    handle.write(audio)
    handle.close()
    player = shutil.which("afplay") or shutil.which("ffplay")
    if player is None:
        print("（没有可用的播放器，语音已丢弃）")
        return
    if player.endswith("ffplay"):
        subprocess.run([player, "-nodisp", "-autoexit", "-loglevel", "quiet", handle.name])
    else:
        subprocess.run([player, handle.name])
    try:
        os.unlink(handle.name)
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="模拟桌宠一次语音往返")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text", help="用合成语音假装说了这句话")
    source.add_argument("--file", help="直接用这个音频文件（wav/m4a/mp3 都行）")
    parser.add_argument("--base-url", default="http://127.0.0.1:18787")
    parser.add_argument("--conversation", default="sim-conversation")
    parser.add_argument("--voice", help="合成语音的音色，例如 Tingting")
    parser.add_argument(
        "--wake",
        action="store_true",
        help="按唤醒模式发送（session_mode=wake），用来验证唤醒词",
    )
    parser.add_argument("--play", action="store_true", help="播放桌宠的回复语音")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    if args.text:
        audio_path = synthesize(args.text, args.voice)
        print(f"你说：{args.text}")
    else:
        audio_path = to_wav_16k(Path(args.file))
        print(f"音频：{args.file}")

    try:
        audio_bytes = audio_path.read_bytes()
        print(f"      {len(audio_bytes)} 字节 -> 上传中")

        media_id = post_media(base_url, audio_bytes)
        request_id = post_request(
            base_url,
            args.conversation,
            media_id,
            session_mode="wake" if args.wake else "conversation",
        )
        result = poll(base_url, request_id, args.timeout)
    finally:
        if args.text:
            try:
                audio_path.unlink()
            except OSError:
                pass

    state = result.get("state")
    transcript = result.get("transcript") or "（没有转写）"
    reply = result.get("response_text") or "（没有回复）"
    print(f"识别：{transcript}")
    print(f"回复：{reply}")
    print(f"状态：{state}" + (f"  错误：{result.get('error_code')}" if result.get("error_code") else ""))

    audio = fetch_audio(base_url, request_id)
    if audio:
        print(f"语音：{len(audio)} 字节 wav")
        if args.play:
            play(audio)
    else:
        print("语音：这次没有回复音频")

    return 0 if state == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
